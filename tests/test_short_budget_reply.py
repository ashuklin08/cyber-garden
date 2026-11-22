"""Short follow-ups change only budget and never consume measurements."""
import unittest
from buyer.conversation import ConversationManager
from buyer.query_parser import parse_turn
from buyer.contracts import read_catalog
from buyer.recovery import recovery_response
from buyer.selection import select
from tests.fixtures import product


def user(text):
    return {'role': 'user', 'text': text}


class ShortBudgetReplyTest(unittest.TestCase):
    def test_budget_edits_preserve_city_categories_and_width(self):
        manager = ConversationManager()
        initial = user('Стол не шире метра и кресло в Таганроге. Бюджет 3000 рублей')
        base = manager.local([initial]).requirements
        for text, expected in [('давай 5000', 5000), ('давайте 5 тысяч', 5000),
                               ('тогда 5 000', 5000), ('пусть будет 5000', 5000),
                               ('можно до 5000?', 5000)]:
            with self.subTest(text=text):
                result = manager.local([initial, user(text)])
                self.assertEqual(result.requirements.budget_rub, expected)
                self.assertEqual(result.requirements.city_id, base.city_id)
                self.assertEqual(result.requirements.categories, base.categories)
                self.assertEqual(result.requirements.width_cm, base.width_cm)
                self.assertFalse(result.decision_questions())

    def test_numbers_are_not_always_money(self):
        current = ConversationManager().local([user('Стол в Азове. Бюджет 3000')]).requirements
        for text, pending in [('давай 100 см', set()), ('давай 5000 кельвинов', set()),
                              ('давай 5000 деталей', set()), ('давай 5000 долларов', set()),
                              ('давай 100', {'width_cm'}), ('давай 5000', {'city_id'}),
                              ('давай на 5000 больше', set()), ('давай 5000 или 6000', set())]:
            with self.subTest(text=text, pending=pending):
                self.assertNotIn('budget_rub', parse_turn(text, pending, current).values)

    def test_budget_reply_after_recovery_offer(self):
        manager = ConversationManager()
        initial = user('Стол в Таганроге, бюджет 100 рублей')
        current = manager.local([initial]).requirements
        catalog = read_catalog([product('t', 'table', 4900, 90)])
        response, _ = recovery_response(catalog, current, (), select(catalog, current))
        state = manager.local([initial, {'role': 'assistant', 'text': response['message']},
                               user('давай 5000')], recovery_listings=catalog)
        self.assertEqual(state.requirements.budget_rub, 5000)
        self.assertEqual(select(catalog, state.requirements).response['status'], 'ok')
