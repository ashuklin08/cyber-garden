"""Real HTTP routing and graceful failures; inference is explicitly stubbed here."""
import builtins
from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from buyer.demo import DemoServer
from buyer.model import ModelQuery, ModelTrace
from tests.fixtures import product
from tests.test_conversation import expected


def multipart(image=b"untrusted-bytes", name="image", filename="photo.png"):
    boundary = "smartset-test-boundary"
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            'Content-Type: application/not-trusted\r\n\r\n').encode() + image + f"\r\n--{boundary}--\r\n".encode()
    return body, {"Content-Type": "multipart/form-data; boundary=" + boundary}


class VisionApiTest(unittest.TestCase):
    def test_local_preview_is_allowed_without_remote_image_sources(self):
        from buyer.demo import CSP
        self.assertIn("img-src 'self' blob:", CSP)
        self.assertNotIn("https:", CSP)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name) / "catalog.json"
        path.write_text(json.dumps([product("real-id", "table", 1000, 90)]))
        with patch("socket.getfqdn", return_value="localhost"):
            self.server = DemoServer(0, path)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01})
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)

    def request(self, path="/api/visual-search", body=None, headers=None, method="POST"):
        body, defaults = multipart() if body is None else (body, {})
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=15)
        try:
            connection.request(method, path, body, defaults | (headers or {}))
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_photo_uses_separate_route_without_nlu(self):
        self.server.visual_service = Mock()
        self.server.visual_service.search.return_value = dict(status="visual_no_match", message="Нет похожих товаров")
        with patch("buyer.demo.extract", side_effect=AssertionError("Gemma must not be called")):
            status, result = self.request()
        self.assertEqual((status, result["status"]), (200, "visual_no_match"))
        raw, catalog = self.server.visual_service.search.call_args.args
        self.assertEqual(raw, b"untrusted-bytes")
        self.assertEqual(catalog[0]["id"], "real-id")

    def test_broken_or_missing_optional_module_does_not_break_server_or_buy(self):
        real_import = builtins.__import__
        def blocked(name, *args, **kwargs):
            if name.startswith("buyer.vision"):
                raise ImportError("module absent")
            return real_import(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=blocked):
            status, result = self.request()
            self.assertEqual((status, result["status"]), (200, "visual_unavailable"))
            status, _ = self.request("/api/catalog", b"", method="GET")
            self.assertEqual(status, 200)
            with patch("buyer.demo.extract", return_value=(ModelQuery(expected(city_id="taganrog", budget_rub=5000, categories=["table"]), ()), ModelTrace("mock", 1, 1, 1))):
                status, result = self.request("/api/buy", json.dumps(dict(messages=[dict(role="user", text="Стол в Таганроге, бюджет 5000 рублей")])).encode(), {"Content-Type": "application/json"})
            self.assertEqual(status, 200)
            self.assertEqual(result["response"]["status"], "ok")

    def test_model_exception_is_a_controlled_visual_response(self):
        self.server.visual_service = Mock()
        self.server.visual_service.search.side_effect = RuntimeError("private model exception")
        status, result = self.request()
        self.assertEqual((status, result["status"]), (200, "visual_unavailable"))
        self.assertNotIn("private", str(result))

    def test_no_urls_or_extra_parts_accepted(self):
        requests = [(b'{"url":"https://example.org/photo.png"}', {"Content-Type": "application/json"}),
                    multipart(name="url"), multipart(image=b""),
                    (b"x", {"Content-Type": "multipart/form-data"})]
        for raw, headers in requests:
            with self.subTest(headers=headers):
                status, result = self.request(body=raw, headers=headers)
                self.assertEqual(status, 400)
                self.assertEqual(result["status"], "invalid_image")

    def test_large_upload_rejected_before_reading_body(self):
        status, result = self.request(body=b"", headers={"Content-Type": "multipart/form-data; boundary=x", "Content-Length": str(6 * 1024 * 1024)})
        self.assertEqual(status, 413)
        self.assertEqual(result["status"], "invalid_image")

    def test_untrusted_filename_never_used_as_filesystem_path(self):
        self.server.visual_service = Mock()
        self.server.visual_service.search.return_value = dict(status="invalid_image", message="Повреждено")
        before = list(Path(self.temp.name).rglob("*"))
        raw, headers = multipart(filename="../../secret.jpg")
        status, result = self.request(body=raw, headers=headers)
        self.assertEqual(status, 400)
        self.assertEqual(list(Path(self.temp.name).rglob("*")), before)

    def test_visual_busy_is_independent_of_gemma_slot(self):
        self.server.visual_slot.acquire()
        try:
            status, result = self.request()
        finally:
            self.server.visual_slot.release()
        self.assertEqual((status, result["status"]), (429, "visual_busy"))
        self.assertTrue(self.server.gateway_slot.acquire(blocking=False))
        self.server.gateway_slot.release()

    def test_missing_assets_and_dependencies_remain_unavailable(self):
        try:
            from buyer.vision.config import Config
            from buyer.vision.service import VisualService
        except ImportError:
            self.skipTest("Visual module removed")
        self.server.visual_service = VisualService(Config(visual=Path(self.temp.name), cache=Path(self.temp.name), model=Path(self.temp.name) / "absent"))
        # A valid small PNG without relying on Pillow in the main server process.
        import base64
        raw, headers = multipart(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aL1sAAAAASUVORK5CYII="))
        status, result = self.request(body=raw, headers=headers)
        self.assertIn(status, (200, 400))  # corrupted PNG is still a controlled validation error
        self.assertIn(result["status"], ("visual_unavailable", "invalid_image"))

    def test_actual_worker_missing_model_index_and_corrupt_manifest(self):
        from tests import test_vision
        if not test_vision.AVAILABLE:
            self.skipTest("Optional image test dependencies missing")
        from buyer.vision.service import VisualService
        fixture = test_vision.VisionTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.server.catalog = fixture.catalog
        for missing in ("missing_model", "missing_index", "invalid_manifest"):
            with self.subTest(missing=missing):
                if self.server.visual_service:
                    self.server.visual_service.close()
                fixture.manifest()
                fixture.build()
                if missing == "missing_index":
                    (fixture.config.cache / "index.npz").unlink()
                elif missing == "invalid_manifest":
                    (fixture.root / "manifest.json").write_text("not json")
                self.server.visual_service = VisualService(fixture.config)
                raw, headers = multipart(fixture.image())
                status, result = self.request(body=raw, headers=headers)
                self.assertEqual((status, result["status"]), (200, "visual_unavailable"))
                if missing != "invalid_manifest":
                    self.assertEqual(result["code"], missing)

    def test_cross_origin_visual_upload_is_rejected(self):
        status, _ = self.request(body=b"", headers={"Origin": "https://unrelated.example"})
        self.assertEqual(status, 403)

    def test_readiness_and_demo_routes_do_not_call_gemma(self):
        self.server.visual_setup = Mock()
        self.server.visual_setup.status.return_value = dict(status="ready", coverage=dict(indexed=1, total=1))
        with patch("buyer.demo.extract", side_effect=AssertionError("No NLU for visual readiness")):
            status, result = self.request("/api/visual-status", b"", method="GET")
        self.assertEqual((status, result["status"]), (200, "ready"))
        self.server.visual_setup.status.assert_called_once_with(retry=False)
        for path in ("/visual-demo/../../.env", "/visual-demo/unknown", "/visual-demo/table?file=.env"):
            status, _ = self.request(path, b"", method="GET")
            self.assertEqual(status, 404)

    def test_readiness_unavailable_does_not_break_catalog(self):
        self.server.visual_setup = Mock()
        self.server.visual_setup.status.side_effect = ImportError("native dependency failed")
        status, result = self.request("/api/visual-status", b"", method="GET")
        self.assertEqual((status, result["status"]), (200, "visual_unavailable"))
        status, _ = self.request("/api/catalog", b"", method="GET")
        self.assertEqual(status, 200)

    def test_image_paths_not_in_manifest_cannot_be_served(self):
        status, _ = self.request("/visual-images/../catalog.json", b"", method="GET")
        self.assertEqual(status, 404)
