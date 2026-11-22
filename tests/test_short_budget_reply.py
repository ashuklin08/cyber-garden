"""Short follow-ups change only budget and never consume measurements."""
import unittest
from buyer.conversation import ConversationManager, PRICE_QUESTION
from buyer.model import ModelQuery
from buyer.query_parser import parse_turn
from buyer.contracts import read_catalog
from buyer.recovery import recovery_response
from buyer.selection import select
from tests.fixtures import product
from tests.test_conversation import dialog, expected


def user(text):
    return {'role': 'user', 'text': text}


class ShortBudgetReplyTest(unittest.TestCase):
    def test_priced_search_replaces_bundle_and_honours_azov(self):
        previous = ['Стол, кресло и лампа в Таганроге. Бюджет 15000 рублей.',
                    'Подобрал комплект с самовывозом. Самый доступный вариант — 4 900 ₽.']
        catalog = read_catalog([product('local', 'table', 4900, 90, 'azov'),
                                product('wrong-city', 'table', 500, 80),
                                product('expensive', 'table', 5001, 90, 'azov')])
        # Gemma may repeat the old bundle or return empty location metadata on a short reply.
        stale = ModelQuery(expected(city_id='taganrog', budget_rub=15000,
                                    categories=['table', 'chair', 'lamp']), (), 'ambiguous',
                           requested_location=dict(type='uncertain', name=None, city_id=None))
        for start, phrase, followup in [
                ([], 'Найти стол за 5000 в Азове', []),
                (previous, 'Найти стол за 5000 в Азове', []),
                (previous, 'Найти стол за 5000 в Азове', [PRICE_QUESTION, 'один товар']),
                (previous, 'Найди стол за 5 000 рублей в Азове', []),
                (previous, 'Найти стол за 5 тысяч рублей в Азове', []),
                (previous, 'Найти стол за 5к в Азове', []),
                (previous, 'Стол 5000 в Азове', [PRICE_QUESTION, 'цена одного товара'])]:
            with self.subTest(phrase=phrase, followup=followup, previous=bool(start)):
                state, _ = ConversationManager().resolve(dialog(*start, phrase, *followup),
                    lambda *args, **kwargs: (stale, None), require_model=True)
                self.assertFalse(state.decision_questions())
                self.assertEqual(state.requirements, expected(city_id='azov', budget_rub=5000, categories=['table']))
                result = select(catalog, state.requirements).response
                self.assertEqual(result['status'], 'ok')
                self.assertEqual([(o['listing_ids'], o['total_rub']) for o in result['options']], [(['local'], 4900)])

    def test_total_price_reply_keeps_composition(self):
        for phrase in ('Стол 5000 в Азове', 'Найти стол за 5000 в Азове'):
            with self.subTest(phrase=phrase):
                state = ConversationManager().local(dialog(
                    'Стол и лампа в Таганроге, бюджет 8000', 'Подобрал комплект.',
                    phrase, PRICE_QUESTION, 'На весь комплект'))
                self.assertFalse(state.decision_questions())
                self.assertEqual(state.requirements, expected(city_id='azov', budget_rub=5000, categories=['table', 'lamp']))

    def test_purchase_shorthand_is_budget_only(self):
        turn = parse_turn('Найти лампу за 5к в Азове', set(), expected())
        self.assertEqual(turn.values['budget_rub'], 5000)
        self.assertNotIn('color_temperature_k', turn.values)

    def test_scope_answer_requires_buyer_amount_and_matching_question(self):
        for phrase, question in [('Найти стол за 5000 км в Азове', PRICE_QUESTION),
                                 ('Найти стол за 5000 долларов в Азове', PRICE_QUESTION),
                                 ('Стол 5000 или 7000 в Азове', PRICE_QUESTION),
                                 ('Стол 5000 в Азове', 'Нашёл комплект за 4900 ₽. Подходит?')]:
            with self.subTest(phrase=phrase, question=question):
                state = ConversationManager().local(dialog(phrase, question, 'один товар'))
                self.assertIsNone(state.requirements.budget_rub)

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
