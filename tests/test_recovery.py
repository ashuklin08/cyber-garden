"""Strict search and end-to-end confirmation against controlled catalogs."""

from dataclasses import replace
from decimal import Decimal
from itertools import product as cartesian
import random
import unittest
from unittest.mock import Mock

from buyer.contracts import Comparison, read_catalog
from buyer.conversation import ConversationManager
from buyer.model import ModelError, ModelQuery, ModelTrace
from buyer.query_parser import Preference
from buyer.recovery import (CHOOSE, DECLINED, RecoveryHistory, build_recovery_suggestions,
                            recovery_response)
from buyer.selection import select
from tests.fixtures import product
from tests.test_buyer import requirements


def row(id, category, price, feature=None, city="taganrog"):
    return product(id, category, price, feature) | {"city_id": city}


def user(text):
    return {"role": "user", "text": text}


def assistant(text):
    return {"role": "assistant", "text": text}


class RecoveryTest(unittest.TestCase):
    def suggestions(self, rows, req, preferences=()):
        catalog = read_catalog(rows)
        original = select(catalog, req, preferences)
        self.assertEqual(original.response["status"], "no_match")
        offers = build_recovery_suggestions(catalog, req, preferences, original)
        self.assertLessEqual(len(offers), 3)
        for offer in offers:
            outcome = select(catalog, offer.apply(req), preferences)
            self.assertEqual(outcome.response["status"], "ok")
            self.assertEqual(outcome.minimum_total_rub, offer.minimum_total_rub)
        return catalog, offers

    def confirmed(self, offers, fields, catalog, req, expected):
        offer = next(offer for offer in offers if {item.field for item in offer.changes} == set(fields))
        revised = offer.apply(req)
        result = select(catalog, revised)
        self.assertEqual(result.response["options"][0]["listing_ids"], expected)
        return revised, offer

    def test_exact_budget_uses_city_and_all_bound_attributes(self):
        req = requirements(categories=["table", "chair", "lamp"], budget_rub=5000, width_cm=[["lte", 90]],
                           adjustable_height=True, color_temperature_k=[["eq", 3000]])
        rows = [row("t", "table", 2500, 90), row("wide", "table", 5, 120),
                row("c", "chair", 2200, True), row("l", "lamp", 1000, 3000),
                row("away", "table", 5, 90, "azov")]
        catalog, offers = self.suggestions(rows, req, [Preference("table", "деревянный")])
        revised, offer = self.confirmed(offers, ["budget_rub"], catalog, req, ["t", "c", "l"])
        self.assertEqual((revised.budget_rub, offer.minimum_total_rub, req.budget_rub), (5700, 5700, 5000))
        self.assertEqual(select(catalog, replace(revised, budget_rub=5699)).response["status"], "no_match")

    def test_other_city_complete_and_affordable(self):
        req = requirements(categories=["table", "chair"])
        rows = [row("az-t", "table", 2500, 90, "azov"), row("az-c", "chair", 2400, True, "azov")]
        catalog, offers = self.suggestions(rows, req)
        revised, _ = self.confirmed(offers, ["city_id"], catalog, req, ["az-t", "az-c"])
        self.assertEqual((revised.city_id, revised.budget_rub), ("azov", req.budget_rub))

    def test_other_city_over_budget_needs_both_changes(self):
        req = requirements(budget_rub=1000)
        catalog, offers = self.suggestions([row("az-t", "table", 1800, 90, "azov")], req)
        self.assertEqual(len(offers), 1)
        revised, offer = self.confirmed(offers, ["city_id", "budget_rub"], catalog, req, ["az-t"])
        self.assertEqual((revised.city_id, revised.budget_rub, offer.minimum_total_rub), ("azov", 1800, 1800))

    def test_width_relaxes_to_nearest_affordable_structured_bound(self):
        req = requirements(budget_rub=1000, width_cm=[["gte", 75], ["lte", 80]])
        rows = [row("too-small", "table", 20, 69), row("close-costly", "table", 1500, 82),
                row("fit", "table", 900, 85), row("wide", "table", 50, 110)]
        catalog, offers = self.suggestions(rows, req)
        revised, _ = self.confirmed(offers, ["width_cm"], catalog, req, ["fit"])
        self.assertEqual(revised.width_cm, (Comparison("gte", Decimal(75)), Comparison("lte", Decimal(85))))

    def test_temperature_lower_and_exact(self):
        rows = [row("warm", "lamp", 500, 3100), row("cold", "lamp", 200, 2800)]
        for op, value in (("gte", 3300), ("eq", 3000)):
            with self.subTest(op=op):
                req = requirements(categories=["lamp"], color_temperature_k=[[op, value]])
                catalog, offers = self.suggestions(rows, req)
                revised, _ = self.confirmed(offers, ["color_temperature_k"], catalog, req, ["warm"])
                self.assertEqual(revised.color_temperature_k, (Comparison(op, Decimal(3100)),))

    def test_opposite_boolean_excludes_unknowns(self):
        req = requirements(categories=["chair"], adjustable_height=True)
        catalog, offers = self.suggestions([row("fixed", "chair", 800, False), row("unknown", "chair", 5)], req)
        revised, _ = self.confirmed(offers, ["adjustable_height"], catalog, req, ["fixed"])
        self.assertIs(revised.adjustable_height, False)

    def test_combination_requires_width_and_budget(self):
        req = requirements(budget_rub=700, width_cm=[["lt", 80]])
        catalog, offers = self.suggestions([row("only", "table", 1000, 80)], req)
        revised, _ = self.confirmed(offers, ["width_cm", "budget_rub"], catalog, req, ["only"])
        self.assertEqual((revised.width_cm, revised.budget_rub), ((Comparison("lte", Decimal(80)),), 1000))

    def test_category_absent_in_entire_catalog_precise_message(self):
        req = requirements(categories=["table", "lamp"])
        catalog, _ = self.suggestions([row("t", "table", 1500)], req)
        response, _ = recovery_response(catalog, req, (), select(catalog, req))
        self.assertIn("«Лампа» отсутствует во всём текущем каталоге", response["message"])
        self.assertEqual(set(response), {"status", "message"})

    def test_category_absent_only_locally(self):
        req = requirements(categories=["table", "lamp"])
        rows = [row("t", "table", 1500), row("az-t", "table", 1500, city="azov"),
                row("az-l", "lamp", 800, city="azov")]
        catalog, offers = self.suggestions(rows, req)
        self.confirmed(offers, ["city_id"], catalog, req, ["az-t", "az-l"])
        self.assertIn("отсутствует в городе Таганрог", offers[0].reason)

    def test_unknown_structured_attribute_cannot_be_relaxed_to_title(self):
        req = requirements(width_cm=[["lte", 90]])
        catalog, offers = self.suggestions([row("unknown", "table", 500) |
                                            {"description": "ширина 85 см"}], req)
        self.assertFalse(offers)
        result, _ = recovery_response(catalog, req, (), select(catalog, req))
        self.assertIn("не указана характеристика", result["message"])

    def test_recovery_never_offers_an_unrepresentable_attribute_bound(self):
        req = requirements(width_cm=[["lte", 90]])
        _, offers = self.suggestions([row("extreme", "table", 500, 10**12 + 1)], req)
        self.assertFalse(offers)

    def test_no_recovery_when_all_categories_missing(self):
        req = requirements(categories=["table", "chair"])
        _, offers = self.suggestions([row("only-lamp", "lamp", 100)], req)
        self.assertFalse(offers)

    def test_ties_independent_of_catalog_order(self):
        req = requirements(width_cm=[["lte", 80]])
        rows = [row("a", "table", 500, 85), row("b", "table", 500, 85)]
        self.assertEqual(self.suggestions(rows, req)[1], self.suggestions(rows[::-1], req)[1])

    def test_randomized_minimum_against_explicit_bundle_enumeration(self):
        rng = random.Random(81)
        req = requirements(categories=["table", "chair", "lamp"], width_cm=[["lte", 90]],
                           adjustable_height=True, color_temperature_k=[["gte", 3000]], budget_rub=5)
        for i in range(32):
            groups = [[], [], []]
            rows = []
            for j, (category, feature) in enumerate((("table", 90), ("chair", True), ("lamp", 3000))):
                for k in range(rng.randint(1, 5)):
                    value = feature if k else None
                    entry = row(f"{i}-{j}-{k}", category, rng.randint(30, 200), value)
                    rows.append(entry)
                    if value is not None:
                        groups[j].append(entry["price_rub"])
                if not groups[j]:
                    entry = row(f"{i}-{j}-known", category, 100, feature)
                    rows.append(entry)
                    groups[j].append(100)
            expected = min(map(sum, cartesian(*groups)))
            _, offers = self.suggestions(rows, req)
            budget = next(offer for offer in offers if [c.field for c in offer.changes] == ["budget_rub"])
            self.assertEqual(budget.minimum_total_rub, expected)


