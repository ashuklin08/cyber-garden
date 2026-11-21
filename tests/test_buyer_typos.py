import unittest

from buyer.conversation import ConversationManager, active_dialog


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

    def test_lookalikes_and_nonmoney_quantities(self):
        for text in ("Ищу столь удобное кресло", "Нужен столик в Азии",
                     "В Растове Великом", "2 тисяча деталей", "В растворе"):
            history = [{"role": "user", "text": text}]
            self.assertEqual(active_dialog(history), history)
