"""Gateway-only latency on the first ten labelled dialogues, one call at a time."""
import json
import time
from buyer.model import ModelError, extract
from tests.test_conversation import CASES
from bench.load import quantiles

if __name__ == "__main__":
    times, errors = [], []
    for name, messages, _ in CASES[:10]:
        start = time.monotonic()
        try:
            _, trace = extract(messages, full_history=True)
            times.append(trace.elapsed_ms)
        except ModelError as exc:
            errors.append(str(exc))
        time.sleep(max(0, 4.2-(time.monotonic()-start)))
    print(json.dumps(dict(success_ms=times,latency_ms=quantiles(times),errors=errors),ensure_ascii=False))