class RecoveryDialogueTest(unittest.TestCase):
    def setUp(self):
        self.manager = ConversationManager()
        self.gemma = Mock(return_value=(ModelQuery(requirements(), ()), ModelTrace("mock", 1, 1, 1)))

    def resolve(self, messages, catalog, cache=None):
        return self.manager.resolve(messages, self.gemma, require_model=True,
                                    recovery_listings=catalog, recovery_history=cache)[0]

    def start(self, rows, text):
        catalog = read_catalog(rows)
        messages = [user(text)]
        query = self.resolve(messages, catalog)
        result, offers = recovery_response(catalog, query.requirements, query.preferences,
                                            select(catalog, query.requirements, query.preferences))
        self.assertEqual(result["status"], "no_match")
        return catalog, [*messages, assistant(result["message"])], offers, query

    def test_budget_acceptance_and_undo_replay_without_gemma(self):
        catalog, messages, _, before = self.start([row("t", "table", 5700, 90)],
            "Стол до 90 см. Таганрог. Бюджет 5000 рублей.")
        accepted = [*messages, user("Увеличивай до 5700")]
        query = self.resolve(accepted, catalog)
        self.assertEqual(query.requirements, replace(before.requirements, budget_rub=5700))
        self.assertEqual(select(catalog, query.requirements).response["options"][0]["listing_ids"], ["t"])
        undo = self.resolve([*accepted, assistant("Подбор готов."), user("Отмени последнее изменение")], catalog)
        self.assertEqual(undo.requirements, before.requirements)
        self.assertEqual(select(catalog, undo.requirements).response["status"], "no_match")
        self.assertEqual(self.gemma.call_count, 1)

    def test_city_then_explicit_city_change(self):
        catalog, messages, _, before = self.start([row("az", "table", 1000, 90, "azov"),
            row("ro", "table", 1100, 85, "rostov-na-donu")], "Стол. Таганрог. Бюджет 5000 рублей.")
        accepted = [*messages, user("Давай Азов")]
        query = self.resolve(accepted, catalog)
        self.assertEqual(query.requirements, replace(before.requirements, city_id="azov"))
        changed = self.resolve([*accepted, assistant("Подбор готов."), user("Город Ростов-на-Дону")], catalog)
        self.assertEqual(changed.requirements.city_id, "rostov-na-donu")
        self.assertEqual(select(catalog, changed.requirements).response["options"][0]["listing_ids"], ["ro"])

    def test_width_short_reply_preserves_preferences(self):
        catalog, messages, _, before = self.start([row("t", "table", 1500, 85)],
            "Стол не шире 80 см, желательно деревянный. Таганрог. Бюджет 5000 рублей.")
        query = self.resolve([*messages, user("Можно до 85 см")], catalog)
        self.assertEqual(query.requirements.width_cm, (Comparison("lte", Decimal(85)),))
        self.assertEqual(query.preferences, before.preferences)
        self.assertEqual(self.gemma.call_count, 1)

    def test_explicit_removal_clears_only_removed_condition(self):
        catalog, messages, _, before = self.start([row("t", "table", 800, 90)],
            "Стол не шире 90 см и лампа 4000 К. Таганрог. Бюджет 5000 рублей.")
        query = self.resolve([*messages, user("Убери лампу")], catalog)
        self.assertEqual(query.requirements, replace(before.requirements, categories=("table",), color_temperature_k=()))
        self.assertEqual(select(catalog, query.requirements).response["options"][0]["listing_ids"], ["t"])
        self.assertEqual(self.gemma.call_count, 1)

    def test_decline_and_off_topic_do_not_apply_suggestions(self):
        catalog, messages, _, before = self.start([row("t", "table", 1500, 90)],
            "Стол. Таганрог. Бюджет 500 рублей.")
        refused = self.resolve([*messages, user("Нет, оставь как было")], catalog)
        self.assertEqual((refused.requirements, refused.recovery_note), (before.requirements, DECLINED))
        self.gemma.side_effect = AssertionError("Off-topic recovery turn must not pick a city")
        unrelated = self.resolve([*messages, user("Какая погода в Азове?")], catalog)
        self.assertEqual(unrelated.requirements, before.requirements)
        self.assertTrue(unrelated.decision_questions())

    def test_partial_agreement_to_combined_edit_requires_explicit_choice(self):
        catalog, messages, _, before = self.start([row("az-t", "table", 1800, 90, "azov")],
            "Стол. Таганрог. Бюджет 1000 рублей.")
        incomplete = self.resolve([*messages, user("Давай Азов")], catalog)
        self.assertEqual((incomplete.requirements, incomplete.recovery_note), (before.requirements, CHOOSE))
        confirmed = self.resolve([*messages, user("Вариант 1")], catalog)
        self.assertEqual((confirmed.requirements.city_id, confirmed.requirements.budget_rub), ("azov", 1800))

    def test_changed_catalog_does_not_apply_stale_offer(self):
        _, messages, _, before = self.start([row("t", "table", 1500, 90)],
            "Стол. Таганрог. Бюджет 500 рублей.")
        query = self.resolve([*messages, user("Вариант 1")], read_catalog([row("t", "table", 200, 90)]))
        self.assertEqual(query.requirements, before.requirements)
        self.assertEqual(query.turn_issue, "recovery_stale")

    def test_model_only_state_recovered_from_exact_web_history(self):
        catalog = read_catalog([row("t", "table", 1500, 90)])
        messages = [user("Нужен стол в Таганроге за полтысячи")]
        req = requirements(budget_rub=500)
        model = Mock(return_value=(ModelQuery(req, ()), ModelTrace("mock", 1, 1, 1)))
        query = self.manager.resolve(messages, model, require_model=True)[0]
        report, options = recovery_response(catalog, query.requirements, (), select(catalog, query.requirements))
        cache = RecoveryHistory()
        cache.remember(messages, report, query, options)
        resolved = self.resolve([*messages, assistant(report["message"]), user("Вариант 1")], catalog, cache)
        self.assertEqual(resolved.requirements.budget_rub, 1500)

    def test_technical_error_propagates(self):
        self.gemma.side_effect = ModelError("Таймаут")
        with self.assertRaises(ModelError):
            self.resolve([user("Стол. Таганрог. Бюджет 500 рублей.")], ())
