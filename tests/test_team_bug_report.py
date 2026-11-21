import unittest
from buyer.conversation import ConversationManager


class TeamBugReportTest(unittest.TestCase):
    def test_reported_product_requests(self):
        cases = [
            ("найди мне комплект - Стол и Лампу в Таганроге до 1000 рублей", ("table", "lamp"), 1000),
            ("мне нужно крксло", ("chair",), None),
            ("найди мне наилучшую лумпу за 1000 рубликов в азове", ("lamp",), 1000),
            ("лампа нужна вась", ("lamp",), None),
            ("здарова брат, надо столик найти прим пиздатый, чтоб на всю семью", ("table",), None),
        ]
        for text, categories, budget in cases:
            with self.subTest(text=text):
                s = ConversationManager().local([{"role": "user", "text": text}])
                self.assertEqual(s.requirements.categories, categories)
                self.assertEqual(s.requirements.budget_rub, budget)
                self.assertFalse(s.blocked_categories)

    def test_unreadable_and_unsupported_feature(self):
        for text, expected in [
            ("мне нужно крееееееessсс слооооо", "Не удалось разобрать"),
            ("братишка кароче мне еще лампа нужна такая чтоб минимум тысяча люмен была", "люменах"),
        ]:
            s = ConversationManager().local([{"role": "user", "text": text}])
            self.assertIn(expected, s.decision_questions()[0]["text"])

    def test_context_and_city_alias(self):
        history = [{"role": "user", "text": "Кресло красное в Азове"},
                   {"role": "assistant", "text": "Какой бюджет?"},
                   {"role": "user", "text": "подыщи мне стол от 100 см"}]
        s = ConversationManager().local(history)
        self.assertEqual(s.requirements.categories, ("table",))
        self.assertEqual(s.requirements.city_id, "azov")
        self.assertFalse(s.preferences)
        history += [{"role": "assistant", "text": "Какой бюджет?"},
                    {"role": "user", "text": "в тагане братан"}]
        self.assertEqual(ConversationManager().local(history).requirements.city_id, "taganrog")
