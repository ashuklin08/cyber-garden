"""An exact, separate image index keyed by existing listing IDs."""
import hashlib
import json
from pathlib import Path
import time
import zipfile

from buyer.vision.config import MODEL_ID, PREPROCESSING, REVISION, WEIGHT_SHA256, safe_path
from buyer.vision.image_validation import decode_image


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def catalog_sha(catalog):
    return sha(json.dumps(catalog, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())


def manifest_entries(config, catalog):
    from buyer.contracts import read_catalog
    listings = read_catalog(catalog)
    manifest_path = config.visual / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError("missing_manifest")
    if manifest_path.stat().st_size > 128_000:
        raise ValueError("manifest_size")
    raw = manifest_path.read_bytes()
    try:
        manifest = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError("invalid_manifest") from None
    if not isinstance(manifest, dict) or manifest.get("version") != 1 or not isinstance(manifest.get("images"), list) or len(manifest["images"]) > 250:
        raise ValueError("invalid_manifest")
    ids = {item.id for item in listings}
    entries, seen_ids, seen_paths, seen_hashes = [], set(), set(), set()
    for row in manifest["images"]:
        if (not isinstance(row, dict) or set(row) != {"listing_id", "path", "synthetic"}
                or not isinstance(row["listing_id"], str) or type(row["synthetic"]) is not bool):
            raise ValueError("invalid_manifest_entry")
        path = safe_path(config.visual, row["path"])
        if not row["path"].startswith("products/"):
            raise ValueError("invalid_product_path")
        if row["listing_id"] not in ids:
            continue
        if not path.is_file() or path.stat().st_size > 5 * 1024 * 1024:
            raise ValueError("missing_or_large_product_image")
        digest = sha(path.read_bytes())
        if row["listing_id"] in seen_ids or row["path"] in seen_paths or digest in seen_hashes:
            raise ValueError("duplicate_product_image")
        seen_ids.add(row["listing_id"]); seen_paths.add(row["path"]); seen_hashes.add(digest)
        entries.append(dict(row, sha256=digest))
    return entries, sha(raw)


def identity(entries, manifest_sha):
    return dict(model=MODEL_ID, revision=REVISION, weights_sha256=WEIGHT_SHA256,
                preprocessing=PREPROCESSING, manifest_sha256=manifest_sha, images=entries)


def build(config, catalog, model=None):
    import numpy as np
    from buyer.vision.model import VisionModel
    started = time.perf_counter()
    entries, manifest_sha = manifest_entries(config, catalog)
    if not entries:
        raise ValueError("no_current_ids")
    model = model or VisionModel(config.model)
    vectors = np.stack([model.embed(decode_image(safe_path(config.visual, row["path"]).read_bytes())) for row in entries])
    metadata = identity(entries, manifest_sha) | dict(catalog_sha256=catalog_sha(catalog))
    metadata["fingerprint"] = sha(json.dumps(identity(entries, manifest_sha), sort_keys=True).encode())
    config.cache.mkdir(parents=True, exist_ok=True)
    target = config.cache / "index.npz"
    temporary = target.with_suffix(".tmp")
    with temporary.open("wb") as stream:
        np.savez_compressed(stream, vectors=vectors, metadata=json.dumps(metadata))
    temporary.replace(target)
    return metadata, dict(model_load_ms=getattr(model, "load_ms", 0), build_ms=(time.perf_counter() - started) * 1000)


def load(config, catalog):
    import numpy as np
    entries, manifest_sha = manifest_entries(config, catalog)
    if not entries:
        raise ValueError("no_current_ids")
    target = config.cache / "index.npz"
    if not target.is_file() or target.stat().st_size > 8_000_000:
        raise FileNotFoundError("missing_index")
    with zipfile.ZipFile(target) as archive:
        if sum(item.file_size for item in archive.infolist()) > 8_000_000:
            raise ValueError("invalid_vectors")
    with np.load(target, allow_pickle=False) as stored:
        vectors = stored["vectors"].copy()
        metadata = json.loads(str(stored["metadata"]))
    if (metadata.get("model") != MODEL_ID or metadata.get("revision") != REVISION
            or metadata.get("weights_sha256") != WEIGHT_SHA256 or metadata.get("preprocessing") != PREPROCESSING
            or metadata.get("manifest_sha256") != manifest_sha):
        raise ValueError("stale_index")
    original = metadata["images"]
    if metadata.get("fingerprint") != sha(json.dumps(identity(original, manifest_sha), sort_keys=True).encode()):
        raise ValueError("stale_index")
    if vectors.shape != (len(original), 768) or not np.isfinite(vectors).all() or not np.allclose(np.linalg.norm(vectors, axis=1), 1, atol=1e-4):
        raise ValueError("invalid_vectors")
    rows = []
    indices = []
    for entry in entries:
        if entry not in original:
            raise ValueError("stale_images")
        rows.append(entry)
        indices.append(original.index(entry))
    return vectors[indices], rows, metadata
