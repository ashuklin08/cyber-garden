"""Local browser demo. The CLI and browser share the same interpretation and solver."""

import argparse
import json
import socket
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import BoundedSemaphore

from buyer.contracts import CATEGORIES, CITIES, ROOT, decode_json, read_catalog, read_messages
from buyer.lab import QUERY, compare
from buyer.model import ModelError, interpret
from buyer.selection import LABELS, actual_feature, describe_condition, select

UI = Path(__file__).with_name("ui")
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/workspace.webp": ("workspace.webp", "image/webp"),
}
CSP = "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"


class DemoServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port, catalog):
        self.catalog = decode_json(catalog.read_bytes())
        self.listings = read_catalog(self.catalog)
        self.titles = {item["id"]: item["title"] for item in self.catalog}
        self.gateway_slot = BoundedSemaphore(1)
        super().__init__(("127.0.0.1", port), Handler)


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def _send(self, status, raw, mime):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(raw)))
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
            self._json(HTTPStatus.OK, {"total": len(self.server.listings), "cities": list(CITIES.values())})
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

    def do_POST(self):
        if not self._local_host():
            self._json(HTTPStatus.FORBIDDEN, {"error": "Демо доступно через localhost."})
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

        if not self.server.gateway_slot.acquire(blocking=False):
            self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "Gemma уже обрабатывает запрос. Повторите чуть позже."})
            return
        try:
            requirements, trace = interpret(messages)
        except ModelError as error:
            self._json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
            return
        finally:
            self.server.gateway_slot.release()

        if self.path == "/api/lab":
            data = compare(requirements)
            data.update({"query": QUERY, "trace": asdict(trace)})
        else:
            data = self._buyer_result(requirements, trace)
        self._json(HTTPStatus.OK, data)

    def _buyer_result(self, requirements, trace):
        result = select(self.server.listings, requirements)
        items = {
            listing_id for option in result.response.get("options", [])
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
            "response": result.response,
            "trace": asdict(trace),
            "requirements": core_fields + conditions,
            "budget_rub": requirements.budget_rub,
            "eligible": [{"category": CATEGORIES[key], "count": count}
                         for key, count in result.eligible_counts.items()],
            "minimum_total_rub": result.minimum_total_rub,
            "items": {
                item.id: {"category": CATEGORIES[item.category], "category_id": item.category,
                          "title": self.server.titles[item.id], "price_rub": item.price_rub,
                          "feature": actual_feature(item), "city": CITIES[item.city_id]}
                for item in self.server.listings if item.id in items
            },
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=ROOT / "data/catalog.json")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    with DemoServer(args.port, args.catalog) as server:
        print(f"http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
