"""Frozen number/unit checks through the real ConversationManager and Gemma."""
import argparse
from dataclasses import asdict
from pathlib import Path

from buyer.contracts import ROOT, read_catalog
from buyer.conversation import ConversationManager
from buyer.model import extract
from buyer.selection import select
from tests.fixtures import CATALOG
from tests.live_observer import install, events
from tests.live_support import preflight, Pacer, denied, write_report
from tests.test_conversation import dialog, expected


CASES = [
    ("original", "стол Азов 13.000 км 100 руб", None, [], "distance"),
    ("kilometers", "Стол в Азове на расстоянии 13 км", None, [], "distance"),
    ("budget_distance", "Стол в Азове. Бюджет 13 000 км", None, [], "distance"),
    ("rubles", "Стол в Азове. Бюджет 13 000 рублей", 13000, [], None),
    ("mixed", "Стол шириной до 90 см в Азове, бюджет 5000 рублей, расстояние 13 км", 5000, [["lte", 90]], "distance"),
    ("ambiguous", "Стол в Азове 13.000", None, [], "price"),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-prefix", default="numeric-units-live")
    parser.add_argument("--case", action="append", dest="selected")
    args = parser.parse_args()
    meta = preflight()
    output = ROOT / "bench/results" / (args.output_prefix + ".json")
    trace_path = Path(str(output) + "-http.jsonl")
    if output.exists() or trace_path.exists():
        raise FileExistsError("Choose a new output prefix")
    install(trace_path)
    pacer = Pacer()
    records = []
    cases = [row for row in CASES if not args.selected or row[0] in args.selected]
    for name, text, budget, width, issue in cases:
        pacer.wait()
        before = len(events(trace_path))
        record = dict(name=name, text=text, expected=asdict(expected(city_id="azov", categories=["table"],
                     budget_rub=budget, width_cm=width)))
        captured = None

        def gemma(messages, *, full_history):
            nonlocal captured
            captured, trace = extract(messages, full_history=full_history)
            return captured, trace

        try:
            state, trace = ConversationManager().resolve(dialog(text), gemma, require_model=True)
            response = ({"status": "clarify", "questions": list(state.decision_questions())}
                        if state.decision_questions() else select(read_catalog(CATALOG), state.requirements, state.preferences).response)
            record.update(model=asdict(captured), actual=asdict(state.requirements), response=response)
            assert state.requirements == expected(city_id="azov", categories=["table"], budget_rub=budget, width_cm=width), record
            assert state.turn_issue == issue, record
            assert trace.model == meta["model"]
            assert response["status"] == ("ok" if name == "rubles" else "clarify"), record
            record["passed"] = True
        except Exception as error:
            record.update(passed=False, error=str(error))
        observed = events(trace_path)[before:]
        pacer.after(observed)
        records.append(record)
        write_report(output, meta | dict(planned=len(cases), executed=len(records), passed=sum(row["passed"] for row in records), cases=records))
        print(("PASS " if record["passed"] else "FAIL ") + name, flush=True)
        if denied(record.get("error", "")):
            break
    return int(len(records) != len(cases) or not all(row["passed"] for row in records))


if __name__ == "__main__":
    raise SystemExit(main())
