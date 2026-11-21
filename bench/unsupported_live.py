"""Real official Gemma checks for unsupported categories and the A/B/C distinction."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
from types import SimpleNamespace

from jsonschema import Draft202012Validator

from buyer.contracts import CATEGORIES, ROOT, read_catalog, read_requirements
from buyer.conversation import ConversationManager, EMPTY
from buyer.demo import Handler
from buyer.model import extract
from tests.fixtures import CATALOG, product
from tests.live_observer import events, install
from tests.live_support import Pacer, denied, preflight, write_report


REFUSAL = "Такого каталога нет. Пожалуйста, выберите из имеющихся. Доступны: " + ", ".join(CATEGORIES.values()) + "."
BASE = "Стол не шире 100 см в Азове, бюджет 5000 рублей"


def dialog(*texts):
    return [{"role": "user" if index % 2 == 0 else "assistant", "text": text}
            for index, text in enumerate(texts)]


CASES = [
    (name, "A", dialog(text), {}, CATALOG, "clarify")
    for name, text in (("car", "Найди автомобиль"), ("fridge", "Хочу холодильник"),
                       ("bicycle", "Подбери велосипед"), ("sofa", "Нужен диван"))
] + [
    ("preserve_city_budget", "A", dialog(BASE, "Подобрал стол", "Хочу холодильник"),
     dict(city_id="azov", budget_rub=5000, width_cm=[["lte", 100]]), CATALOG, "clarify"),
    ("resume_table", "resume", dialog(BASE, "Подобрал стол", "Хочу холодильник", REFUSAL, "Тогда найди стол"),
     dict(city_id="azov", budget_rub=5000, categories=["table"], width_cm=[["lte", 100]]), CATALOG, "ok"),
    ("supported_category_absent", "B", dialog("Кресло в Таганроге, бюджет 5000 рублей"),
     dict(city_id="taganrog", budget_rub=5000, categories=["chair"]),
     [row for row in CATALOG if row["category"] != "chair"], "no_match"),
    ("supported_category_filtered", "C", dialog("Стол не шире 90 см в Таганроге, бюджет 5000 рублей"),
     dict(city_id="taganrog", budget_rub=5000, categories=["table"], width_cm=[["lte", 90]]),
     [product("wide", "table", 1000, 120)], "no_match"),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-prefix", default="unsupported-live")
    args = parser.parse_args()
    meta = preflight()
    prefix = ROOT / "bench/results" / args.output_prefix
    output, http = (Path(str(prefix) + suffix) for suffix in (".json", "-http.jsonl"))
    if output.exists() or http.exists():
        raise FileExistsError("Use a new report prefix")
    install(http)
    validator = Draft202012Validator(json.loads((ROOT / "schemas/response.schema.json").read_bytes()))
    pacer = Pacer()
    records = []
    for name, situation, messages, fields, rows, status in CASES:
        pacer.wait()
        before = len(events(http))
        record = dict(name=name, situation=situation, messages=messages, expected_status=status)
        interpreted = None

        def gemma(history, *, full_history):
            nonlocal interpreted
            interpreted, trace = extract(history, full_history=full_history)
            return interpreted, trace

        try:
            state, trace = ConversationManager().resolve(messages, gemma, require_model=True)
            handler = Handler.__new__(Handler)
            handler.server = SimpleNamespace(listings=read_catalog(rows), provider=None,
                                             titles={row["id"]: row["title"] for row in rows})
            handler._stages = None
            body = handler._buyer_result(state, trace)
            record.update(model_query=asdict(interpreted), requirements=asdict(state.requirements),
                          preferences=[asdict(pref) for pref in state.preferences], response=body["response"])
            validator.validate(body["response"])
            assert state.requirements == read_requirements(EMPTY | fields), record
            assert not state.preferences, record
            assert body["response"]["status"] == status, record
            if situation == "A":
                # Check real model recognition independently of the local guard.
                assert interpreted.requested_categories, "Gemma omitted requested product identity"
                assert any(item not in CATEGORIES for item in interpreted.requested_categories), interpreted
                assert not interpreted.requirements.categories, "Gemma substituted a supported product"
                assert body["response"] == {"status": "clarify", "questions": [
                    {"field": "categories", "text": REFUSAL}]}, record
                assert not body["items"], record
            elif situation == "resume":
                assert body["response"]["options"][0]["listing_ids"] == ["azTable"], record
                assert not state.blocked_categories, record
            else:
                message = body["response"]["message"]
                assert ("«Кресло» отсутствует в каталоге" if situation == "B" else
                        "нет подтверждённого соответствия условию") in message, record
                assert REFUSAL not in message and not body["items"], record
            observed = events(http)[before:]
            attempts = [event for event in observed if event["kind"] == "http_attempt"]
            assert attempts and all(event["url"] == meta["endpoint"] + "/chat/completions" for event in attempts)
            assert trace.model == meta["model"]
            record["passed"] = True
        except Exception as error:
            record.update(passed=False, error=str(error))
        observed = events(http)[before:]
        record["gateway_events"] = observed
        pacer.after(observed)
        records.append(record)
        write_report(output, meta | dict(planned=len(CASES), executed=len(records),
                     passed=sum(row["passed"] for row in records), cases=records))
        print(("PASS " if record["passed"] else "FAIL ") + name, flush=True)
        if denied(record.get("error", "")):
            break
    print(f"{sum(row['passed'] for row in records)}/{len(CASES)} real Gemma scenarios passed", flush=True)
    return int(len(records) != len(CASES) or not all(row["passed"] for row in records))


if __name__ == "__main__":
    raise SystemExit(main())
