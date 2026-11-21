"""Real Gemma geography recognition and city-refusal dialogue regressions."""
import argparse
from dataclasses import asdict
import json

from buyer.contracts import CITIES, ROOT
from buyer.model import extract
from buyer.conversation import UNSUPPORTED_CITY_MESSAGE
from tests.live_support import Pacer, preflight, write_report, denied
from tests.test_conversation import dialog
from tests.test_unsupported_cities import UnsupportedCitiesTest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-prefix", default="unsupported-cities-live")
    parser.add_argument("--case", action="append", dest="selected")
    args = parser.parse_args()
    report = preflight() | dict(cases=[])
    output = ROOT / "bench/results" / (args.output_prefix + ".json")
    if output.exists():
        raise FileExistsError("Use a new output prefix")
    pacer = Pacer()
    checker = UnsupportedCitiesTest()
    checker.setUp()
    base = "Стол, кресло и лампа. Таганрог. До 15000 рублей"
    history = dialog(base, "Подобрал комплект", "Хочу забрать в Москве")
    cases = [("moscow_after_bundle", history, "Москве", "clarify", None)]
    cases += [(name, dialog(text), city, "clarify", None) for name, text, city in (
        ("lowercase_moscow", "самовывоз из москвы", "москвы"),
        ("lowercase_krasnodar", "Найди стол в краснодаре", "краснодаре"),
        ("piter", "Стол из питера", "питера"),
        ("novosibirsk", "Стол в Новосибирске", "Новосибирске"),
        ("saint_petersburg", "Доставка в Санкт-Петербург", "Санкт-Петербург"))]
    # These continuations use the actual backend refusal from the first case.
    first_refusal = UNSUPPORTED_CITY_MESSAGE + ", ".join(CITIES.values()) + ". Вы указали: «Москве»."
    executed = 0
    for name, messages, city_name, status, city_id in cases + [
        ("resume_azov", None, None, "ok", "azov"),
        ("replace_unknown", None, "Краснодаре", "clarify", None),
        ("clear_unknown", None, None, "clarify", None),
        ("rostov_alias", dialog("Стол в ростове, бюджет 5000 рублей"), None, "ok", "rostov-na-donu"),
        ("not_a_city", dialog("Стол в хорошем состоянии в Азове, бюджет 5000 рублей"), None, "ok", "azov")]:
        if args.selected and name not in args.selected:
            continue
        executed += 1
        if messages is None:
            text = {"resume_azov": "Тогда в Азове", "replace_unknown": "А в Краснодаре?",
                    "clear_unknown": "Нет, город неважен"}[name]
            messages = history + [{"role": "assistant", "text": first_refusal}, {"role": "user", "text": text}]
        pacer.wait()
        record = dict(name=name, messages=messages)
        try:
            model, trace = extract(messages, full_history=True)
            state, body = checker.resolve(messages, model)
            record.update(model_query=asdict(model), response=body["response"], requirements=asdict(state.requirements))
            assert trace.model == report["model"]
            assert body["response"]["status"] == status, body
            assert state.requirements.city_id == city_id, state
            if city_name:
                assert model.requested_location and model.requested_location["type"] == "city", model
                assert model.requested_location["name"] == city_name, model
                checker.assert_refusal(state, body, city_name)
            else:
                assert state.unsupported_city is None, state
            if name in {"moscow_after_bundle", "resume_azov", "replace_unknown", "clear_unknown"}:
                assert state.requirements.categories == ("table", "chair", "lamp"), state
                assert state.requirements.budget_rub == 15000, state
            if name == "moscow_after_bundle":
                first_refusal = body["response"]["questions"][0]["text"]
            record["passed"] = True
        except Exception as error:
            record.update(passed=False, error=str(error))
        report["cases"].append(record)
        report.update(executed=len(report["cases"]), passed=sum(row["passed"] for row in report["cases"]))
        write_report(output, report)
        print(("PASS " if record["passed"] else "FAIL ") + name, flush=True)
        if denied(record.get("error", "")):
            break
    return int(report["passed"] != executed)


if __name__ == "__main__":
    raise SystemExit(main())
