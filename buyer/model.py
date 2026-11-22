"""Gemma chat completions transport. The catalog never enters this module."""

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from urllib.parse import urlsplit

from buyer.anchors import categories_from_dialog
from buyer.contracts import CATEGORIES, CITIES, ROOT, Requirements, decode_json, read_requirements
from buyer.prompt import FAST_SYSTEM, SYSTEM
from buyer.query_parser import Preference, location_mentions
from buyer.preferences import grounded
from llm.client import NoRedirect

MAX_TEXT_BYTES = 32_768
MAX_BODY_BYTES = 65_536
RETRYABLE = {429, 502, 503}
SETTINGS = ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY", "LLM_USER_AGENT")


class ModelError(Exception):
    pass


@dataclass(frozen=True)
class ModelTrace:
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None
    elapsed_ms: int
    category_source: str = "Gemma"


@dataclass(frozen=True)
class ModelQuery:
    requirements: Requirements
    preferences: tuple[Preference, ...]
    intent: str | None = None
    requested_categories: tuple[str, ...] | None = None
    requested_location: dict | None = None


def configuration():
    settings = {name: os.environ.get(name, "").strip() for name in SETTINGS}
    if not all(settings.values()):
        local = {}
        try:
            lines = (ROOT / ".env").read_text(encoding="utf-8-sig").splitlines()
        except FileNotFoundError:
            lines = []
        except (OSError, UnicodeError):
            raise ModelError("Не удалось прочитать .env в корне проекта. Сохраните файл в UTF-8.") from None
        for line in lines:
            name, sep, value = line.partition("=")
            if sep and name.strip() in SETTINGS:
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                local[name.strip()] = value
        # Keep a file credential bound to its configured gateway.
        if settings["LLM_BASE_URL"] and settings["LLM_BASE_URL"].rstrip("/") != local.get("LLM_BASE_URL", "").rstrip("/"):
            local.pop("LLM_API_KEY", None)
        settings = {name: value or local.get(name, "") for name, value in settings.items()}
    missing = [name for name in SETTINGS if not settings[name]]
    if missing:
        raise ModelError("Заполните .env в корне проекта или задайте переменные окружения: " + ", ".join(missing) + ".")
    try:
        base = urlsplit(settings["LLM_BASE_URL"])
        _ = base.port
        local = base.hostname in ("localhost", "127.0.0.1", "::1")
        if (not base.hostname or base.scheme not in (("https", "http") if local else ("https",))
                or base.username or base.password or base.query or base.fragment
                or any(ch.isspace() for ch in settings["LLM_BASE_URL"])):
            raise ValueError
    except ValueError:
        raise ModelError("Для LLM_BASE_URL требуется HTTPS или локальный HTTP.") from None
    if any(not value.isascii() or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)
           for value in (settings["LLM_USER_AGENT"], settings["LLM_API_KEY"])):
        raise ModelError("Проверьте формат LLM_USER_AGENT и LLM_API_KEY.")
    return settings


def prepare_body(messages, model, *, full_history=False, use_anchors=True):
    anchor = categories_from_dialog(messages)
    system = SYSTEM if full_history else FAST_SYSTEM
    if use_anchors and anchor.categories is not None:
        system += "\nПроверенные словоформы категорий: " + ",".join(anchor.categories) + "."
    conversation = [{"role": "system", "content": system}]
    first_message = 0 if full_history else max(0, len(messages) - 2)
    conversation.extend(
        {"role": message["role"], "content": message["text"]}
        for index, message in enumerate(messages)
        if index >= first_message
    )
    if sum(len(message["content"].encode("utf-8")) for message in conversation) > MAX_TEXT_BYTES:
        raise ModelError("История превышает лимит байтов шлюза. Сократите реплики.")
    body = {
        "model": model,
        "messages": conversation,
        "temperature": 0,
        "max_tokens": 384 if full_history else 256,
        "response_format": {"type": "json_object"},
        "stream": False,
        "n": 1,
    }
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_BODY_BYTES:
        raise ModelError("HTTP-запрос превышает лимит байтов шлюза.")
    return raw


def _retry_delay(error):
    header = error.headers.get("Retry-After", "").strip()
    if header:
        if re.fullmatch(r"[0-9]{1,5}", header):
            return max(1, int(header))
        try:
            retry_at = parsedate_to_datetime(header)
            return max(1, retry_at.timestamp() - time.time()) if retry_at.tzinfo else None
        except (TypeError, ValueError, OSError, OverflowError):
            return None
    return 2 if error.code in {502, 503} else None


def _gateway(request):
    deadline = time.monotonic() + 106
    opener = urllib.request.build_opener(NoRedirect)
    for attempt in range(2):
        try:
            timeout = min(89, deadline - time.monotonic())
            if timeout <= 0:
                raise ModelError("Истекло общее время ожидания шлюза Gemma.")
            with opener.open(request, timeout=timeout) as response:
                return _read_body(response, deadline)
        except urllib.error.HTTPError as error:
            delay = _retry_delay(error)
            if (attempt == 0 and error.code in RETRYABLE and delay is not None
                    and delay <= 12 and deadline - time.monotonic() > delay + 15):
                error.close()
                time.sleep(delay)
                continue
            status = error.code
            error.close()
            if status in {401, 403}:
                raise ModelError(f"Шлюз Gemma вернул HTTP {status}. Проверьте ключ, User-Agent и срок доступа.") from None
            raise ModelError(f"Шлюз Gemma вернул HTTP {status}.") from None
        except (urllib.error.URLError, OSError, HTTPException):
            raise ModelError("Соединение со шлюзом Gemma не установлено или истекло время ожидания.") from None


