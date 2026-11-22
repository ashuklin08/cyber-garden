"""Explicit local image-index build. Does not modify the official catalog."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from buyer.contracts import ROOT, decode_json
from buyer.vision.config import Config
from buyer.vision.index import build


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=ROOT / "data/catalog.json")
    args = parser.parse_args()
    from buyer.vision.setup import installation_lock
    config = Config()
    with installation_lock(config.cache):
        metadata, timings = build(config, decode_json(args.catalog.read_bytes()))
    print(json.dumps(dict(indexed=len(metadata["images"]), fingerprint=metadata["fingerprint"], timings=timings)))


if __name__ == "__main__":
    main()
