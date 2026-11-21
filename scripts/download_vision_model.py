"""Explicit, one-time download of pinned public ONNX weights; never called by demo."""
import hashlib
from pathlib import Path
import sys
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from buyer.vision.config import Config, MODEL_ID, REVISION, WEIGHT_FILE, WEIGHT_SHA256, WEIGHT_BYTES


def main():
    target = Config().model
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == WEIGHT_SHA256:
        print("Pinned vision weights already present")
        return
    temporary = target.with_suffix(".download")
    digest = hashlib.sha256()
    size = 0
    try:
        with urlopen(f"https://huggingface.co/{MODEL_ID}/resolve/{REVISION}/{WEIGHT_FILE}", timeout=60) as response, temporary.open("wb") as stream:
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > WEIGHT_BYTES:
                    raise ValueError("Unexpected model size")
                digest.update(chunk)
                stream.write(chunk)
        if size != WEIGHT_BYTES or digest.hexdigest() != WEIGHT_SHA256:
            raise ValueError("Model integrity check failed")
        temporary.replace(target)
        print(f"Verified {size} bytes: {target}")
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
