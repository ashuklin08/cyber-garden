"""Optional runtime isolated in a bounded, lazy subprocess, without Gemma."""
import json
import multiprocessing
import os
import time

from buyer.vision.config import Config, UNAVAILABLE, safe_path


def unavailable(code):
    return dict(status="visual_unavailable", code=code, message=UNAVAILABLE)


class Engine:
    def __init__(self, config=Config()):
        self.config = config
        self.model = None

    def search(self, raw, catalog):
        from buyer.vision.image_validation import decode_image, InvalidImage
        from buyer.vision.index import load, catalog_sha
        from buyer.contracts import CATEGORIES, CITIES
        started = time.perf_counter()
        image = decode_image(raw)
        # IDs are checked before inference or loading weights. A swapped catalog
        # cannot receive old listings, and prices are never copied from the index.
        vectors, rows, metadata = load(self.config, catalog)
        calibration_path = self.config.visual / "calibration.json"
        if not calibration_path.is_file():
            return unavailable("missing_calibration")
        calibration = json.loads(calibration_path.read_text(encoding="utf-8"))
        threshold = calibration.get("threshold")
        if (calibration.get("fingerprint") != metadata["fingerprint"] or type(threshold) not in (float, int)
                or not -1 <= threshold <= 1 or not calibration.get("method")):
            return unavailable("stale_calibration")
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
                result = engine.search(*request)
            except ImportError:
                result = unavailable("missing_dependencies")
            except Exception as error:
                from buyer.vision.image_validation import InvalidImage
                if isinstance(error, InvalidImage):
                    result = dict(status="invalid_image", message=str(error))
                else:
                    codes = {"missing_model", "missing_index", "missing_manifest", "no_current_ids", "stale_index", "stale_images",
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
        started = time.perf_counter()
        try:
            if self.process is None:
                context = multiprocessing.get_context("spawn")
                parent, child = context.Pipe()
                self.process = context.Process(target=worker, args=(child, self.config), daemon=True)
                self.connection = parent
                self.process.start()
                child.close()
            self.connection.send((raw, catalog))
            if not self.connection.poll(self.config.timeout):
                self.close()
                return unavailable("timeout")
            result = self.connection.recv()
            result["api_ms"] = (time.perf_counter() - started) * 1000
            return result
        except Exception:
            self.close()
            return unavailable("worker_failure")

    def close(self):
        if self.process:
            try:
                if self.connection:
                    self.connection.send(None)
            except (OSError, EOFError):
                pass
            self.process.join(.2)
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(1)
        if self.connection:
            self.connection.close()
        self.process = self.connection = None
