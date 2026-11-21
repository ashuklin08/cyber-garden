"""City support is independent of catalog availability and spelling case."""
import json
import unittest
from unittest.mock import Mock, patch

from buyer.contracts import CITIES
from buyer.conversation import ConversationManager, UNSUPPORTED_CITY_MESSAGE
from buyer.model import ModelQuery, extract
from buyer.query_parser import Preference
from tests.fixtures import product
from tests.test_conversation import dialog, expected
from tests import test_unsupported_categories as category_tests

BASE = "Деревянный стол, кресло и лампа. Таганрог. До 15000 рублей"


def location(name, city=None, kind="city"):
    return dict(name=name, type=kind, city_id=city)


class UnsupportedCitiesTest(unittest.TestCase):
    setUp = category_tests.UnsupportedCategoryTest.setUp
    resolve = category_tests.UnsupportedCategoryTest.resolve

    def model(self, place=None, **fields):
        return ModelQuery(expected(**fields), (), "shopping", requested_location=place)

    def assert_refusal(self, state, body, name):
        self.assertEqual(state.unsupported_city, name)
        self.assertIsNone(state.requirements.city_id)
        self.assertEqual(body["response"], {"status": "clarify", "questions": [{"field": "city_id", "text":
            UNSUPPORTED_CITY_MESSAGE + ", ".join(CITIES.values()) + f". Вы указали: «{name}»."}]})
        self.assertEqual(body["items"], {})

    def test_cases_and_lowercase_preserve_original_name(self):
        for phrase, name in (("в Москве", "Москве"), ("в москве", "москве"),
                             ("из Москвы", "Москвы"), ("самовывоз из москвы", "москвы"),
                             ("в Краснодаре", "Краснодаре"), ("в краснодаре", "краснодаре"),
                             ("в Питере", "Питере"), ("в Новосибирске", "Новосибирске"),
                             ("Доставка в Санкт-Петербург", "Санкт-Петербург"), ("Самовывоз из Казани", "Казани")):
            with self.subTest(phrase=phrase):
                state, body = self.resolve(dialog(phrase), self.model(location(name)))
                self.assert_refusal(state, body, name)
                self.assertEqual(state.requirements.categories, ())
                self.assertIsNone(state.requirements.budget_rub)

    def test_A_and_B_preserve_purchase_and_resume_in_azov(self):
        history = dialog(BASE, "Подобрал комплект", "Хочу забрать в Москве")
        stale = self.model(location("Москве"), city_id="taganrog", budget_rub=1, categories=["table"])
        state, body = self.resolve(history, stale)
        self.assert_refusal(state, body, "Москве")
        self.assertEqual(state.requirements.budget_rub, 15000)
        self.assertEqual(state.requirements.categories, ("table", "chair", "lamp"))
        self.assertIn(Preference("table", "деревянный"), state.preferences)
        refusal = body["response"]["questions"][0]["text"]
        history += [{"role": "assistant", "text": refusal}, {"role": "user", "text": "Тогда в Азове"}]
        state, body = self.resolve(history, self.model(location("Азове", "azov")))
        self.assertIsNone(state.unsupported_city)
        self.assertEqual(state.requirements.city_id, "azov")
        self.assertEqual(state.requirements.budget_rub, 15000)
        self.assertEqual(state.requirements.categories, ("table", "chair", "lamp"))
        self.assertEqual(body["response"]["status"], "ok")
        self.assertEqual(body["response"]["options"][0]["listing_ids"], ["azTable", "azChair", "azLamp"])

    def test_C_unknown_city_replaces_unknown_without_inventing_budget(self):
        history = dialog("Найди стол в Москве")
        _, body = self.resolve(history, self.model(location("Москве")))
        history += [{"role": "assistant", "text": body["response"]["questions"][0]["text"]},
                    {"role": "user", "text": "А в Краснодаре?"}]
        state, body = self.resolve(history, self.model(location("Краснодаре")))
        self.assert_refusal(state, body, "Краснодаре")
        self.assertEqual(state.requirements.categories, ("table",))
        self.assertIsNone(state.requirements.budget_rub)

    def test_D_supported_city_change_and_rostov_alias(self):
        for text, city in (("Нет, лучше в Азове", "azov"), ("в ростове", "rostov-na-donu")):
            state, _ = self.resolve(dialog("Хочу деревянный стол в Таганроге", "Какой бюджет?", text), self.model())
            self.assertEqual(state.requirements.city_id, city)
            self.assertEqual(state.requirements.categories, ("table",))
            self.assertIn(Preference("table", "деревянный"), state.preferences)

    def test_E_clear_unknown_city_and_do_not_restore_previous(self):
        history = dialog("Хочу стол в Москве")
        _, body = self.resolve(history, self.model(location("Москве")))
        history += [{"role": "assistant", "text": body["response"]["questions"][0]["text"]},
                    {"role": "user", "text": "Нет, город неважен"}]
        state, body = self.resolve(history, self.model(city_id="taganrog"))
        self.assertIsNone(state.unsupported_city)
        self.assertIsNone(state.requirements.city_id)
        self.assertEqual(body["response"]["status"], "clarify")
        self.assertIn("city_id", [q["field"] for q in body["response"]["questions"]])
        self.assertNotIn("Такого города нет", str(body["response"]))

    def test_pending_unknown_cannot_reuse_old_city_on_budget_edit(self):
        _, first = self.resolve(dialog(BASE, "Подобрал комплект", "в москве"), self.model(location("москве")))
        history = dialog(BASE, "Подобрал комплект", "в москве", first["response"]["questions"][0]["text"], "Бюджет 16000 рублей")
        state, body = self.resolve(history, self.model(city_id="taganrog"))
        self.assert_refusal(state, body, "москве")
        self.assertEqual(state.requirements.budget_rub, 16000)

    def test_uncertain_place_and_non_city_are_ordinary_questions(self):
        for name, kind in (("чемодане", "other"), (None, "uncertain")):
            state, body = self.resolve(dialog("Стол в чемодане"), self.model(location(name, kind=kind)))
            self.assertIsNone(state.unsupported_city)
            self.assertEqual(body["response"]["status"], "clarify")
            self.assertNotIn("Такого города нет", str(body["response"]))

    def test_city_list_is_dynamic(self):
        state, _ = self.resolve(dialog("в москве"), self.model(location("москве")))
        with patch.dict(CITIES, {"new": "Новый город"}, clear=True):
            self.assertEqual(state.decision_questions()[0]["text"],
                             UNSUPPORTED_CITY_MESSAGE + "Новый город. Вы указали: «москве».")

    def test_model_cannot_substitute_a_supported_id_for_moscow(self):
        state, body = self.resolve(dialog("Стол в москве"), self.model(location("москве", "taganrog")))
        self.assert_refusal(state, body, "москве")

    def test_supported_name_is_checked_even_when_model_omits_id(self):
        state, _ = self.resolve(dialog("Стол в Азове"), self.model(location("Азове")))
        self.assertIsNone(state.unsupported_city)
        self.assertEqual(state.requirements.city_id, "azov")

    def test_supported_city_without_listings_or_matching_features_is_no_match(self):
        for text, fields, rows in (
            ("Кресло в Азове до 5000 рублей", dict(city_id="azov", budget_rub=5000, categories=["chair"]),
             [product("chair", "chair", 1000, True)]),
            ("Стол до 90 см в Азове до 5000 рублей", dict(city_id="azov", budget_rub=5000, categories=["table"], width_cm=[["lte", 90]]),
             [product("wide", "table", 1000, 120, "azov")])):
            with self.subTest(text=text):
                state, body = self.resolve(dialog(text), self.model(**fields), rows)
                self.assertIsNone(state.unsupported_city)
                self.assertEqual(body["response"]["status"], "no_match")
                self.assertNotIn("Такого города нет", body["response"]["message"])

    def test_unknown_raw_model_city_is_sanitized_before_contract_validation(self):
        fields = dict(city_id="moscow", budget_rub=None, categories=["table"], width_cm=[],
                      adjustable_height=None, color_temperature_k=[])
        raw = json.dumps({"choices": [{"message": {"content": json.dumps(fields)}}]}).encode()
        config = dict(LLM_BASE_URL="http://127.0.0.1/v1", LLM_MODEL="mock", LLM_API_KEY="mock", LLM_USER_AGENT="mock")
        with patch("buyer.model.configuration", return_value=config), patch("buyer.model._gateway", return_value=raw):
            model, _ = extract(dialog("Стол в Москве"))
        self.assertIsNone(model.requirements.city_id)
        state, body = self.resolve(dialog("Стол в Москве"), model)
        self.assert_refusal(state, body, "Москве")
        with patch("buyer.model.configuration", return_value=config), patch("buyer.model._gateway", return_value=raw):
            model, _ = extract(dialog("здарова брат, надо столик найти"))
        self.assertIsNone(model.requirements.city_id)
        self.assertIsNone(model.requested_location)


if __name__ == "__main__":
    unittest.main()
