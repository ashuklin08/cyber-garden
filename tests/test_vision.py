"""Optional vision unit tests use explicit fake vectors, never claim real inference."""
from dataclasses import replace
import importlib.util
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.fixtures import product

AVAILABLE = all(importlib.util.find_spec(name) is not None for name in ("numpy", "PIL", "buyer.vision"))


class FakeModel:
    load_ms = 0

    def embed(self, image):
        import numpy as np
        color = image.getpixel((0, 0))
        vector = np.zeros(768, dtype=np.float32)
        vector[int(np.argmax(color))] = 1
        return vector


@unittest.skipUnless(AVAILABLE, "Optional vision dependencies/module not installed")
class VisionTest(unittest.TestCase):
    def setUp(self):
        from buyer.vision.config import Config
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "products").mkdir()
        self.config = Config(visual=self.root, cache=self.root / "cache", model=self.root / "absent.onnx")
        self.catalog = [product("red", "table", 1000, 90), product("green", "chair", 2000, True)]
        self.rows = [dict(listing_id="red", path="products/red.png", synthetic=True),
                     dict(listing_id="green", path="products/green.png", synthetic=True)]
        for name, color in (("red", (255, 0, 0)), ("green", (0, 255, 0))):
            (self.root / f"products/{name}.png").write_bytes(self.image(color=color))
        self.manifest()

    def image(self, fmt="PNG", size=(24, 20), color=(255, 0, 0), exif=None):
        from PIL import Image
        stream = BytesIO()
        Image.new("RGB", size, color).save(stream, format=fmt, **({"exif": exif} if exif else {}))
        return stream.getvalue()

    def manifest(self):
        (self.root / "manifest.json").write_text(json.dumps(dict(version=1, images=self.rows)), encoding="utf-8")

    def build(self):
        from buyer.vision.index import build
        metadata, _ = build(self.config, self.catalog, FakeModel())
        (self.root / "calibration.json").write_text(json.dumps(dict(fingerprint=metadata["fingerprint"],
            threshold=.9, method="test-fixture-orthogonal-fake-vectors")), encoding="utf-8")
        return metadata

    def engine(self):
        from buyer.vision.service import Engine
        engine = Engine(self.config)
        engine.model = FakeModel()
        return engine

    def test_valid_formats_real_signature_not_extension_or_declared_mime(self):
        from buyer.vision.image_validation import decode_image
        for fmt in ("JPEG", "PNG", "WEBP"):
            with self.subTest(fmt=fmt):
                image = decode_image(self.image(fmt))
                self.assertEqual(image.mode, "RGB")
                self.assertEqual(image.size, (24, 20))

    def test_corrupt_and_unsupported_images(self):
        from buyer.vision.image_validation import decode_image, InvalidImage
        for raw in (b"not-an-image", b"\x89PNG\r\n\x1a\n", self.image("GIF")):
            with self.subTest(raw=raw[:10]), self.assertRaises(InvalidImage):
                decode_image(raw)

    def test_file_and_resolution_limits(self):
        from buyer.vision.image_validation import decode_image, InvalidImage
        for raw in (b"x" * (5 * 1024 * 1024 + 1), self.image(size=(2049, 10))):
            with self.assertRaises(InvalidImage):
                decode_image(raw)

    def test_exif_orientation_and_rgb(self):
        from PIL import Image
        from buyer.vision.image_validation import decode_image
        exif = Image.Exif(); exif[274] = 6
        self.assertEqual(decode_image(self.image("JPEG", exif=exif)).size, (20, 24))

    def test_missing_model_and_index(self):
        from buyer.vision.model import VisionModel
        from buyer.vision.index import load
        with self.assertRaises(FileNotFoundError):
            VisionModel(self.config.model)
        with self.assertRaises(FileNotFoundError):
            load(self.config, self.catalog)

    def test_corrupt_manifest(self):
        from buyer.vision.index import manifest_entries
        (self.root / "manifest.json").write_text("{invalid")
        with self.assertRaises(ValueError):
            manifest_entries(self.config, self.catalog)

    def test_unknown_id_is_excluded_not_invented(self):
        from buyer.vision.index import manifest_entries
        self.rows.append(dict(listing_id="not-real", path="products/missing.png", synthetic=True))
        self.manifest()
        entries, _ = manifest_entries(self.config, self.catalog)
        self.assertEqual([row["listing_id"] for row in entries], ["red", "green"])

    def test_catalog_replacement_and_partial_coverage(self):
        self.build()
        with self.assertRaisesRegex(ValueError, "no_current_ids"):
            self.engine().search(self.image(), [product("replacement", "table", 50)])
        result = self.engine().search(self.image(), self.catalog[:1])
        self.assertEqual(result["coverage"], dict(indexed=1, total=1))
        self.assertEqual(result["results"][0]["listing_id"], "red")
        self.assertTrue(result["catalog_changed"])

    def test_prices_and_city_always_come_from_current_catalog(self):
        self.build()
        changed = [self.catalog[0] | dict(price_rub=7654, city_id="azov"), self.catalog[1]]
        result = self.engine().search(self.image(), changed)
        self.assertEqual(result["results"][0]["price_rub"], 7654)
        self.assertEqual(result["results"][0]["city"], "Азов")
        self.assertEqual(result["results"][0]["rank"], 1)
        self.assertNotIn("similarity", result["results"][0])

    def test_low_similarity_has_no_result(self):
        self.build()
        result = self.engine().search(self.image(color=(0, 0, 255)), self.catalog)
        self.assertEqual(result["status"], "visual_no_match")
        self.assertEqual(result["results"], [])

    def test_manifest_image_changes_and_preprocessing_invalidate_index(self):
        from buyer.vision.index import load
        self.build()
        (self.root / "products/red.png").write_bytes(self.image(color=(123, 0, 0)))
        with self.assertRaisesRegex(ValueError, "stale_images"):
            load(self.config, self.catalog)
        self.build()
        self.rows[0]["synthetic"] = False
        self.manifest()
        with self.assertRaisesRegex(ValueError, "stale_index"):
            load(self.config, self.catalog)

    def test_duplicate_photo_and_traversal_are_rejected(self):
        from buyer.vision.index import manifest_entries
        for path in ("../outside.png", "products/../../outside.png", "C:/secret.png", "https://example.org/x.png"):
            self.rows[0]["path"] = path; self.manifest()
            with self.assertRaises(ValueError):
                manifest_entries(self.config, self.catalog)
        self.rows[0]["path"] = "products/red.png"
        self.rows[1]["path"] = "products/red.png"; self.manifest()
        with self.assertRaisesRegex(ValueError, "duplicate_product_image"):
            manifest_entries(self.config, self.catalog)

    def test_model_exception_is_contained_inside_worker(self):
        from buyer.vision.service import worker
        class Connection:
            def __init__(inner): inner.requests = iter([(b"photo", self.catalog), None]); inner.sent = []
            def recv(inner): return next(inner.requests)
            def send(inner, result): inner.sent.append(result)
            def close(inner): pass
        connection = Connection()
        with patch("buyer.vision.service.limit_worker"), patch("buyer.vision.service.Engine") as engine:
            engine.return_value.search.side_effect = RuntimeError("inference_failed_private_detail")
            worker(connection, self.config)
        self.assertEqual(connection.sent[0]["status"], "visual_unavailable")
        self.assertNotIn("private_detail", str(connection.sent))
        connection = Connection()
        with patch("buyer.vision.service.limit_worker"), patch("buyer.vision.service.Engine") as engine:
            engine.return_value.search.side_effect = ImportError("PIL missing")
            worker(connection, self.config)
        self.assertEqual(connection.sent[0]["code"], "missing_dependencies")

    def test_invalid_index_never_executes_pickle(self):
        import numpy as np
        from buyer.vision.index import load
        self.config.cache.mkdir()
        np.savez(self.config.cache / "index.npz", vectors=np.array([object()], dtype=object), metadata="{}")
        with self.assertRaises(ValueError):
            load(self.config, self.catalog)
