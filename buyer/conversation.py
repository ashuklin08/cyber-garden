"""Replay buyer turns and reconcile local facts with a single Gemma result."""

import re
from dataclasses import dataclass, replace

from buyer.anchors import WORDS, accessory_context, categories_from_dialog
from buyer.contracts import Requirements, read_requirements
from buyer.query_parser import ATTRIBUTES, CITIES_RE, CORE, OWNER, Preference, normalize, parse_turn, tighten
from buyer.preferences import category_text, evolve, grounded

EMPTY = dict(city_id=None, budget_rub=None, categories=None, width_cm=[],
             adjustable_height=None, color_temperature_k=[])
MISSING_CATEGORY = "В запросе есть товар вне каталога"
LIMIT_OPERATIONS = {"gte": "lower", "gt": "lower", "lte": "upper", "lt": "upper", "eq": "equal"}
QUESTIONS = {
    "city_id": r"город\w*|самовывоз\w*",
    "budget_rub": r"бюджет\w*|верхн\w+\s+цен\w*|максималь\w+\s+цен\w*|сколько\s+готов\w*",
    "categories": r"что\s+(?:найти|подобрать|купить)|категори\w*|состав\w*",
}
ANSWER_QUESTIONS = {**QUESTIONS, "width_cm": r"ширин\w*", "adjustable_height": r"регулиров\w*",
                    "color_temperature_k": r"температур\w*|кельвин\w*"}
ATTRIBUTE_QUESTIONS = {
    "width_cm": "Какую ширину стола указать? Можно назвать точное значение или диапазон в сантиметрах.",
    "adjustable_height": "Нужна ли регулировка высоты кресла?",
    "color_temperature_k": "Какую температуру света указать? Можно назвать значение или диапазон в кельвинах.",
}
CATEGORY_ALTERNATIVE = re.compile(r"\b(?:или|либо)\b")
CITY_NAMES = "(?:" + "|".join(pattern.pattern for pattern in CITIES_RE.values()) + ")"
CITY_ALTERNATIVE = re.compile(rf"{CITY_NAMES}\s+(?:или|либо)\s+(?:в\s+)?{CITY_NAMES}")
SAFE_WORDS = re.compile(
    r"(?:(?:мне|нам|я|ты|бы|в|во|на|из|для|под|с|со|и|а|но|за|по|к|у|не|да|"
    r"до|от|см|мм|м|р|руб|тыс|лишь|только|еще|уже|ровно|точно|строго|чуть|"
    r"было|хорошо|теперь|лучше|все|то|же|очень|также|плюс|минимум|максимум|"
    r"больше|меньше|шире|выше|ниже|более|менее|хотя|убери|добавь|помоги|найди|подбери|"
    r"хочу|ищу|нуж\w*|бюджет\w*|город\w*|самовывоз\w*|рубл\w*|тысяч\w*|"
    r"миллиметр\w*|сантиметр\w*|метр\w*|кельвин\w*|температур\w*|ширин\w*|"
    r"высот\w*|регулиров\w*|регулиру\w*|фиксированн\w*|комплект\w*|покупк\w*|"
    r"желательн\w*|предпочтительн\w*|возможност\w*|идеал\w*|важ\w*|"
    r"сними|ограничени\w*|принципиальн\w*|без|компьютерн\w*|письменн\w*|"
    r"офисн\w*|удобн\w*|эргономичн\w*|мягк\w*|настольн\w*|тепл\w*|"
    r"холодн\w*|свет\w*|деревянн\w*|углов\w*|одн\w*|две|два|три|"
    r"пять|десять|пятнадцать|полтор\w*|двадцать|полностью))$"
)


@dataclass(frozen=True)
class LocalState:
    requirements: Requirements
    preferences: tuple[Preference, ...]
    confirmed: frozenset[str]
    unresolved: frozenset[str]
    ambiguous_attributes: frozenset[str]
    needs_model: bool
    blocked_categories: bool = False

    def questions(self):
        values = self.requirements
        return tuple(key for key in CORE if getattr(values, key) in (None, ()))

    def attribute_questions(self):
        return tuple(question for attr, question in ATTRIBUTE_QUESTIONS.items()
                     if OWNER[attr] in self.requirements.categories and attr in self.unresolved
                     and getattr(self.requirements, attr) in (None, ()))


def _pending(text):
    return {key for sentence in normalize(text).split("?")[:-1]
            for key, pattern in ANSWER_QUESTIONS.items() if re.search(pattern, sentence)}


def _category_update(text, categories, prompted):
    text = CITY_ALTERNATIVE.sub(" ", text)
    mentions = {key for key, pattern in WORDS.items() if pattern.search(text)}
    anchor = categories_from_dialog([{"role": "user", "text": text}], categories)
    if anchor.reason == MISSING_CATEGORY:
        return True, None
    if not mentions:
        return False, None
    if CATEGORY_ALTERNATIVE.search(text):
        left, right = CATEGORY_ALTERNATIVE.split(text, maxsplit=1)
        if any(pattern.search(left) for pattern in WORDS.values()) and any(
                pattern.search(right) for pattern in WORDS.values()):
            return True, None
    if any(accessory_context(text[:match.start()]) for pattern in WORDS.values()
           for match in pattern.finditer(text)):
        return False, None
    if prompted:
        anchor = categories_from_dialog([{"role": "user", "text": text}])
    if anchor.categories:
        return True, list(anchor.categories)
    if categories and re.search(r"\b(?:без|убери|не\s+нуж\w*)\b", text) and mentions == set(categories):
        return True, []
    return False, None


