"""Conservative local category anchors for the three supported products."""

import re
from dataclasses import dataclass

from buyer.contracts import CATEGORIES

FORMS = {
    "table": r"стол(?:ик(?:а|у|ом|и|ов)?|а|у|ом|е|ы|ов|ами|ах)?",
    "chair": r"кресл(?:о|а|у|ом|е|ы|ам|ами|ах)?|стул(?:а|у|ом|е|ы|ья|ьев|ьям|ьями|ьях)?",
    "lamp": r"ламп(?:а|у|ы|е|ой|ами|ах)?|светильник(?:а|у|ом|е|и|ов|ами|ах)?",
}
WORDS = {key: re.compile(r"(?<!\w)(?:" + forms + r")(?!\w)", re.I)
         for key, forms in FORMS.items()}
UNSUPPORTED = re.compile(
    r"(?<!\w)(?:диван|шкаф|кровать|полк[ауи]|тумб[ауеы]|монитор|компьютер|ноутбук|"
    r"табурет|пуфик|зеркало|телевизор)(?:а|у|ом|е|ы|ов|ами|ах)?(?!\w)", re.I
)
REMOVE = re.compile(r"\b(?:без|убери|исключи|удали|вычеркни|отмени)\s*$", re.I)
ADD = re.compile(r"\b(?:добавь|добавить|плюс|ещё|еще|также)\b", re.I)
ONLY = re.compile(r"\b(?:только|лишь|один|одну|одно)\b", re.I)
AMBIGUOUS = re.compile(r"\b(?:или|либо|может|возможно|если|вместо|лучше|предпочтительнее)\b", re.I)


@dataclass(frozen=True)
class CategoryAnchor:
    categories: tuple[str, ...] | None
    reason: str


def categories_from_dialog(messages):
    """Return an anchor only when all category changes have an explicit local reading."""
    selected = set()
    saw_category = False
    unsupported_requested = False
    for message in messages:
        if message["role"] != "user":
            continue
        text = message["text"].lower().replace("ё", "е")
        mentions = sorted(
            (match.start(), match.end(), key)
            for key, pattern in WORDS.items() for match in pattern.finditer(text)
        )
        if ONLY.search(text):
            unsupported_requested = False
        for match in UNSUPPORTED.finditer(text):
            before = text[max(0, match.start() - 32):match.start()]
            after = text[match.end():match.end() + 32]
            if re.search(r"\b(?:для|под|к|от|на|с|со)\s*$", before):
                continue
            removed = (REMOVE.search(before) or re.search(r"\bне\s+нуж\w*\s*$", before)
                       or re.search(r"^\s*(?:больше\s+)?не\s+нуж", after))
            unsupported_requested = not bool(removed)
        if not mentions:
            continue
        if AMBIGUOUS.search(text):
            return CategoryAnchor(None, "Неоднозначное сравнение товаров")

        adds = set()
        removes = set()
        for start, end, key in mentions:
            before = text[max(0, start - 32):start]
            after = text[end:end + 32]
            if (REMOVE.search(before) or re.search(r"^\s*(?:больше\s+)?не\s+нуж", after)
                    or re.search(r"^\s+уже\s+есть\b", after)):
                removes.add(key)
            else:
                adds.add(key)
        if adds & removes:
            return CategoryAnchor(None, "Противоречивое изменение состава")

        saw_category = True
        if not selected or ONLY.search(text):
            selected = adds
        elif ADD.search(text) or removes:
            selected.update(adds)
        else:
            return CategoryAnchor(None, "Неясно, заменён ли состав комплекта")
        selected.difference_update(removes)

    if unsupported_requested:
        return CategoryAnchor(None, "В запросе есть товар вне каталога")
    if not saw_category or not selected:
        return CategoryAnchor(None, "Категории нужно уточнить")
    return CategoryAnchor(tuple(key for key in CATEGORIES if key in selected), "Явные категории и изменения")
