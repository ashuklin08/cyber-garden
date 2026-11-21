"""Calibrate on frozen positive/negative photos; evaluate separate holdout photos."""
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from buyer.contracts import ROOT, decode_json
from buyer.vision.config import Config, MODEL_ID, REVISION, safe_path
from buyer.vision.index import load, sha
from buyer.vision.image_validation import decode_image
from buyer.vision.model import VisionModel


def main():
    config = Config()
    catalog = decode_json((ROOT / "data/catalog.json").read_bytes())
    vectors, entries, metadata = load(config, catalog)
    model = VisionModel(config.model)
    evaluation_bytes = (config.visual / "evaluation.json").read_bytes()
    evaluation = json.loads(evaluation_bytes)
    ids = [row["listing_id"] for row in entries]
    positives, negatives, observations, durations = [], [], [], []
    for split in ("calibration", "holdout"):
        for sample in evaluation[split]:
            raw = safe_path(config.visual, sample["path"]).read_bytes()
            started = time.perf_counter()
            embedding = model.embed(decode_image(raw))
            durations.append((time.perf_counter() - started) * 1000)
            scores = vectors @ embedding
            expected_id = sample["listing_id"]
            best = int(scores.argmax())
            observations.append(dict(split=split, path=sample["path"], sha256=sha(raw),
                expected_id=expected_id, top_id=ids[best], top_cosine=float(scores[best]),
                scores={key: float(value) for key, value in zip(ids, scores)}))
            if split == "calibration":
                for key, score in zip(ids, scores):
                    (positives if key == expected_id else negatives).append(float(score))
    if not positives or not negatives or min(positives) <= max(negatives):
        raise ValueError("Calibration distributions overlap; collect better labelled data before enabling search")
    threshold = (min(positives) + max(negatives)) / 2
    for row in observations:
        if row["split"] == "holdout":
            row["passed"] = (row["top_cosine"] < threshold if row["expected_id"] is None else
                             row["top_id"] == row["expected_id"] and row["top_cosine"] >= threshold)
    report = dict(model=MODEL_ID, revision=REVISION, fingerprint=metadata["fingerprint"],
        method="midpoint_between_min_positive_and_max_negative_cosine_on_frozen_calibration_pairs",
        evaluation_sha256=sha(evaluation_bytes), threshold=threshold,
        min_positive=min(positives), max_negative=max(negatives),
        positive_pairs=len(positives), negative_pairs=len(negatives),
        limitations="Small synthetic demo only; no claim of production accuracy or calibrated probabilities.",
        model_load_ms=model.load_ms, query_embedding_ms=durations, observations=observations)
    (ROOT / "bench/results/vision-calibration.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if not all(row.get("passed", True) for row in observations):
        raise ValueError("Holdout failed; calibration not installed")
    (config.visual / "calibration.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(dict(threshold=threshold, positive_pairs=len(positives), negative_pairs=len(negatives), holdout="2/2")))


if __name__ == "__main__":
    main()
