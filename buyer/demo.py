"""Local browser demo. The CLI and browser share the same interpretation and solver."""

import argparse
import errno
import json
import socket
import sys
import time
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import BoundedSemaphore, Lock

from buyer.contracts import CATEGORIES, CITIES, ROOT, decode_json, read_catalog, read_messages
from buyer.conversation import ConversationManager
from buyer.indexed import SearchError, SearchProvider
from buyer.lab import QUERY, compare
from buyer.model import ModelError, ModelTrace, extract
from buyer.selection import LABELS, Selection, actual_feature, describe_condition, select

UI = Path(__file__).with_name("ui")
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/workspace.webp": ("workspace.webp", "image/webp"),
}
CSP = "default-src 'self'; img-src 'self' blob:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"


class DemoServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, port, catalog, provider=None):
        self.provider = provider
        self.catalog_path = catalog.resolve()
        self.catalog = [] if provider else decode_json(catalog.read_bytes())
        self.listings = read_catalog(self.catalog)
        self.titles = {item["id"]: item["title"] for item in self.catalog}
        self.gateway_slot = BoundedSemaphore(1)
        self.visual_slot = BoundedSemaphore(1)
        self.visual_service = None
        self.visual_setup = None
        self.visual_setup_lock = Lock()
        super().__init__(("127.0.0.1", port), Handler)

    def server_close(self):
        if self.visual_setup is not None:
            self.visual_setup.close()
        if self.visual_service is not None:
            self.visual_service.close()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def _send(self, status, raw, mime):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(raw)))
        stages = getattr(self, "_stages", None)
        if stages is not None:
            stages["total_ms"] = (time.perf_counter() - self._request_started) * 1000
            measured = {key: round(value, 3) for key, value in stages.items()}
            self.send_header("Server-Timing", ", ".join(
                key.removesuffix("_ms") + ";dur=" + str(value) for key, value in measured.items()))
            # Deliberately exclude request text, headers, URLs and credentials.
            sys.stderr.write(json.dumps({"event": "buyer_stage_latency", "status": int(status), **measured}) + "\n")
            sys.stderr.flush()
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _json(self, status, data):
        self._send(status, json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _local_host(self):
        port = self.server.server_port
        return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def do_GET(self):
        asset = ASSETS.get(self.path)
        if not self._local_host():
            self._json(HTTPStatus.FORBIDDEN, {"error": "Демо доступно через localhost."})
        elif self.path == "/api/catalog":
            try:
                provider = self.server.provider
                total = provider.cluster.call(provider.index + "/_count")["count"] if provider else len(self.server.listings)
                self._json(HTTPStatus.OK, {"total": total, "cities": list(CITIES.values()),
                                           "photos": self._catalog_photos()})
            except SearchError:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Каталог временно недоступен."})
        elif self.path.startswith("/visual-images/"):
            self._visual_image()
        elif self.path in ("/api/visual-status", "/api/visual-status?retry=1"):
            self._visual_status()
        elif self.path.startswith("/visual-demo/"):
            self._visual_demo()
        elif asset and (UI / asset[0]).is_file():
            self._send(HTTPStatus.OK, (UI / asset[0]).read_bytes(), asset[1])
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Страница не найдена."})

    def _request(self):
        if self.headers.get_content_type() != "application/json":
            raise ValueError
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 < size <= 65_536:
            raise ValueError
        return decode_json(self.rfile.read(size))

    def _catalog_photos(self):
        if self.server.provider:
            return {}
        try:
            from buyer.vision.config import Config
            from buyer.vision.index import manifest_entries
            rows, _ = manifest_entries(Config(), self.server.catalog)
            return {row["listing_id"]: {"path": row["path"], "synthetic": row["synthetic"]}
                    for row in rows}
        except Exception:
            return {}

    def _visual_image(self):
        try:
            from buyer.vision.config import Config, safe_path
            from buyer.vision.index import manifest_entries
            config = Config()
            relative = self.path.removeprefix("/visual-images/")
            rows, _ = manifest_entries(config, self.server.catalog)
            if relative not in {row["path"] for row in rows}:
                raise ValueError
            path = safe_path(config.visual, relative)
            mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}[path.suffix.lower()]
            self._send(HTTPStatus.OK, path.read_bytes(), mime)
        except Exception:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Изображение недоступно."})

    def _visual_status(self):
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self._json(HTTPStatus.FORBIDDEN, {"status": "visual_unavailable", "message": "Откройте страницу SmartSet."})
            return
        try:
            from buyer.vision.setup import SetupManager
            if self.server.provider:
                raise ValueError("Visual search requires a local JSON catalog")
            with self.server.visual_setup_lock:
                if self.server.visual_setup is None:
                    self.server.visual_setup = SetupManager(self.server.catalog_path)
            result = self.server.visual_setup.status(retry=self.path.endswith("?retry=1"))
        except Exception:
            result = {"status": "visual_unavailable", "code": "missing_module", "message":
                "Визуальный поиск недоступен. Текстовый и голосовой поиск продолжают работать."}
        self._json(HTTPStatus.OK, result)

    def _visual_demo(self):
        try:
            from buyer.vision.config import Config
            from buyer.vision.service import demo_image
            raw = demo_image(Config(), self.path.removeprefix("/visual-demo/"))
            self._send(HTTPStatus.OK, raw, "image/png")
        except Exception:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Демо-пример недоступен."})

    def _visual_request(self):
        # All optional imports live inside this route. Even deleting buyer/vision
        # leaves startup, catalog, buy, lab and the official CLI untouched.
        unavailable = {"status": "visual_unavailable", "message":
            "Визуальный поиск пока недоступен для текущего каталога. Вы можете воспользоваться обычным поиском."}
        if not self.server.visual_slot.acquire(blocking=False):
            self.close_connection = True
            self._json(HTTPStatus.TOO_MANY_REQUESTS, {"status": "visual_busy", "message": "Визуальный поиск занят. Попробуйте чуть позже."})
            return
        try:
            from email.parser import BytesParser
            from email.policy import default
            origin = self.headers.get("Origin")
            if origin and origin not in (f"http://localhost:{self.server.server_port}", f"http://127.0.0.1:{self.server.server_port}"):
                self.close_connection = True
                self._json(HTTPStatus.FORBIDDEN, {"status": "invalid_image", "message": "Откройте локальную страницу проекта."})
                return
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 5 * 1024 * 1024 + 65536:
                self.close_connection = True
                self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"status": "invalid_image", "message": "Максимальный размер фото — 5 МБ."})
                return
            if self.headers.get_content_type() != "multipart/form-data":
                raise ValueError
            boundary = self.headers.get_boundary()
            if not boundary or len(boundary) > 70:
                raise ValueError
            deadline = time.monotonic() + 10
            chunks, remaining = [], size
            while remaining:
                timeout = deadline - time.monotonic()
                if timeout <= 0:
                    raise ValueError
                self.connection.settimeout(timeout)
                chunk = self.rfile.read1(min(remaining, 65536))
                if not chunk:
                    raise ValueError
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            self.connection.settimeout(10)
            message = BytesParser(policy=default).parsebytes(
                b"Content-Type: " + self.headers["Content-Type"].encode("ascii") + b"\r\nMIME-Version: 1.0\r\n\r\n" + payload)
            parts = list(message.iter_parts())
            if message.defects or len(parts) != 1 or parts[0].is_multipart() or parts[0].get_content_disposition() != "form-data" or parts[0].get_param("name", header="content-disposition") != "image":
                raise ValueError
            image = parts[0].get_payload(decode=True)
            if not image or len(image) > 5 * 1024 * 1024:
                raise ValueError
            try:
                from buyer.vision.service import VisualService
                if self.server.visual_setup is not None:
                    state = self.server.visual_setup.status()
                    if state.get("status") != "ready":
                        self._json(HTTPStatus.OK, state)
                        return
                if self.server.visual_service is None:
                    self.server.visual_service = VisualService()
                result = self.server.visual_service.search(image, self.server.catalog)
            except Exception:
                result = unavailable
            self._json(HTTPStatus.BAD_REQUEST if result.get("status") == "invalid_image" else HTTPStatus.OK, result)
        except (ValueError, UnicodeError, OSError):
            self.close_connection = True
            self._json(HTTPStatus.BAD_REQUEST, {"status": "invalid_image", "message": "Выберите один файл JPEG, PNG или WebP размером до 5 МБ."})
        except Exception:
            self._json(HTTPStatus.OK, unavailable)
        finally:
            self.server.visual_slot.release()

    def do_POST(self):
        self._request_started = time.perf_counter()
        self._stages = dict(gemma_ms=0.0, resolve_ms=0.0, search_ms=0.0, total_ms=0.0) if self.path == "/api/buy" else None
        if not self._local_host():
            self._json(HTTPStatus.FORBIDDEN, {"error": "Демо доступно через localhost."})
            return
        if self.path == "/api/visual-search":
            self._visual_request()
            return
        if self.path not in ("/api/buy", "/api/lab"):
            self._json(HTTPStatus.NOT_FOUND, {"error": "Страница не найдена."})
            return
        try:
            data = self._request()
            if self.path == "/api/lab":
                if data != {}:
                    raise ValueError
                messages = [{"role": "user", "text": QUERY}]
            else:
                messages = read_messages(data)
        except (ValueError, RecursionError, socket.timeout):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Проверьте формат и длину диалога."})
            return

        try:
            resolve_started = time.perf_counter()
            try:
                query, trace = ConversationManager().resolve(
                    messages, self._gemma, require_model=True
                )
            finally:
                if self._stages is not None:
                    self._stages["resolve_ms"] = (time.perf_counter() - resolve_started) * 1000
        except GatewayBusy:
            self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "Gemma уже обрабатывает запрос. Повторите чуть позже."})
            return
        except ModelError as error:
            self._json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
            return
        requirements = query.requirements
        if trace is None:
            trace = ModelTrace("Локальный разбор", None, None, 0, "локальный разбор")
        elif "categories" in query.confirmed:
            trace = ModelTrace(trace.model, trace.prompt_tokens, trace.completion_tokens,
                               trace.elapsed_ms, "локальный разбор")
        if self.path == "/api/lab":
            data = compare(requirements)
            data.update({"query": QUERY, "trace": asdict(trace)})
        else:
            try:
                data = self._buyer_result(query, trace)
            except (SearchError, ValueError):
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Не удалось получить полный результат поиска."})
                return
            data["preferences"] = [asdict(item) for item in query.preferences]
        self._json(HTTPStatus.OK, data)

    def _gemma(self, messages, *, full_history):
        if not self.server.gateway_slot.acquire(blocking=False):
            raise GatewayBusy
        started = time.perf_counter()
        try:
            return extract(messages, full_history=full_history)
        finally:
            if self._stages is not None:
                # One high-resolution clock for all nested Web stages. The
                # existing client trace uses monotonic and stays unchanged.
                self._stages["gemma_ms"] = (time.perf_counter() - started) * 1000
            self.server.gateway_slot.release()

    def _buyer_result(self, query, trace):
        requirements = query.requirements
        clarification = query.attribute_questions() if not query.questions() else ()
        listings, titles = self.server.listings, self.server.titles
        if query.decision_questions():
            clarification = ()
            result = Selection({"status": "clarify", "questions": list(query.decision_questions())}, {}, None)
        elif clarification:
            result = None
        else:
            search_started = time.perf_counter()
            try:
                if self.server.provider:
                    found = self.server.provider.search(requirements, query.preferences)
                    result, listings, titles = found.selection, found.listings, found.titles
                else:
                    result = select(listings, requirements, query.preferences)
            finally:
                if self._stages is not None:
                    self._stages["search_ms"] = (time.perf_counter() - search_started) * 1000
        items = {
            listing_id for option in (result.response.get("options", []) if result else [])
            for listing_id in option["listing_ids"]
        }
        core_fields = [
            {"label": "Город самовывоза", "value": CITIES.get(requirements.city_id)},
            {"label": "Бюджет", "value": requirements.budget_rub},
            {"label": "Состав", "value": ", ".join(CATEGORIES[key] for key in requirements.categories) or None},
        ]
        conditions = []
        for key in requirements.categories:
            condition = requirements.condition(key)
            if condition is not None and condition != ():
                conditions.append({"label": LABELS[key], "value": describe_condition(key, condition)})

        return {
            "response": result.response if result else None,
            "clarification": clarification,
            "trace": asdict(trace),
            "requirements": core_fields + conditions,
            "budget_rub": requirements.budget_rub,
            "eligible": [{"category": CATEGORIES[key], "count": count}
                         for key, count in result.eligible_counts.items()] if result else [],
            "minimum_total_rub": result.minimum_total_rub if result else None,
            "items": {
                item.id: {"category": CATEGORIES[item.category], "category_id": item.category,
                          "title": titles[item.id], "price_rub": item.price_rub,
                          "feature": actual_feature(item), "city": CITIES[item.city_id]}
                for item in listings if item.id in items
            },
        }


class GatewayBusy(Exception):
    pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=ROOT / "data/catalog.json")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--index", help="Имя готового индекса OpenSearch")
    parser.add_argument("--search-url", default="http://127.0.0.1:19200")
    parser.add_argument("--semantic-model", type=Path, help="Папка с ONNX E5 и tokenizer.json")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port должен быть от 0 до 65535")
    provider = SearchProvider(args.search_url, args.index) if args.index else None
    if args.semantic_model:
        if not provider:
            parser.error("--semantic-model требует --index")
        from buyer.semantic import SemanticRanker
        provider.semantic = SemanticRanker(args.semantic_model)

    # A previous demo process is often still running during a presentation.
    # Pick the next port instead of failing with an opaque bind error.
    server = None
    for port in range(args.port, min(args.port + 10, 65536)):
        try:
            server = DemoServer(port, args.catalog, provider)
            break
        except OSError as error:
            if error.errno != errno.EADDRINUSE and getattr(error, "winerror", None) != 10048:
                raise
    if server is None:
        parser.error(f"Порты {args.port}–{args.port + 9} заняты. Остановите старое демо или задайте --port.")
    with server:
        print(f"http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
