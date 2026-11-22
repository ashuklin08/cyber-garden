"""Optional runtime isolated in a bounded, lazy subprocess, without Gemma."""
import json
import multiprocessing
import os
import time
from threading import Event, Thread

from buyer.vision.config import Config, DEMO_IMAGES, UNAVAILABLE, safe_path


def unavailable(code):
    actions = {
        "missing_dependencies": "Установите дополнительные зависимости: requirements-vision.txt.",
        "missing_model": "Скачайте веса: scripts/download_vision_model.py.",
        "model_integrity": "Проверка весов не пройдена. Повторите скачивание модели.",
        "missing_index": "Подготовьте индекс: scripts/prepare_visual_demo.py.",
        "stale_index": "Изменились данные. Повторите подготовку визуального индекса.",
        "stale_images": "Изменились изображения. Повторите подготовку визуального индекса.",
        "missing_calibration": "Подготовьте калибровку: scripts/calibrate_visual_search.py.",
        "stale_calibration": "Индекс обновился. Повторите калибровку визуального поиска.",
        "no_current_ids": "Для объявлений текущего каталога пока нет демонстрационных фото.",
        "invalid_manifest": "Повреждён manifest демонстрационных изображений.",
        "timeout": "Обработка заняла слишком много времени. Попробуйте ещё раз.",
    }
    return dict(status="visual_unavailable", code=code, message=UNAVAILABLE,
                setup_hint=actions.get(code, "Проверьте подготовку визуального модуля по README."))


def demo_image(config, name):
    relative, _ = DEMO_IMAGES[name]
    path = safe_path(config.visual, relative)
    if path.stat().st_size > 5 * 1024 * 1024:
        raise ValueError("invalid_demo_image")
    return path.read_bytes()


def read_calibration(config, metadata, *, reference=False):
    path = config.cache / "calibration.json"
    if reference or not path.is_file():
        path = config.visual / "calibration.json"
    if not path.is_file():
        raise FileNotFoundError("missing_calibration")
    if path.stat().st_size > 128_000:
        raise ValueError("stale_calibration")
    calibration = json.loads(path.read_text(encoding="utf-8"))
    if path.parent == config.visual and calibration.get("images") and calibration.get("fingerprint") == metadata["fingerprint"]:
        # The bundled report is a reference for strict subsets. A fresh full
        # installation writes its own calibration to the runtime cache.
        raise FileNotFoundError("missing_calibration")
    threshold = calibration.get("threshold")
    subset = (calibration.get("manifest_sha256") == metadata.get("manifest_sha256")
              and calibration.get("weights_sha256") == metadata.get("weights_sha256")
              and calibration.get("preprocessing") == metadata.get("preprocessing")
              and bool(metadata.get("images")) and all(row in calibration.get("images", []) for row in metadata["images"]))
    if ((calibration.get("fingerprint") != metadata["fingerprint"] and not subset) or type(threshold) not in (float, int)
            or not -1 <= threshold <= 1 or not calibration.get("method")):
        raise ValueError("stale_calibration")
    if calibration.get("evaluation_sha256"):
        from buyer.vision.index import manifest_sha256
        evaluation = config.visual / "evaluation.json"
        if not evaluation.is_file() or evaluation.stat().st_size > 128_000:
            raise ValueError("stale_calibration")
        if manifest_sha256(json.loads(evaluation.read_bytes())) != calibration["evaluation_sha256"]:
            raise ValueError("stale_calibration")
        from buyer.vision.index import sha
        for row in calibration.get("calibration_images", []):
            sample = safe_path(config.visual, row["path"])
            if not sample.is_file() or sample.stat().st_size > 5 * 1024 * 1024 or sha(sample.read_bytes()) != row["sha256"]:
                raise ValueError("stale_calibration")
    return threshold


def ready_state(config, catalog, rows):
    examples = [dict(name=name, label=label, url="/visual-demo/" + name)
                for name, (relative, label) in DEMO_IMAGES.items()
                if safe_path(config.visual, relative).is_file()]
    return dict(status="ready", message="Визуальный поиск готов. Выберите фото или демо-пример.",
                coverage=dict(indexed=len(rows), total=len(catalog)), examples=examples)


class Engine:
    def __init__(self, config=Config()):
        self.config = config
        self.model = None

    def status(self, catalog):
        # Runs in the optional worker too: a broken native dependency must never
        # load into the HTTP-server process. No inference session is created.
        import importlib.util
        if any(importlib.util.find_spec(name) is None for name in ("numpy", "PIL", "onnxruntime")):
            raise ImportError("Optional vision packages missing")
        from buyer.vision.index import load
        from buyer.vision.model import verify_weights
        _, rows, metadata = load(self.config, catalog)
        read_calibration(self.config, metadata)
        verify_weights(self.config.model)
        return ready_state(self.config, catalog, rows)

    def search(self, raw, catalog):
        from buyer.vision.image_validation import decode_image, InvalidImage
        from buyer.vision.index import load, catalog_sha
        from buyer.contracts import CATEGORIES, CITIES
        started = time.perf_counter()
        image = decode_image(raw)
        # IDs are checked before inference or loading weights. A swapped catalog
        # cannot receive old listings, and prices are never copied from the index.
        vectors, rows, metadata = load(self.config, catalog)
        threshold = read_calibration(self.config, metadata)
        if self.model is None:
            from buyer.vision.model import VisionModel
            self.model = VisionModel(self.config.model)
        embedded = time.perf_counter()
        query = self.model.embed(image)
        embed_ms = (time.perf_counter() - embedded) * 1000
        searched = time.perf_counter()
        scores = vectors @ query
        ranked = sorted(range(len(rows)), key=lambda i: (-float(scores[i]), rows[i]["listing_id"]))
        ranked = [i for i in ranked if scores[i] >= threshold][:5]
        cosine_ms = (time.perf_counter() - searched) * 1000
        current = {item["id"]: item for item in catalog}
        results = []
        for i in ranked:
            row = rows[i]
            item = current[row["listing_id"]]
            results.append(dict(rank=len(results) + 1, listing_id=item["id"], title=item["title"],
                category=CATEGORIES[item["category"]], city=CITIES[item["city_id"]], price_rub=item["price_rub"],
                image_path=row["path"], synthetic=row["synthetic"]))
        return dict(status="ok" if results else "visual_no_match", results=results,
                    message="Демонстрационные изображения, а не фотографии продавцов." if results else
                    "Не удалось найти достаточно похожие товары. Попробуйте другое изображение или воспользуйтесь текстовым поиском.",
                    coverage=dict(indexed=len(rows), total=len(catalog)),
                    catalog_changed=catalog_sha(catalog) != metadata["catalog_sha256"],
                    timings=dict(model_load_ms=self.model.load_ms, embedding_ms=embed_ms, cosine_ms=cosine_ms,
                                 total_ms=(time.perf_counter() - started) * 1000))