def _new_bounds(previous, incoming):
    incoming = tighten(incoming)
    if not incoming or any(op == "eq" for op, _ in incoming):
        return incoming
    directions = {LIMIT_OPERATIONS[op] for op, _ in incoming}
    if directions == {"lower", "upper"}:
        return incoming
    return [rule for rule in previous if LIMIT_OPERATIONS[rule[0]] not in directions | {"equal"}] + incoming


def _filter_preferences(preferences, categories):
    return tuple(dict.fromkeys(item for item in preferences
                               if item.category is None or item.category in categories))


class ConversationManager:
    """Stateless replay also works when CLI launches a fresh process per turn."""

    def local(self, messages):
        values = EMPTY.copy()
        confirmed = set()
        unresolved = set()
        ambiguous_attributes = set()
        preferences = ()
        pending = set()
        needs_model = False
        composition_history = []
        blocked_categories = False
        for message in messages:
            text = normalize(message["text"])
            if message["role"] == "assistant":
                pending = _pending(text)
                continue

            current = read_requirements(values)
            turn = parse_turn(message["text"], pending, current)
            composition = category_text(text)
            composition_history.append({"role": "user", "text": composition})
            anchor = categories_from_dialog(composition_history)
            recognized, categories = _category_update(composition, current.categories, "categories" in pending)
            mentions = any(pattern.search(composition) for pattern in WORDS.values())
            was_blocked = blocked_categories
            blocked_categories = bool(anchor.pending_unknown)
            if blocked_categories:
                # Retain the known part for replay, but never confirm a partial set.
                values["categories"] = list(anchor.supported)
                turn.uncertain.add("categories")
                turn.values.pop("categories", None)
            elif was_blocked and anchor.categories:
                turn.values["categories"] = list(anchor.categories)
            elif recognized:
                turn.values["categories"] = categories
            elif mentions or "categories" in pending:
                turn.uncertain.add("categories")
            pending = set()

            # Unknown prose or unsupported syntax is sent to Gemma with the original dialogue.
            room = text
            for pattern in (*CITIES_RE.values(), *WORDS.values()):
                room = pattern.sub(" ", room)
            needs_model |= turn.needs_model or any(
                not SAFE_WORDS.fullmatch(word) for word in re.findall(r"[а-яa-z]+", room)
            )
            if turn.uncertain:
                needs_model = True

            unresolved.update(turn.uncertain - turn.values.keys())
            ambiguous_attributes.update(turn.ambiguous_attributes - turn.values.keys())
            confirmed.difference_update(turn.uncertain)
            for item in turn.preferences:
                attr = item.attribute
                if attr and attr not in turn.values and attr not in unresolved:
                    confirmed.add(attr)
            new_categories = turn.values.get("categories")
            if new_categories is not None:
                removed = set(current.categories) - set(new_categories)
                for field, category in OWNER.items():
                    if category in removed and field not in turn.values:
                        turn.values[field] = EMPTY[field]
            for field, value in turn.values.items():
                if field in ("width_cm", "color_temperature_k"):
                    value = _new_bounds(values[field], value)
                values[field] = value
                confirmed.add(field)
                unresolved.discard(field)
                ambiguous_attributes.discard(field)
                if field in ATTRIBUTES and (value is None or value == []):
                    preferences = tuple(item for item in preferences if item.attribute != field)
            preferences = evolve(preferences, turn.preferences, text)
            if new_categories is not None:
                preferences = _filter_preferences(preferences, new_categories)

        # An uncertain last edit cannot reuse the old value in a local-only result.
        for field in unresolved:
            values[field] = EMPTY[field]
        requirements = read_requirements(values)
        return LocalState(requirements, _filter_preferences(preferences, requirements.categories),
                          frozenset(confirmed), frozenset(unresolved),
                          frozenset(ambiguous_attributes), needs_model, blocked_categories)

    def merge(self, local, model, messages=()):
        fields = {field: getattr(local.requirements, field)
                  for field in local.confirmed | local.ambiguous_attributes}
        requirements = replace(model.requirements, **fields)
        if local.blocked_categories:
            requirements = replace(requirements, categories=())
        preferences = (evolve(tuple(pref for pref in local.preferences if pref.attribute),
                              grounded((*local.preferences, *model.preferences), messages), "")
                       if messages else evolve(local.preferences, model.preferences, ""))
        return replace(local, requirements=requirements, preferences=_filter_preferences(
            preferences, requirements.categories
        ))

    def resolve(self, messages, gemma, *, require_model):
        local = self.local(messages)
        if not require_model and not local.needs_model:
            return local, None
        model, trace = gemma(messages, full_history=local.needs_model)
        return self.merge(local, model, messages), trace
