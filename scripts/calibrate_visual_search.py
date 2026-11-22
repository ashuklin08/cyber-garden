"""Calibrate on frozen positive/negative photos; evaluate separate holdout photos."""
import json
import argparse
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from buyer.contracts import ROOT, decode_json
from buyer.vision.config import Config, MODEL_ID, REVISION, safe_path
from buyer.vision.index import load, sha, manifest_sha256
from buyer.vision.image_validation import decode_image
from buyer.vision.model import VisionModel


def calibrate(config, catalog, *, metadata, rows, vectors):
    model = VisionModel(config.model, verified=True)
    evaluation_bytes = (config.visual / "evaluation.json").read_bytes()
    evaluation = json.loads(evaluation_bytes)
    ids = [row["listing_id"] for row in rows]
    categories = {item["id"]: item["category"] for item in catalog}
    positives, negatives, observations, durations = [], [], [], []
    for sample in evaluation["calibration"]:
        raw = safe_path(config.visual, sample["path"]).read_bytes()
        started = time.perf_counter()
        embedding = model.embed(decode_image(raw))
        durations.append((time.perf_counter() - started) * 1000)
        scores = vectors @ embedding
        expected_id = sample["listing_id"]
        best = int(scores.argmax())
        observations.append(dict(path=sample["path"], sha256=sha(raw),
            expected_id=expected_id, top_id=ids[best], top_cosine=float(scores[best]),
            scores={key: float(value) for key, value in zip(ids, scores)}))
        if expected_id is None:
            negatives.append(float(scores[best]))
        elif expected_id in ids:
            positives.append(float(scores[ids.index(expected_id)]))
            wrong = [float(score) for key, score in zip(ids, scores) if categories[key] != categories[expected_id]]
            if wrong:
                negatives.append(max(wrong))
    if not positives or not negatives:
        raise ValueError("not_enough_calibration_examples")
    if min(positives) <= max(negatives):
        raise ValueError("calibration_overlap")
    threshold = (min(positives) + max(negatives)) / 2
    return dict(model=MODEL_ID, revision=REVISION, fingerprint=metadata["fingerprint"],
        manifest_sha256=metadata["manifest_sha256"], weights_sha256=metadata["weights_sha256"],
        preprocessing=metadata["preprocessing"], images=metadata["images"],
        calibration_images=[dict(path=row["path"], sha256=row["sha256"]) for row in observations],
        method="midpoint_between_min_positive_and_max_negative_cosine_on_frozen_calibration_pairs",
        evaluation_sha256=manifest_sha256(evaluation), threshold=threshold,
        min_positive=min(positives), max_negative=max(negatives),
        positive_pairs=len(positives), negative_pairs=len(negatives),
        limitations="Small synthetic demo only; no claim of production accuracy or calibrated probabilities.",
        model_load_ms=model.load_ms, query_embedding_ms=durations, observations=observations)


def main():
    from buyer.vision.setup import atomic_json, installation_lock
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=ROOT / "data/catalog.json")
    args = parser.parse_args()
    config = Config()
    catalog = decode_json(args.catalog.read_bytes())
    with installation_lock(config.cache):
        from buyer.vision.model import verify_weights
        verify_weights(config.model)
        vectors, rows, metadata = load(config, catalog)
        report = calibrate(config, catalog, metadata=metadata, rows=rows, vectors=vectors)
        atomic_json(config.cache / "calibration.json", report)
    print(json.dumps(dict(threshold=report["threshold"], positive_pairs=report["positive_pairs"],
                          negative_pairs=report["negative_pairs"])))


if __name__ == "__main__":
    main()
