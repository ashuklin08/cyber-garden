"""Exercise first HTTP-triggered install in an empty cache and reuse after Python restarts."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from buyer.contracts import ROOT
from buyer.vision.config import Config, WEIGHT_BYTES, WEIGHT_SHA256


def local(port, route, raw=None, headers=None):
    connection = HTTPConnection("127.0.0.1", port, timeout=45)
    try:
        connection.request("POST" if raw is not None else "GET", route, raw, headers or {})
        response = connection.getresponse()
        status = response.status
        data = response.read()
        return status, data
    finally:
        connection.close()


def json_call(port, route, raw=None, headers=None):
    status, data = local(port, route, raw, headers)
    return status, json.loads(data)


def server_child(model, cache):
    from buyer.demo import DemoServer
    from buyer.vision.setup import SetupManager
    from buyer.model import ModelQuery, ModelTrace
    from buyer.contracts import read_requirements
    from unittest.mock import patch

    config = Config(cache=cache, model=model)
    requirements = read_requirements(dict(city_id="taganrog", budget_rub=15000, categories=["table"],
                                          width_cm=[], adjustable_height=None, color_temperature_k=[]))
    with patch("socket.getfqdn", return_value="localhost"), patch(
        "buyer.vision.setup.SetupManager", side_effect=lambda path: SetupManager(path, config)
    ), patch(
        "buyer.vision.service.VisualService",
        side_effect=lambda: service_type(config)
    ), patch("buyer.demo.extract", return_value=(ModelQuery(requirements, ()), ModelTrace("offline", 1, 1, 1))):
        with DemoServer(0, ROOT / "data/catalog.json") as server:
            print(server.server_port, flush=True)
            server.serve_forever()


def launch(model, cache):
    proc = subprocess.Popen([sys.executable, "-m", "bench.vision_auto_real", "--server", "--workspace", str(cache.parent)],
                            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    line = proc.stdout.readline()
    if not line:
        proc.wait(timeout=10)
        raise RuntimeError(f"Smoke server failed: {proc.returncode}")
    return proc, int(line)


def wait_ready(port):
    phases = []
    deadline = time.monotonic() + 660
    while time.monotonic() < deadline:
        _, result = json_call(port, "/api/visual-status")
        if not phases or phases[-1] != result["status"]:
            phases.append(result["status"])
            print("Phase:", phases[-1], flush=True)
        if result["status"] == "ready":
            return result, phases
        if result["status"] in {"failed", "visual_unavailable", "dependencies_required"}:
            raise RuntimeError(result)
        time.sleep(.75)
    raise TimeoutError("Real setup timed out")


def evaluate(port, config):
    labels = {row["id"]: row["category"] for row in json.loads((ROOT / "data/catalog.json").read_bytes())}
    holdout = json.loads((config.visual / "evaluation.json").read_bytes())["holdout"]
    counts = Counter()
    rows = []
    for sample in holdout:
        image = (config.visual / sample["path"]).read_bytes()
        payload = b'--vision-real\r\nContent-Disposition: form-data; name="image"; filename="query.webp"\r\n\r\n' + image + b"\r\n--vision-real--\r\n"
        started = time.monotonic()
        status, result = json_call(port, "/api/visual-search", payload, {"Content-Type": "multipart/form-data; boundary=vision-real"})
        assert status == 200 and result["status"] in ("ok", "visual_no_match"), result
        ranked = [row["listing_id"] for row in result["results"]]
        expected = sample["listing_id"]
        if expected:
            counts["positive"] += 1
            for k in (1, 3, 5):
                counts[f"recall_at_{k}"] += expected in ranked[:k]
                counts[f"category_at_{k}"] += any(labels[item] == labels[expected] for item in ranked[:k])
        else:
            counts["negative"] += 1
            counts["false_matches"] += bool(ranked)
        rows.append(dict(**sample, results=ranked, wall_ms=round((time.monotonic() - started)*1000, 1)))
    return dict(metrics={key: counts[key] for key in ("positive", "negative", "recall_at_1", "recall_at_3", "recall_at_5",
                                                     "category_at_1", "category_at_3", "category_at_5", "false_matches")},
                samples=rows)


def smoke(workspace):
    model, cache = workspace / "models/nomic-v1.5-int8.onnx", workspace / "cache"
    assert not model.exists() and not (cache / "index.npz").exists()
    proc, port = launch(model, cache)
    try:
        assert local(port, "/")[0] == 200 and not model.exists() and not cache.exists()
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=5) as executor:
            first = list(executor.map(lambda _: json_call(port, "/api/visual-status")[1]["status"], range(5)))
        assert all(value in {"checking", "preparing", "downloading", "verifying"} for value in first), first
        status, buy = json_call(port, "/api/buy", json.dumps({"messages": [{"role": "user",
            "text": "Стол в Таганроге, бюджет 15000 рублей"}]}).encode(), {"Content-Type": "application/json"})
        assert status == 200 and buy["response"]["status"] == "ok" and not model.exists(), buy
        ready, phases = wait_ready(port)
        elapsed = round(time.monotonic() - started, 2)
        assert ready["downloaded"] and ready["index_built"] and ready["calibrated"], ready
        with model.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        assert model.stat().st_size == WEIGHT_BYTES and digest == WEIGHT_SHA256
        timestamps = (model.stat().st_mtime_ns, (cache / "index.npz").stat().st_mtime_ns)
        assert json_call(port, "/api/visual-status")[1] == ready
        assert timestamps == (model.stat().st_mtime_ns, (cache / "index.npz").stat().st_mtime_ns)
        report = evaluate(port, Config(model=model, cache=cache))
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    proc, port = launch(model, cache)
    try:
        reused, _ = wait_ready(port)
        assert not any(reused[key] for key in ("downloaded", "index_built", "calibrated")), reused
        assert timestamps == (model.stat().st_mtime_ns, (cache / "index.npz").stat().st_mtime_ns)
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    return dict(**report, first_ready=ready, first_wall_s=elapsed, phases=phases, parallel_status=first,
                buy_during_setup="ok (mock Gemma)", sha256=digest, bytes=WEIGHT_BYTES,
                repeat_same_process="unchanged mtime", restarted=reused)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "bench/results/vision-auto-real.json")
    args = parser.parse_args()
    if args.server:
        server_child(args.workspace / "models/nomic-v1.5-int8.onnx", args.workspace / "cache")
    else:
        with tempfile.TemporaryDirectory(dir=args.workspace) as temporary:
            report = smoke(Path(temporary))
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report["metrics"]), flush=True)


if __name__ == "__main__":
    from buyer.vision.service import VisualService as service_type
    main()
