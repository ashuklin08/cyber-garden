"""Frozen unit/price regressions, including formally valid but wrong Gemma JSON."""
from dataclasses import asdict
import json
import unittest
from unittest.mock import Mock

from buyer.conversation import ConversationManager
from buyer.model import ModelQuery
from buyer.query_parser import parse_turn, normalize, _decimal, MEASURE
from tests import test_buyer as buyer_tests
from tests.test_conversation import dialog, expected


# text, budget, width, local issue; no expected state is derived from Gemma.
CASES = [
    ("Стол Азов 13.000 км 100 руб", None, [], "distance"),
    ("Стол Азов 13 000 км 100 рублей", None, [], "distance"),
    ("Стол в Азове на расстоянии 13 км", None, [], "distance"),
    ("Стол шириной 100 см", None, [["eq", 100]], None),
    ("Стол шириной до 100 см", None, [["lte", 100]], None),
    ("Стол высотой 1000 мм", None, [], "unsupported_size"),
    ("Бюджет 13 000 рублей", 13000, [], None),
    ("На весь комплект 13 000", 13000, [], None),
    ("Стол стоит 13 000 рублей", None, [], "price"),
    ("Кресло 1400 рублей", None, [], "price"),
    ("Стол до 5000 рублей", 5000, [], None),
    ("Стол 13.000", None, [], "price"),
    ("Стол 13 000 км", None, [], "distance"),
    ("Стол 13 000 ₽", None, [], "price"),
    ("Бюджет 13 тысяч, ширина стола до 100 см", 13000, [["lte", 100]], None),
]


def bad_model():
    return ModelQuery(expected(budget_rub=13000, width_cm=[["lte", 100]]), ())


class NumericUnitsTest(unittest.TestCase):
    def test_location_metadata_cannot_clear_numeric_clarification(self):
        from dataclasses import replace
        model = replace(bad_model(), requested_location=dict(name="Азов", type="city", city_id="azov"))
        state, _ = ConversationManager().resolve(dialog("Стол Азов 13.000 км 100 руб"),
                    Mock(return_value=(model, None)), require_model=True)
        self.assertEqual(state.turn_issue, "distance")
        self.assertIn("Расстояние не поддерживается", state.decision_questions()[0]["text"])

    def test_frozen_cases_through_turn_local_merge_and_resolve(self):
        for text, budget, width, issue in CASES:
            with self.subTest(text=text):
                messages = dialog(text)
                manager = ConversationManager()
                turn = parse_turn(text, set(), expected())
                self.assertEqual(turn.values.get("budget_rub"), budget)
                self.assertEqual(expected(width_cm=turn.values.get("width_cm", [])).width_cm,
                                 expected(width_cm=width).width_cm)
                local = manager.local(messages)
                self.assertEqual(local.turn_issue, issue)
                for state in (local, manager.merge(local, bad_model(), messages),
                              manager.resolve(messages, Mock(return_value=(bad_model(), None)), require_model=True)[0]):
                    self.assertEqual(state.requirements.budget_rub, budget)
                    self.assertEqual(state.requirements.width_cm, expected(width_cm=width).width_cm)
                    categories = () if text.startswith(("Бюджет 13 000", "На весь")) else (("chair",) if text.startswith("Кресло") else ("table",))
                    self.assertEqual(state.requirements.categories, categories)

    def test_explicit_budget_cannot_license_distance_number_from_model(self):
        for unit in ("км", "километр", "километра", "километров", "м", "метр", "метра", "метров",
                     "см", "сантиметр", "сантиметра", "сантиметров", "мм", "миллиметр", "миллиметра", "миллиметров"):
            for suffix in ("", ", 100 рублей"):
                with self.subTest(unit=unit, suffix=suffix):
                    messages = dialog(f"Бюджет 13 000 {unit}{suffix}")
                    state, _ = ConversationManager().resolve(messages, Mock(return_value=(bad_model(), None)), require_model=True)
                    self.assertNotEqual(state.requirements.budget_rub, 13000)

    def test_thousands_and_decimal_units(self):
        self.assertEqual(_decimal("13.000"), 13000)
        self.assertEqual(_decimal("13 000"), 13000)
        self.assertEqual(str(_decimal("1,2")), "1.2")
        hit = MEASURE.search(normalize("13.000 км"))
        self.assertEqual(hit["n"], "13.000")
        self.assertEqual(hit["u"], "км")

    def test_distance_and_item_price_preserve_previous_budget_and_wishes(self):
        manager = ConversationManager()
        base = "Деревянный стол шириной до 100 см в Азове. Бюджет 15000 рублей"
        for edit in ("Расстояние 13 000 км", "Стол стоит 13000 рублей", "Кресло 1400 рублей"):
            with self.subTest(edit=edit):
                state, _ = manager.resolve(dialog(base, "Подобрал стол", edit), Mock(return_value=(bad_model(), None)), require_model=True)
                self.assertEqual(state.requirements.budget_rub, 15000)
                self.assertEqual(state.requirements.width_cm, expected(width_cm=[["lte", 100]]).width_cm)
                self.assertTrue(any(pref.text == "деревянный" for pref in state.preferences))
        history = dialog(base, "Подобрал стол", "Расстояние 13 000 км",
                         "Расстояние не поддерживается", "Бюджет 10000 рублей")
        state, _ = manager.resolve(history, Mock(return_value=(bad_model(), None)), require_model=True)
        self.assertEqual(state.requirements.budget_rub, 10000)
        self.assertEqual(state.requirements.city_id, "azov")
        self.assertEqual(state.requirements.width_cm, expected(width_cm=[["lte", 100]]).width_cm)
        self.assertIsNone(state.turn_issue)


class NumericUnitsCliTest(unittest.TestCase):
    setUp = buyer_tests.CliTest.setUp
    invoke = buyer_tests.CliTest.invoke

    def test_all_frozen_cases_through_actual_run_with_bad_gemma(self):
        for text, budget, width, issue in CASES:
            with self.subTest(text=text):
                self.model_output = dict(city_id=None, budget_rub=13000, categories=None,
                    width_cm=[["lte", 100]], adjustable_height=None, color_temperature_k=[])
                done = self.invoke(text=text)
                self.assertEqual(done.returncode, 0, done.stderr)
                response = json.loads(done.stdout)
                self.assertEqual(response["status"], "clarify")
                if issue == "distance":
                    self.assertIn("Расстояние не поддерживается", response["questions"][0]["text"])
                if issue == "price":
                    self.assertEqual(response["questions"][0]["field"], "budget_rub")


if __name__ == "__main__":
    unittest.main()