def limit_worker():
    """Hard memory limit on supported hosts; failure disables only this feature."""
    if os.name != "nt":
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (3 * 1024**3, 3 * 1024**3))
        return
    import ctypes as c
    from ctypes import wintypes as w
    class Basic(c.Structure):
        _fields_ = [("per_process", c.c_int64), ("per_job", c.c_int64), ("flags", w.DWORD),
                    ("min_ws", c.c_size_t), ("max_ws", c.c_size_t), ("active", w.DWORD),
                    ("affinity", c.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]
    class Extended(c.Structure):
        _fields_ = [("basic", Basic), ("io", c.c_uint64 * 6), ("process_memory", c.c_size_t),
                    ("job_memory", c.c_size_t), ("peak_process", c.c_size_t), ("peak_job", c.c_size_t)]
    kernel = c.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.restype = w.HANDLE
    kernel.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    kernel.GetCurrentProcess.restype = w.HANDLE
    job = kernel.CreateJobObjectW(None, None)
    info = Extended()
    info.basic.flags = 0x100  # JOB_OBJECT_LIMIT_PROCESS_MEMORY, 1 GiB private commit
    info.process_memory = 1024**3
    if not job or not kernel.SetInformationJobObject(job, 9, c.byref(info), c.sizeof(info)) or not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
        raise OSError("worker_memory_limit_unavailable")
    # Keep the handle alive for the lifetime of the worker.
    globals()["_memory_job"] = job


def worker(connection, config):
    try:
        os.environ["OPENBLAS_NUM_THREADS"] = "1"
        os.environ["OMP_NUM_THREADS"] = "2"
        limit_worker()
        engine = Engine(config)
        while True:
            request = connection.recv()
            if request is None:
                break
            try:
                result = engine.status(request[1]) if request[0] == "status" else engine.search(*request)
            except ImportError:
                result = unavailable("missing_dependencies")
            except Exception as error:
                from buyer.vision.image_validation import InvalidImage
                if isinstance(error, InvalidImage):
                    result = dict(status="invalid_image", message=str(error))
                else:
                    codes = {"missing_model", "missing_index", "missing_calibration", "stale_calibration", "missing_manifest", "no_current_ids", "stale_index", "stale_images",
                             "model_integrity", "invalid_manifest", "invalid_manifest_entry", "invalid_vectors",
                             "missing_or_large_product_image", "duplicate_product_image"}
                    code = str(error) if str(error) in codes else "invalid_visual_assets_or_model"
                    result = unavailable(code)
            connection.send(result)
    except (EOFError, BrokenPipeError):
        pass
    except Exception:
        try:
            connection.send(unavailable("worker_unavailable"))
        except (OSError, EOFError):
            pass
    finally:
        connection.close()


class VisualService:
    def __init__(self, config=Config()):
        self.config = config
        self.process = self.connection = None

    def search(self, raw, catalog):
        return self._request((raw, catalog))

    def status(self, catalog):
        return self._request(("status", catalog), timeout=min(self.config.timeout, 8))

    def _request(self, request, timeout=None):
        started = time.perf_counter()
        deadline = started + (self.config.timeout if timeout is None else timeout)
        try:
            if self.process is None:
                context = multiprocessing.get_context("spawn")
                parent, child = context.Pipe()
                self.process = context.Process(target=worker, args=(child, self.config), daemon=True)
                self.connection = parent
                self.process.start()
                child.close()
            # A pipe write can block too (e.g. a worker dying before reading a
            # large upload). The deadline covers IPC, not only model inference.
            connection = self.connection
            sent, errors = Event(), []
            def send_request():
                try:
                    connection.send(request)
                except Exception as error:
                    errors.append(error)
                finally:
                    sent.set()
            sender = Thread(target=send_request, daemon=True)
            sender.start()
            if not sent.wait(max(0, deadline - time.perf_counter())):
                self.close()
                sender.join(.2)
                return unavailable("timeout")
            if errors:
                raise errors[0]
            if not self.connection.poll(max(0, deadline - time.perf_counter())):
                self.close()
                return unavailable("timeout")
            result = self.connection.recv()
            result["api_ms"] = (time.perf_counter() - started) * 1000
            return result
        except Exception:
            self.close()
            return unavailable("worker_failure")

    def close(self):
        process, connection = self.process, self.connection
        self.process = self.connection = None
        if process:
            try:
                # Do not send a shutdown message through a possibly blocked
                # pipe. The worker holds only read-only model/index resources.
                if process.is_alive():
                    process.terminate()
                process.join(1)
            except (OSError, ValueError, AssertionError):
                pass
        if connection:
            connection.close()
