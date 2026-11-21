"""Real local CPU weights, separate holdout images and actual multipart HTTP API."""
from http.client import HTTPConnection
import json
import threading
import time
from unittest.mock import patch

from buyer.contracts import ROOT
from buyer.demo import DemoServer
from buyer.vision.config import Config, MODEL_ID, REVISION, WEIGHT_BYTES, WEIGHT_SHA256
from buyer.vision.index import build
from buyer.contracts import decode_json


def main():
    config = Config()
    metadata, build_times = build(config, decode_json((ROOT / "data/catalog.json").read_bytes()))
    report = dict(model=MODEL_ID, revision=REVISION, weight_bytes=WEIGHT_BYTES, weight_sha256=WEIGHT_SHA256,
                  cpu=True, real_weights=True, build=build_times, cases=[])
    # Only DNS reverse lookup is stubbed for fast local HTTP-server setup.
    with patch("socket.getfqdn", return_value="localhost"):
        server = DemoServer(0, ROOT / "data/catalog.json")
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01})
    thread.start()
    try:
        for name, expected in (("table", "t12"), ("chair", "c04"), ("lamp", "l09"), ("cat", None), ("landscape", None)):
            raw = (config.visual / f"queries/{name}.png").read_bytes()
            body = b'--vision-demo\r\nContent-Disposition: form-data; name="image"; filename="query.png"\r\nContent-Type: image/png\r\n\r\n' + raw + b"\r\n--vision-demo--\r\n"
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=40)
            started = time.perf_counter()
            try:
                connection.request("POST", "/api/visual-search", body, {"Content-Type": "multipart/form-data; boundary=vision-demo"})
                response = connection.getresponse()
                result = json.loads(response.read())
                wall_ms = (time.perf_counter() - started) * 1000
                passed = response.status == 200 and (result["status"] == "visual_no_match" if expected is None else
                          result["status"] == "ok" and result["results"][0]["listing_id"] == expected)
                report["cases"].append(dict(name=name, expected=expected, passed=passed, wall_ms=wall_ms, response=result))
                print(("PASS " if passed else "FAIL ") + name, flush=True)
            finally:
                connection.close()
    finally:
        server.shutdown(); thread.join(); server.server_close()
    report["passed"] = sum(row["passed"] for row in report["cases"])
    target = ROOT / "bench/results/vision-real.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return int(report["passed"] != 5)


if __name__ == "__main__":
    raise SystemExit(main())
