"""Offline, bounded lexical relevance. Seller text is only passive search data.

Each wish scores query concept coverage in [0, 10000]. Repetition and document
length cannot boost coverage. Wishes are averaged per category; bundle scores
sum category scores (equivalent to their mean for a fixed requested composition).
There is no network, model loading, instruction execution or fact extraction.
"""

import re
from itertools import product
import heapq

from buyer.anchors import WORDS
from buyer.query_parser import normalize

# Shared with the existing OpenSearch positive-wish expansion.
ALIASES = {"деревян": "дерево массив сосна", "мягк": "мягкое мягкая обивка",
           "светодиод": "LED", "углов": "угловой", "эргоном": "эргономика поясничная"}
CONCEPTS = {
    "wood": ("дерев", "массив", "сосн", "дуб", "бук"),
    "soft": ("мягк", "обивк"),
    "led": ("светодиод", "led"),
    "ergonomic": ("эргоном", "пояснич"),
    "comfortable": ("удобн", "комфорт"),
}
STOP = set("и с из для на в по хочу нужен нужна нужно желательно предпочтительно "
           "теперь очень бы был было хорошо свет".split())
ENDINGS = re.compile(r"(?:ыми|ими|ого|ему|ому|ыми|ими|ая|яя|ое|ее|ые|ие|ый|ий|ой|ую|юю|ых|их|ам|ям|ами|ями|ов|ев|ом|ем|а|я|ы|и|е|у|ю)$")


def tokens(text):
    text = normalize(text)
    for pattern in WORDS.values():
        text = pattern.sub(" ", text)
    result = set()
    for word in re.findall(r"[а-яa-z0-9]+", text):
        if word in STOP:
            continue
        concept = next((key for key, stems in CONCEPTS.items()
                        if any(word.startswith(stem) for stem in stems)), None)
        result.add(concept or (ENDINGS.sub("", word) if len(word) > 4 else word))
    return frozenset(result)


def listing_scores(groups, preferences):
    """Return internal fixed-point scores for every strictly eligible listing."""
    wishes = {}
    for pref in preferences:
        if (pref.category not in groups or pref.attribute
                or re.search(r"\b(?:не|без)\b", normalize(pref.text))):
            continue
        query = tokens(pref.text)
        if query:
            wishes.setdefault(pref.category, set()).add(query)
    scores = {}
    for category, items in groups.items():
        queries = wishes.get(category, ())
        for item in items:
            if not queries:
                scores[item.id] = 0
                continue
            document = tokens(item.title) | tokens(item.description)
            scores[item.id] = sum(10000 * len(query & document) // len(query)
                                  for query in queries) // len(queries)
    return scores


def ranked_bundles(groups, budget, scores):
    """Exact top three, O(product(group sizes) * categories), O(3) result space.

    With <=250 listings and <=3 categories there are at most 84*83*83=578676
    combinations. Never truncate candidates; unaffordable high scores do not
    hide affordable alternatives. Budget is checked before retaining a result.
    """
    def candidates():
        for items in product(*groups):
            total = sum(item.price_rub for item in items)
            if total <= budget:
                ids = tuple(item.id for item in items)
                yield (-sum(scores[item.id] for item in items), total, ids, items)

    for _, total, _, items in heapq.nsmallest(3, candidates()):
        yield total, items
