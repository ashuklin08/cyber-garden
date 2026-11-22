"""Exercise optional installation state and the pinned downloader without network."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from buyer.vision.config import Config
from buyer.vision.setup import SetupManager, atomic_json, installation_lock
from scripts.download_vision_model import download


class SetupTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = replace(Config(), cache=self.root / "cache", model=self.root / "model.onnx")

    def test_parallel_pollers_start_one_child_and_ready_is_reused(self):
        manager = SetupManager(self.root / "catalog.json", self.config)
        self.addCleanup(manager.close)
        child = Mock()
        child.poll.return_value = None
        with patch("buyer.vision.setup.subprocess.Popen", return_value=child) as launch:
            with ThreadPoolExecutor(max_workers=6) as pool:
                statuses = list(pool.map(lambda _: manager.status()["status"], range(8)))
            launch.assert_called_once()
            self.assertTrue(set(statuses) <= {"checking", "preparing"})
            atomic_json(self.config.cache / f"setup-{manager.job}.json", {"status": "ready"})
            child.poll.return_value = 0
            self.assertEqual(manager.status(), {"status": "ready"})
            self.assertEqual(manager.status(), {"status": "ready"})
            launch.assert_called_once()

    def test_failure_stays_terminal_until_explicit_retry(self):
        manager = SetupManager(self.root / "catalog.json", self.config)
        self.addCleanup(manager.close)
        with patch("buyer.vision.setup.subprocess.Popen", side_effect=OSError) as launch:
            self.assertEqual(manager.status()["status"], "failed")
            self.assertEqual(manager.status()["status"], "failed")
            self.assertEqual(launch.call_count, 1)
            self.assertEqual(manager.status(retry=True)["status"], "failed")
            self.assertEqual(launch.call_count, 2)

    def test_installation_lock_releases_even_when_setup_fails(self):
        with self.assertRaisesRegex(RuntimeError, "broken setup"):
            with installation_lock(self.config.cache):
                with self.assertRaises(TimeoutError):
                    with installation_lock(self.config.cache, timeout=0):
                        self.fail("Two writers acquired the same installation lock")
                raise RuntimeError("broken setup")
        with installation_lock(self.config.cache, timeout=0):
            pass

    def test_catalog_with_no_matching_ids_never_calls_downloader(self):
        from buyer.vision.setup import run_setup
        with patch("scripts.download_vision_model.download") as network:
            result = run_setup(self.config, [])
        self.assertEqual(result["code"], "no_current_ids")
        network.assert_not_called()

    def test_bad_manifest_never_calls_downloader(self):
        from buyer.vision.setup import run_setup
        (self.root / "manifest.json").write_text("broken")
        with patch("scripts.download_vision_model.download") as network:
            result = run_setup(replace(self.config, visual=self.root), [])
        self.assertEqual(result["code"], "invalid_manifest")
        network.assert_not_called()

    def test_download_replaces_corrupt_weights_and_preserves_valid_file(self):
        raw = b"known weights fixture"
        digest = hashlib.sha256(raw).hexdigest()
        progress = Mock()
        self.config.model.write_bytes(b"bad")
        with patch.multiple("buyer.vision.model", WEIGHT_SHA256=digest, WEIGHT_BYTES=len(raw)), \
             patch.multiple("scripts.download_vision_model", WEIGHT_SHA256=digest, WEIGHT_BYTES=len(raw)), \
             patch("scripts.download_vision_model.urlopen", return_value=BytesIO(raw)) as network:
            self.assertTrue(download(self.config.model, progress))
            self.assertEqual(self.config.model.read_bytes(), raw)
            self.assertFalse(download(self.config.model))
            network.assert_called_once()
            progress.assert_any_call("downloading", downloaded_bytes=len(raw), total_bytes=len(raw))
            self.assertFalse(self.config.model.with_suffix(".download").exists())

    def test_network_failures_have_bounded_attempts_and_remove_partial_files(self):
        for code, error, attempts in (("download_denied", HTTPError("fixture", 403, "Forbidden", {}, None), 1),
                                      ("download_failed", URLError("offline"), 2)):
            with self.subTest(code=code), patch("scripts.download_vision_model.urlopen", side_effect=error) as network, \
                 patch("scripts.download_vision_model.time.sleep"):
                with self.assertRaisesRegex(ValueError, code):
                    download(self.config.model)
                self.assertEqual(network.call_count, attempts)
                self.assertFalse(self.config.model.exists())
                self.assertFalse(self.config.model.with_suffix(".download").exists())

    def test_missing_dependency_returns_install_command(self):
        from buyer.vision.setup import run_setup
        with patch("buyer.vision.setup.prepare", side_effect=ImportError):
            result = run_setup(self.config, [])
        self.assertEqual(result["status"], "dependencies_required")
        self.assertIn("pip install", result["setup_hint"])
