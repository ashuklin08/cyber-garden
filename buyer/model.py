"""Gemma chat completions transport. The catalog never enters this module."""

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from urllib.parse import urlsplit

from buyer.contracts import decode_json, read_requirements
from buyer.prompt import SYSTEM
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


def configuration():
    settings = {name: os.environ.get(name, "").strip() for name in SETTINGS}
    missing = [name for name in SETTINGS if not settings[name]]
    if missing:
        raise ModelError("Задайте переменные окружения " + ", ".join(missing) + ".")
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


def prepare_body(messages, model):
    conversation = [{"role": "system", "content": SYSTEM}]
    conversation.extend({"role": message["role"], "content": message["text"]} for message in messages)
    if sum(len(message["content"].encode("utf-8")) for message in conversation) > MAX_TEXT_BYTES:
        raise ModelError("История превышает лимит байтов шлюза. Сократите реплики.")
    body = {
        "model": model,
        "messages": conversation,
        "temperature": 0,
        "max_tokens": 512,
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
            with opener.open(request, timeout=timeout) as response:
                return response.read(128_001)
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


def _tokens(usage, key):
    value = usage.get(key) if isinstance(usage, dict) else None
    return value if type(value) is int and value >= 0 else None


def interpret(messages):
    settings = configuration()
    model = settings["LLM_MODEL"]
    request = urllib.request.Request(
        settings["LLM_BASE_URL"].rstrip("/") + "/chat/completions",
        data=prepare_body(messages, model),
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
        requirements = read_requirements(decode_json(choice["message"]["content"]))
        usage = envelope.get("usage", {})
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        raise ModelError("Gemma вернула неполный ответ или данные вне схемы условий.") from None
    return requirements, ModelTrace(model, _tokens(usage, "prompt_tokens"),
                                    _tokens(usage, "completion_tokens"),
                                    round((time.monotonic() - started) * 1000))
