"""Explicit preflight/setup for the optional feature; never runs on app startup."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from buyer.contracts import ROOT, decode_json
from buyer.vision.config import Config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true", help="Allow downloading the pinned weights if needed")
    parser.add_argument("--check", action="store_true", help="Read-only readiness check, no inference or download")
    config = Config()
    parser.add_argument("--catalog", type=Path, default=ROOT / "data/catalog.json")
    parser.add_argument("--model", type=Path, default=config.model)
    parser.add_argument("--cache", type=Path, default=config.cache)
    parser.add_argument("--visual", type=Path, default=config.visual)
    parser.add_argument("--job", default="manual")
    args = parser.parse_args()
    config = replace(config, model=args.model, cache=args.cache, visual=args.visual)
    try:
        catalog = decode_json(args.catalog.read_bytes())
        if args.check:
            from buyer.vision.service import VisualService
            service = VisualService(config)
            try:
                result = service.status(catalog)
            finally:
                service.close()
        else:
            from buyer.vision.setup import run_setup
            result = run_setup(config, catalog, allow_download=args.download, job=args.job)
        print(json.dumps(result, ensure_ascii=True))
        return int(result["status"] != "ready")
    except Exception as error:
        print("Visual demo setup failed: " + type(error).__name__ + ". Check weights, manifest and evaluation images in README.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
