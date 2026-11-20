"""Entrypoint for the organizers' stdin/stdout contract."""

import argparse
import json
import signal
import sys
from pathlib import Path

from buyer.contracts import decode_json, read_catalog, read_messages
from buyer.model import ModelError, interpret
from buyer.selection import select


def _timeout(_signal, _frame):
    raise ModelError("Превышено время выполнения запроса.")


def main():
    parser = argparse.ArgumentParser(description="Подбор комплекта по контракту ЮФУ 1.1.")
    parser.add_argument("--catalog", required=True, type=Path, help="Путь к JSON-каталогу")
    args = parser.parse_args()

    try:
        request = read_messages(decode_json(sys.stdin.buffer.read()))
        listings = read_catalog(decode_json(args.catalog.read_bytes()))
        requirements, _ = interpret(request)
        response = select(listings, requirements).response
    except ModelError as error:
        print(f"run: {error}", file=sys.stderr)
        return 2
    except (ValueError, OSError, RecursionError):
        print("run: не удалось прочитать запрос или каталог по контракту 1.1.", file=sys.stderr)
        return 2

    sys.stdout.buffer.write((json.dumps(response, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(115)
    raise SystemExit(main())
