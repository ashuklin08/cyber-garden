"""Single installer for pinned public ONNX weights."""
import hashlib
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from buyer.vision.config import Config, MODEL_ID, REVISION, WEIGHT_FILE, WEIGHT_SHA256, WEIGHT_BYTES


def download(target, progress=lambda *_args, **_fields: None):
    from buyer.vision.model import verify_weights
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".download")
    temporary.unlink(missing_ok=True)
    progress("verifying")
    try:
        verify_weights(target)
        return False
    except (FileNotFoundError, ValueError):
        pass
    for attempt in range(2):
        progress("downloading", downloaded_bytes=0, total_bytes=WEIGHT_BYTES)
        digest, size = hashlib.sha256(), 0
        try:
            with urlopen(f"https://huggingface.co/{MODEL_ID}/resolve/{REVISION}/{WEIGHT_FILE}", timeout=35) as response, temporary.open("wb") as stream:
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > WEIGHT_BYTES:
                        raise ValueError("model_integrity")
                    digest.update(chunk)
                    stream.write(chunk)
                    progress("downloading", downloaded_bytes=size, total_bytes=WEIGHT_BYTES)
            progress("verifying")
            if size != WEIGHT_BYTES or digest.hexdigest() != WEIGHT_SHA256:
                raise ValueError("model_integrity")
            temporary.replace(target)
            return True
        except HTTPError as error:
            if error.code in (401, 403):
                raise ValueError("download_denied") from None
            if attempt or error.code not in (408, 429, 500, 502, 503, 504):
                raise ValueError("download_failed") from None
        except (URLError, TimeoutError, ConnectionError):
            if attempt:
                raise ValueError("download_failed") from None
        finally:
            temporary.unlink(missing_ok=True)
        time.sleep(1)


def main():
    from buyer.vision.setup import installation_lock
    config = Config()
    with installation_lock(config.cache):
        installed = download(config.model)
    print("Verified and installed weights" if installed else "Pinned vision weights already present")


if __name__ == "__main__":
    main()