def _read_body(response, deadline):
    """Bound total body time, including servers that continually drip bytes.

    read1 returns after one buffered/socket read; a single read(size) can wait
    through arbitrarily many successful short reads. Refresh the socket timeout
    to the remaining shared deadline before each operation.
    """
    chunks = []
    size = 0
    while size < 128_001:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ModelError("Истекло общее время ожидания шлюза Gemma.")
        if response.isclosed():
            break
        response.fp.raw._sock.settimeout(min(89, remaining))
        chunk = response.read1(min(16_384, 128_001 - size))
        if not chunk:
            if time.monotonic() >= deadline:
                raise ModelError("Истекло общее время ожидания шлюза Gemma.")
            break
        chunks.append(chunk)
        size += len(chunk)
    if time.monotonic() >= deadline:
        raise ModelError("Истекло общее время ожидания шлюза Gemma.")
    return b"".join(chunks)


def _tokens(usage, key):
    value = usage.get(key) if isinstance(usage, dict) else None
    return value if type(value) is int and value >= 0 else None


def extract(messages, *, full_history=False, use_anchors=True):
    settings = configuration()
    model = settings["LLM_MODEL"]
    request = urllib.request.Request(
        settings["LLM_BASE_URL"].rstrip("/") + "/chat/completions",
        data=prepare_body(messages, model, full_history=full_history, use_anchors=use_anchors),
        headers={
            "Authorization": "Bearer " + settings["LLM_API_KEY"],
            "Content-Type": "application/json",
            "User-Agent": settings["LLM_USER_AGENT"],
        },
    )
    started = time.monotonic()
    raw = _gateway(request)
    try:
        if len(raw) > 128_000:
            raise ValueError("Oversized gateway response")
        envelope = decode_json(raw)
        choice = envelope["choices"][0]
        if choice.get("finish_reason") not in (None, "stop"):
            raise ValueError("Incomplete generation")
        extracted = decode_json(choice["message"]["content"])
        soft = extracted.pop("preferences", [])
        intent = extracted.pop("intent", None)
        location = extracted.pop("requested_location", None)
        if location is not None:
            if (not isinstance(location, dict) or set(location) != {"name", "type", "city_id"}
                    or location["type"] not in {"city", "uncertain", "other", "clear"}
                    or (location["city_id"] is not None and not isinstance(location["city_id"], str))
                    or (location["name"] is not None and (not isinstance(location["name"], str)
                        or not 0 < len(location["name"]) <= 120))):
                raise ValueError("Invalid location metadata")
            # Names are literal buyer evidence, not model-created labels.
            if location["type"] == "city" and (not location["name"] or
                    location["name"].casefold() not in messages[-1]["text"].casefold()):
                location = dict(name=None, type="uncertain", city_id=None)
            elif location["type"] == "city":
                source = re.search(re.escape(location["name"]), messages[-1]["text"], re.I)
                location = dict(location, name=source[0])
        raw_city = extracted.get("city_id")
        if isinstance(raw_city, str) and raw_city not in CITIES:
            if not 0 < len(raw_city) <= 120:
                raise ValueError("Invalid city ID")
            if location is None or location["type"] != "city":
                mentions = list(location_mentions(messages[-1]["text"]))
                location = (dict(name=mentions[-1]["name"], type="city", city_id=None)
                            if mentions else None)
            extracted["city_id"] = None
        requested = extracted.pop("requested_categories", None)
        if requested is not None:
            if (not isinstance(requested, list) or len(requested) > 12
                    or any(not isinstance(item, str) or not 0 < len(item.strip()) <= 120
                           for item in requested)):
                raise ValueError("Invalid requested categories")
            requested = tuple(dict.fromkeys(item.strip() for item in requested))
        # Unknown string IDs are a category refusal, not a transport failure or
        # permission to substitute another product. Other malformed fields still fail.
        raw_categories = extracted.get("categories")
        if isinstance(raw_categories, list) and all(isinstance(item, str) for item in raw_categories):
            unknown = [item for item in raw_categories if item not in CATEGORIES]
            if unknown:
                if len(raw_categories) > 12 or any(not 0 < len(item.strip()) <= 120 for item in unknown):
                    raise ValueError("Invalid category IDs")
                requested = tuple(dict.fromkeys((*(requested or ()), *unknown)))
                extracted["categories"] = [item for item in raw_categories if item in CATEGORIES]
        if intent not in (None, "shopping", "off_topic", "ambiguous", "reset"):
            raise ValueError("Unknown turn intent")
        for field in ("width_cm", "color_temperature_k"):
            value = extracted.get(field)
            if (isinstance(value, list) and len(value) == 2
                    and isinstance(value[0], str) and type(value[1]) in (int, Decimal)):
                extracted[field] = [value]
        requirements = read_requirements(extracted)
        preferences = _preferences(soft, messages)
        usage = envelope.get("usage", {})
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        raise ModelError("Gemma вернула неполный ответ или данные вне схемы условий.") from None
    return ModelQuery(requirements, preferences, intent, requested, location), ModelTrace(
        model, _tokens(usage, "prompt_tokens"), _tokens(usage, "completion_tokens"),
        round((time.monotonic() - started) * 1000)
    )


def _preferences(raw, messages):
    if not isinstance(raw, list) or len(raw) > 12:
        return ()
    return grounded(tuple(Preference(item["category"], item["text"].strip()) for item in raw
                          if isinstance(item, dict) and item.get("category") in CATEGORIES
                          and isinstance(item.get("text"), str) and 0 < len(item["text"].strip()) <= 120), messages)
