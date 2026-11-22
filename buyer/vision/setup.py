"""On-demand preparation supervised separately from the request and inference paths."""
from contextlib import contextmanager
import json
import os
import subprocess
import sys
import tempfile
import time
from threading import Lock, Timer
import re

from buyer.vision.config import Config, ROOT

PHASES = {
    "not_started": "Визуальный поиск ещё не подготовлен.",
    "checking": "Проверяем визуальный поиск…",
    "preparing": "Ожидаем завершения подготовки на этом компьютере…",
    "downloading": "Скачиваем модель · 97 МБ",
    "verifying": "Проверяем целостность модели…",
    "building_index": "Создаём индекс изображений…",
    "calibrating": "Калибруем поиск…",
}
DEPENDENCIES = dict(status="dependencies_required", message="Установите зависимости визуального поиска.",
                    setup_hint=r".\.venv\Scripts\python.exe -m pip install -r requirements-vision.txt")


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False, mode="w", encoding="utf-8") as stream:
        temporary = path.parent / os.path.basename(stream.name)
        json.dump(value, stream, ensure_ascii=False, indent=2)
    try:
        for attempt in range(4):
            try:
                temporary.replace(path)
                break
            except PermissionError:
                if attempt == 3:
                    raise
                time.sleep(.05)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def installation_lock(cache, *, timeout=600):
    """Kernel lock, automatically released even on forced process exit."""
    cache.mkdir(parents=True, exist_ok=True)
    with (cache / "setup.lock").open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"\x00")
            stream.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("setup_timeout") from None
                time.sleep(.25)
        yield


def setup_failure(error):
    from buyer.vision.service import unavailable
    code = str(error)
    if isinstance(error, ImportError):
        return DEPENDENCIES.copy()
    if code in {"no_current_ids", "missing_manifest", "invalid_manifest", "invalid_manifest_entry",
                "missing_or_large_product_image", "duplicate_product_image", "invalid_product_path"}:
        return unavailable(code)
    messages = {
        "download_denied": "Сервер весов отклонил загрузку (HTTP 401 или 403).",
        "download_failed": "Не удалось скачать модель. Проверьте соединение и повторите попытку.",
        "model_integrity": "Загруженный файл не прошёл проверку размера или SHA-256.",
        "missing_model": "Локальные веса отсутствуют.",
        "calibration_overlap": "Размеченные примеры визуального поиска слишком похожи друг на друга для надёжной калибровки.",
        "not_enough_calibration_examples": "Для этого набора изображений не хватает примеров калибровки.",
        "setup_timeout": "Подготовка заняла слишком много времени.",
    }
    return dict(status="failed", code=code if code in messages else "setup_failed",
                message=messages.get(code, "Не удалось подготовить визуальный поиск. Попробуйте ещё раз."))


