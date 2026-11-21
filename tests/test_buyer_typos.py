import unittest

from buyer.conversation import ConversationManager, active_dialog
from buyer.model import ModelQuery, ModelTrace
from buyer.selection import clarification_questions
from buyer.contracts import read_requirements


class BuyerTypoTest(unittest.TestCase):
    def test_typo_purchase_and_followup(self):
        history = [{"role": "user", "text": "Нужен столь в азеви, бюджет 2 тисяча"}]
        manager = ConversationManager()
        first = manager.local(history).requirements
        self.assertEqual((first.categories, first.city_id, first.budget_rub),
                         (("table",), "azov", 2000))
        history.extend([{"role": "assistant", "text": "Искать в Азове?"},
                        {"role": "user", "text": "Тогда в растове"}])
        updated = manager.local(history).requirements
        self.assertEqual((updated.categories, updated.city_id, updated.budget_rub),
                         (("table",), "rostov-na-donu", 2000))

    def test_single_typo_in_supported_product(self):
        manager = ConversationManager()
        for product in ("ккесло", "крксло", "креслло", "кресо", "керсло"):
            with self.subTest(product=product):
                state = manager.local([{"role": "user", "text":
                    f"Нужно {product} в Азове, бюджет 5 тысяч"}])
                self.assertEqual(state.requirements.categories, ("chair",))
                self.assertEqual(state.requirements.city_id, "azov")
                self.assertEqual(state.requirements.budget_rub, 5000)

    def test_lookalikes_and_nonmoney_quantities(self):
        for text in ("Ищу столь удобное кресло", "Хочу рампу", "Хочу сток воды",
                     "Сток воды перекрыт", "Лапа болит", "Нужен столик в Азии",
                     "В Растове Великом", "2 тисяча деталей", "В растворе"):
            history = [{"role": "user", "text": text}]
            self.assertEqual(active_dialog(history), history)

    def test_unclear_request_and_only_missing_budget(self):
        manager = ConversationManager()
        unclear = manager.local([{"role": "user", "text": "какая-то штука где-то недорого"}])
        self.assertEqual(unclear.decision_questions(), ({"field": "categories", "text":
            "Не удалось понять запрос. Пожалуйста, напишите отдельно, какой товар вам нужен, "
            "город самовывоза и максимальный бюджет."},))

        messages = [{"role": "user", "text": "Стол в Азове, бюджет примерно вот столько"}]
        requirements = read_requirements({"city_id": "azov", "budget_rub": None,
            "categories": ["table"], "width_cm": [], "adjustable_height": None,
            "color_temperature_k": []})
        query, _ = manager.resolve(messages, lambda *_args, **_kwargs: (
            ModelQuery(requirements, (), "ambiguous"), ModelTrace("test", 0, 0, 0)),
            require_model=True)
        self.assertFalse(query.decision_questions())
        questions = clarification_questions(query.requirements)
        self.assertEqual([question["field"] for question in questions], ["budget_rub"])
        self.assertIn("максимальную сумму", questions[0]["text"])
