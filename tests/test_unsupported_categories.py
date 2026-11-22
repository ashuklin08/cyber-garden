"""Category support, catalog availability and hard-filter absence stay distinct."""
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from jsonschema import Draft202012Validator

from buyer.contracts import CATEGORIES, ROOT, read_catalog
from buyer.conversation import ConversationManager, UNSUPPORTED_MESSAGE
from buyer.demo import Handler
from buyer.model import ModelError, ModelQuery, ModelTrace, extract
from buyer.query_parser import Preference
from tests.fixtures import CATALOG, product
from tests.test_conversation import dialog, expected


REFUSAL = "Такого каталога нет. Пожалуйста, выберите из имеющихся. Доступны: Стол, Кресло, Лампа."


class UnsupportedCategoryTest(unittest.TestCase):
    def setUp(self):
        self.manager = ConversationManager()
        self.validator = Draft202012Validator(json.loads((ROOT / "schemas/response.schema.json").read_bytes()))

    def resolve(self, messages, model=None, rows=CATALOG):
        gemma = Mock(return_value=(model or ModelQuery(expected(), (), "shopping"), ModelTrace("mock-gemma", 1, 1, 1)))
        state, trace = self.manager.resolve(messages, gemma, require_model=True)
        gemma.assert_called_once()
        # Exercise the production response path as well as the real manager.
        handler = Handler.__new__(Handler)
        handler.server = SimpleNamespace(listings=read_catalog(rows), provider=None,
                                         titles={row["id"]: row["title"] for row in rows})
        handler._stages = None
        body = handler._buyer_result(state, trace)
        self.validator.validate(body["response"])
        return state, body

    def assert_refusal(self, body):
        self.assertEqual(body["response"], {"status": "clarify", "questions": [
            {"field": "categories", "text": REFUSAL}]})
        self.assertEqual(body["items"], {})
        self.assertNotIn("options", body["response"])

    def test_four_unsupported_products_even_when_model_substitutes_old_table(self):
        for text in ("Найди автомобиль", "Хочу холодильник", "Подбери велосипед", "Нужен диван"):
            with self.subTest(text=text):
                # An incorrect supported category from Gemma cannot bypass local evidence.
                model = ModelQuery(expected(categories=["table"]), (Preference("table", text),), "shopping")
                state, body = self.resolve(dialog(text), model)
                self.assert_refusal(body)
                self.assertTrue(state.blocked_categories)
                self.assertEqual(state.requirements, expected())
                self.assertEqual(state.preferences, ())

    def test_missing_supported_category_is_no_match_not_missing_catalog(self):
        rows = [row for row in CATALOG if row["category"] != "chair"]
        gold = expected(city_id="taganrog", categories=["chair"])
        state, body = self.resolve(dialog("Нужно кресло в Таганроге"), ModelQuery(gold, ()), rows)
        self.assertEqual(state.requirements, gold)
        self.assertFalse(state.blocked_categories)
        self.assertEqual(body["response"], {"status": "no_match", "message":
            "Город самовывоза — Таганрог. «Кресло» отсутствует в каталоге. Подойдёт другой город или другой состав комплекта?"})
        self.assertEqual(body["items"], {})

    def test_model_intent_cannot_override_local_unsupported_category(self):
        for intent in ("off_topic", "ambiguous"):
            with self.subTest(intent=intent):
                _, body = self.resolve(dialog("Найди автомобиль"), ModelQuery(expected(), (), intent))
                self.assert_refusal(body)

    def test_existing_category_without_matching_listings_is_no_match(self):
        rows = [product("wide", "table", 1000, 120)]
        gold = expected(city_id="taganrog", budget_rub=5000, categories=["table"], width_cm=[["lte", 90]])
        state, body = self.resolve(dialog("Стол не шире 90 см в Таганроге, бюджет 5000 рублей"), ModelQuery(gold, ()), rows)
        self.assertEqual(state.requirements, gold)
        self.assertFalse(state.blocked_categories)
        self.assertEqual(body["response"]["status"], "no_match")
        self.assertIn("Ширина стола ≤ 90 см", body["response"]["message"])
        self.assertIn("≤ 120 см", body["response"]["message"])
        self.assertIn("1 000 ₽", body["response"]["message"])
        self.assertEqual(body["items"], {})

    def test_supported_choice_after_refusal_unblocks_and_does_not_restore_bundle(self):
        for choice in ("Тогда найди стол", "Добавь стол"):
            with self.subTest(choice=choice):
                history = dialog("Найди автомобиль в Азове, бюджет 5000 рублей")
                _, body = self.resolve(history)
                self.assert_refusal(body)
                history += [{"role": "assistant", "text": REFUSAL}, {"role": "user", "text": choice}]
                stale = ModelQuery(expected(city_id="azov", budget_rub=5000, categories=["table", "chair", "lamp"]), ())
                state, body = self.resolve(history, stale)
                self.assertEqual(state.requirements, expected(city_id="azov", budget_rub=5000, categories=["table"]))
                self.assertFalse(state.blocked_categories)
                self.assertEqual(body["response"]["status"], "ok")
                self.assertEqual(body["response"]["options"][0]["listing_ids"], ["azTable"])

    def test_refusal_preserves_budget_city_width_and_supported_preferences(self):
        base = "Деревянный стол не шире 100 см в Азове, бюджет 5000 рублей"
        history = dialog(base, "Подобрал стол.", "Хочу холодильник")
        wrong = ModelQuery(expected(city_id="taganrog", budget_rub=1, categories=["chair"], width_cm=[]),
                           (Preference("chair", "холодильник"),), "shopping")
        state, body = self.resolve(history, wrong)
        self.assert_refusal(body)
        self.assertEqual(state.requirements, expected(city_id="azov", budget_rub=5000, width_cm=[["lte", 100]]))
        self.assertEqual(state.preferences, (Preference("table", "деревянный"),))
        history += [{"role": "assistant", "text": REFUSAL}, {"role": "user", "text": "Тогда найди стол"}]
        state, body = self.resolve(history, wrong)
        self.assertFalse(state.blocked_categories)
        self.assertEqual(state.requirements, expected(city_id="azov", budget_rub=5000,
                                                     categories=["table"], width_cm=[["lte", 100]]))
        self.assertEqual(state.preferences, (Preference("table", "деревянный"),))
        self.assertEqual(body["response"]["status"], "ok")

    def test_budget_only_reply_cannot_search_old_category_after_refusal(self):
        history = dialog("Стол в Азове, бюджет 5000 рублей", "Подобрал стол", "Хочу автомобиль",
                         REFUSAL, "Бюджет теперь 6000 рублей")
        state, body = self.resolve(history, ModelQuery(expected(categories=["table"]), ()))
        self.assert_refusal(body)
        self.assertEqual(state.requirements.budget_rub, 6000)
        self.assertEqual(state.requirements.city_id, "azov")

    def test_supported_choice_after_refusal_replaces_previous_bundle(self):
        history = dialog("Стол, кресло и лампа в Азове, бюджет 5000 рублей", "Подобрал комплект",
                         "Хочу автомобиль", REFUSAL, "Тогда найди стол")
        stale = ModelQuery(expected(categories=["table", "chair", "lamp"]), ())
        state, body = self.resolve(history, stale)
        self.assertEqual(state.requirements, expected(city_id="azov", budget_rub=5000, categories=["table"]))
        self.assertEqual(body["response"]["status"], "ok")
        self.assertEqual(body["response"]["options"][0]["listing_ids"], ["azTable"])

    def test_semantic_unknown_is_checked_against_registry_not_model_intent(self):
        # Outside the Russian local item-slot grammar: Gemma supplies product identity,
        # while backend membership, not a model boolean, determines support.
        model = ModelQuery(expected(categories=["chair"]), (), "shopping", ("bicycle",))
        state, body = self.resolve(dialog("Could you locate a bicycle for me?"), model)
        self.assert_refusal(body)
        self.assertTrue(state.blocked_categories)

    def test_semantic_refusal_survives_constraint_edit_until_supported_choice(self):
        history = dialog("Стол в Азове, бюджет 5000 рублей", "Подобрал стол",
                         "Could you locate a bicycle for me?", REFUSAL, "Бюджет теперь 6000 рублей")
        stale = ModelQuery(expected(categories=["table"]), ())
        state, body = self.resolve(history, stale)
        self.assert_refusal(body)
        self.assertEqual(state.requirements, expected(city_id="azov", budget_rub=6000))
        history += [{"role": "assistant", "text": REFUSAL}, {"role": "user", "text": "Тогда найди стол"}]
        state, body = self.resolve(history, stale)
        self.assertFalse(state.blocked_categories)
        self.assertEqual(body["response"]["status"], "ok")
        self.assertEqual(state.requirements, expected(city_id="azov", budget_rub=6000, categories=["table"]))

    def test_semantic_refusal_with_unrecognized_followup_keeps_refusal(self):
        _, body = self.resolve(dialog("Could you locate a bicycle for me?", REFUSAL, "???"))
        self.assert_refusal(body)

    def test_unsupported_product_attributes_do_not_overwrite_previous_table(self):
        history = dialog("Стол не шире 90 см в Азове, бюджет 5000 рублей", "Подобрал стол",
                         "Хочу диван шириной 200 см")
        state, body = self.resolve(history)
        self.assert_refusal(body)
        self.assertEqual(state.requirements, expected(city_id="azov", budget_rub=5000, width_cm=[["lte", 90]]))

    def test_model_category_ids_are_rechecked_even_for_direct_model_query(self):
        model = ModelQuery(replace(expected(), categories=("car",)), (), "shopping")
        _, body = self.resolve(dialog("Find a car"), model)
        self.assert_refusal(body)

    def test_available_names_are_read_from_registry(self):
        state, _ = self.resolve(dialog("Нужен диван"))
        with patch.dict(CATEGORIES, {"table": "Название из справочника"}, clear=True):
            self.assertEqual(state.decision_questions(), ({"field": "categories", "text":
                UNSUPPORTED_MESSAGE + ". Доступны: Название из справочника."},))