def prepare(config, catalog, *, allow_download=True, progress=lambda *_args, **_fields: None):
    from zipfile import BadZipFile
    from buyer.vision.index import build, load, manifest_entries
    from buyer.vision.model import verify_weights
    from buyer.vision.service import read_calibration, ready_state
    from scripts.calibrate_visual_search import calibrate
    from scripts.download_vision_model import download

    progress("checking")
    entries, _ = manifest_entries(config, catalog)
    if not entries:
        raise ValueError("no_current_ids")
    import numpy  # noqa: F401 — check native imports before attempting a download
    import PIL.Image  # noqa: F401
    import onnxruntime  # noqa: F401
    started = time.monotonic()
    if allow_download:
        downloaded = download(config.model, progress)
    else:
        progress("verifying")
        verify_weights(config.model)
        downloaded = False
    weights_ms = (time.monotonic() - started) * 1000
    built = calibrated = False
    build_ms = 0
    try:
        vectors, rows, metadata = load(config, catalog)
    except (OSError, ValueError, KeyError, BadZipFile):
        progress("building_index")
        _, timings = build(config, catalog, weights_verified=True)
        build_ms = timings["build_ms"]
        built = True
        vectors, rows, metadata = load(config, catalog)
    try:
        read_calibration(config, metadata)
    except (OSError, ValueError, KeyError, TypeError):
        try:
            read_calibration(config, metadata, reference=True)
            report = json.loads((config.visual / "calibration.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError, TypeError):
            progress("calibrating")
            report = calibrate(config, catalog, metadata=metadata, rows=rows, vectors=vectors)
            calibrated = True
        atomic_json(config.cache / "calibration.json", report)
    return ready_state(config, catalog, rows) | dict(
        downloaded=downloaded, index_built=built, calibrated=calibrated,
        weights_ms=round(weights_ms, 1), index_ms=round(build_ms, 1))


def run_setup(config, catalog, *, allow_download=True, job="manual"):
    if not re.fullmatch(r"(?:manual|[a-f0-9]{24})", job):
        raise ValueError("Invalid setup job")
    def write(status, **fields):
        atomic_json(config.cache / "setup-state.json",
                    dict(job=job, status=status, message=PHASES.get(status, ""), **fields))

    with installation_lock(config.cache):
        try:
            result = prepare(config, catalog, allow_download=allow_download, progress=write)
        except Exception as error:
            result = setup_failure(error)
        if job != "manual":
            atomic_json(config.cache / f"setup-{job}.json", result)
        atomic_json(config.cache / "setup-state.json", dict(job=job, **result))
        return result


class SetupManager:
    """Each HTTP server supervises at most one child. Polling only reads state."""
    def __init__(self, catalog_path, config=Config()):
        self.config = config
        self.catalog_path = catalog_path
        self.lock = Lock()
        self.child = None
        self.result = None
        self.watchdog = None
        self.state_file = config.cache / "setup-state.json"

    def status(self, *, retry=False):
        with self.lock:
            if self.child is not None:
                if self.child.poll() is not None:
                    self.watchdog.cancel()
                    self.child = None
                    result_path = self.config.cache / f"setup-{self.job}.json"
                    try:
                        self.result = self._read_state(result_path) or setup_failure(ValueError("setup_failed"))
                        result_path.unlink(missing_ok=True)
                    except OSError:
                        self.result = setup_failure(ValueError("setup_failed"))
                else:
                    state = self._read_state()
                    if state.get("status") in PHASES:
                        return self._public(state)
                    phase = "preparing" if state.get("job") != self.job else "checking"
                    return dict(status=phase, message=PHASES[phase])
            if retry:
                self.result = None
            if self.result is not None:
                return self._public(self.result)
            self.job = os.urandom(12).hex()
            try:
                self.child = subprocess.Popen([
                    sys.executable, str(ROOT / "scripts/prepare_visual_demo.py"), "--download",
                    "--catalog", str(self.catalog_path), "--model", str(self.config.model),
                    "--cache", str(self.config.cache), "--visual", str(self.config.visual), "--job", self.job,
                ], cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except OSError as error:
                self.result = setup_failure(error)
                return self.result
            self.watchdog = Timer(660, self._expire, args=(self.child,))
            self.watchdog.daemon = True
            self.watchdog.start()
            return dict(status="checking", message=PHASES["checking"])

    @staticmethod
    def _public(state):
        return {key: value for key, value in state.items() if key != "job"}

    def _read_state(self, path=None):
        try:
            state = json.loads((path or self.state_file).read_text(encoding="utf-8"))
            return state if isinstance(state, dict) else {}
        except (OSError, ValueError):
            return {}

    def close(self):
        with self.lock:
            if self.watchdog:
                self.watchdog.cancel()
            if self.child is not None and self.child.poll() is None:
                self.child.kill()
                self.child.wait(timeout=2)
            self.child = None

    def _expire(self, child):
        with self.lock:
            if self.child is child and child.poll() is None:
                child.kill()
                child.wait(timeout=2)
                self.child = None
                self.result = setup_failure(TimeoutError("setup_timeout"))
