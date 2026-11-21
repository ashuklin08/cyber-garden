"""Explicit preflight/setup for the optional feature; never runs on app startup."""
import argparse
import importlib.util
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
    args = parser.parse_args()
    if any(importlib.util.find_spec(name) is None for name in ("numpy", "PIL", "onnxruntime")):
        print("Install optional dependencies: python -m pip install -r requirements-vision.txt", file=sys.stderr)
        return 1
    try:
        if not args.check:
            if args.download:
                from scripts.download_vision_model import main as download
                download()
            from buyer.vision.index import build
            from scripts.calibrate_visual_search import main as calibrate
            build(Config(), decode_json((ROOT / "data/catalog.json").read_bytes()))
            calibrate()
        from buyer.vision.service import VisualService
        service = VisualService()
        try:
            result = service.status(decode_json((ROOT / "data/catalog.json").read_bytes()))
        finally:
            service.close()
        print(json.dumps(result, ensure_ascii=False))
        return int(result["status"] != "ready")
    except Exception as error:
        print("Visual demo setup failed: " + type(error).__name__ + ". Check weights, manifest and evaluation images in README.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