class UnsupportedCategoryExtractionTest(unittest.TestCase):
    def extract(self, **extra):
        fields = dict(city_id=None, budget_rub=None, categories=None, width_cm=[],
                      adjustable_height=None, color_temperature_k=[], preferences=[], intent="shopping") | extra
        response = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(fields)}}]}).encode()
        config = dict(LLM_BASE_URL="http://127.0.0.1/v1", LLM_MODEL="mock", LLM_API_KEY="mock", LLM_USER_AGENT="mock")
        with patch("buyer.model.configuration", return_value=config), patch("buyer.model._gateway", return_value=response):
            return extract(dialog("Найди автомобиль"))[0]

    def test_internal_product_recognition_is_not_an_official_contract_field(self):
        model = self.extract(requested_categories=["автомобиль"])
        self.assertEqual(model.requested_categories, ("автомобиль",))
        self.assertEqual(model.requirements.categories, ())

    def test_unknown_model_id_becomes_refusal_evidence_instead_of_technical_error(self):
        model = self.extract(categories=["car"])
        state, _ = ConversationManager().resolve(dialog("Find a car"), Mock(return_value=(model, None)), require_model=True)
        self.assertTrue(state.blocked_categories)
        self.assertEqual(model.requested_categories, ("car",))

    def test_malformed_product_metadata_still_fails(self):
        for value in ("car", [False], [""], ["x" * 121]):
            with self.subTest(value=value), self.assertRaises(ModelError):
                self.extract(requested_categories=value)


if __name__ == "__main__":
    unittest.main()
