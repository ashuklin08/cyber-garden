"""Verified alternatives for a failed strict search."""

from collections import OrderedDict
from dataclasses import dataclass, replace
from decimal import Decimal
from itertools import product
import re
from threading import Lock

from buyer.anchors import WORDS
from buyer.contracts import CATEGORIES, CITIES, FEATURES, Comparison
from buyer.query_parser import CITIES_RE, normalize, parse_turn
from buyer.selection import CHECK, LABELS, describe_condition, filter_listings, matches, rubles, select


HEADING = "Проверенные варианты:"
NO_OPTIONS = "Среди проверенных изменений нет варианта, который даёт результат."
DECLINED = "Оставил исходные условия."
CHOOSE = "Уточните выбор: назовите номер варианта, чтобы подтвердить все его изменения, или укажите новые условия."


@dataclass(frozen=True)
class Change:
    field: str
    before: object
    after: object


@dataclass(frozen=True)
class Suggestion:
    changes: tuple[Change, ...]
    minimum_total_rub: int
    reason: str

    def apply(self, requirements):
        values = {change.field: change.after for change in self.changes}
        for category in set(requirements.categories) - set(values.get("categories", requirements.categories)):
            field = FEATURES[category]
            values[field] = None if category == "chair" else ()
        return replace(requirements, **values)


def _condition_for(item, rules):
    if matches(item, rules):
        return rules, Decimal(0)
    if item.feature is None:
        return None
    if item.category == "chair":
        return item.feature, Decimal(1)
    if item.feature > 10**12:
        return None  # The requirements contract cannot represent this boundary.
    value = Decimal(item.feature)
    amended = []
    distance = Decimal(0)
    for rule in rules:
        if CHECK[rule.operation](value, rule.value):
            amended.append(rule)
            continue
        operation = {"lt": "lte", "gt": "gte"}.get(rule.operation, rule.operation)
        amended.append(Comparison(operation, value))
        distance += abs(value - rule.value) or 1
    return tuple(dict.fromkeys(amended)), distance


def _group_options(items, rules):
    # Every possible recovery boundary is witnessed by an actual product.
    # Reuse the exact cheapest eligible price for each such condition.
    choices = {candidate for item in items if (candidate := _condition_for(item, rules)) is not None}
    ordered = sorted(items, key=lambda item: (item.price_rub, item.id))
    return [
        (condition, distance, next(item.price_rub for item in ordered if matches(item, condition)))
        for condition, distance in sorted(choices, key=lambda pair: (pair[1], repr(pair[0])))
    ]


def _cause(listings, requirements, selection):
    single_item = len(requirements.categories) == 1
    if selection.minimum_total_rub is not None:
        minimum = selection.minimum_total_rub
        target = "подходящий товар" if single_item else "полный комплект"
        return (f"Бюджета {rubles(requirements.budget_rub)} не хватает на {target}. "
                f"В городе {CITIES[requirements.city_id]} самый доступный {target} "
                f"с вашими характеристиками стоит {rubles(minimum)}. "
                f"До него не хватает {rubles(minimum - requirements.budget_rub)}.")
    by_category, _ = filter_listings(listings, requirements)
    available = {item.category for item in listings}
    reasons = []
    for category, count in selection.eligible_counts.items():
        if count:
            continue
        name = CATEGORIES[category]
        if category not in available:
            reasons.append(f"«{name}» отсутствует во всём текущем каталоге")
        elif not by_category[category]:
            reasons.append(f"«{name}» отсутствует в городе {CITIES[requirements.city_id]}")
        elif all(item.feature is None for item in by_category[category]):
            reasons.append(f"для «{name}» не указана характеристика «{LABELS[category]}»")
        else:
            condition = describe_condition(category, requirements.condition(category))
            reasons.append(f"для «{name}» нет объявлений с условием «{LABELS[category]} {condition}»")
    if not reasons:
        return selection.response["message"]
    intro = "Подходящих товаров нет. " if single_item else "Полный комплект недоступен. "
    return intro + " ".join(reason[0].upper() + reason[1:] + "." for reason in reasons)


def build_recovery_suggestions(listings, requirements, preferences, selection):
    if (selection.response["status"] != "no_match" or requirements.budget_rub is None
            or not requirements.city_id or not requirements.categories):
        return ()
    by_scope = {}
    for item in listings:
        by_scope.setdefault((item.city_id, item.category), []).append(item)
    original = requirements.categories
    compositions = [original]
    if len(original) > 1:
        compositions.extend(tuple(key for key in original if key != removed) for removed in original)
    cause = _cause(listings, requirements, selection)
    best = {}
    for city in CITIES:
        options = {category: _group_options(by_scope.get((city, category), ()), requirements.condition(category))
                   for category in original}
        city_change = city != requirements.city_id
        for categories in compositions:
            removed = categories != original
            base = []
            if city_change:
                base.append(Change("city_id", requirements.city_id, city))
            if removed:
                base.append(Change("categories", original, categories))
            for combination in product(*(options[key] for key in categories)):
                minimum = sum(option[2] for option in combination)
                extra = max(0, minimum - requirements.budget_rub)
                distance = sum(option[1] for option in combination)
                edited = tuple(i for i, option in enumerate(combination) if option[1])
                changes = len(base) + bool(extra) + len(edited)
                if not changes:
                    continue
                # One closest alternative per city, composition and set of edits.
                family = (city, removed, bool(extra), tuple(categories[i] for i in edited))
                rank = (changes, extra, distance, city_change, removed, minimum)
                if family in best and best[family][0] <= rank:
                    continue
                edits = [*base, *(Change(FEATURES[categories[i]], requirements.condition(categories[i]),
                                          combination[i][0]) for i in edited)]
                if extra:
                    edits.insert(0, Change("budget_rub", requirements.budget_rub, minimum))
                best[family] = (rank, Suggestion(tuple(edits), minimum, cause))
    verified = []
    budget_alternative = None
    for _, suggestion in sorted(best.values(), key=lambda pair: pair[0]):
        budget_only = tuple(change.field for change in suggestion.changes) == ("budget_rub",)
        if len(verified) == 3 and not budget_only:
            continue
        if len(suggestion.changes) > 1 and any(
                _affordable(listings, replace(suggestion, changes=suggestion.changes[:i] + suggestion.changes[i+1:])
                            .apply(requirements))
                for i in range(len(suggestion.changes))):
            continue
        result = select(listings, suggestion.apply(requirements), preferences)
        if result.response["status"] == "ok" and result.minimum_total_rub == suggestion.minimum_total_rub:
            if len(verified) < 3:
                verified.append(suggestion)
            if budget_only:
                budget_alternative = suggestion
            if len(verified) == 3 and (budget_alternative or selection.minimum_total_rub is None):
                break
    if budget_alternative and budget_alternative not in verified:
        verified[-1] = budget_alternative
    return tuple(verified)


def _affordable(listings, requirements):
    _, eligible = filter_listings(listings, requirements)
    return all(eligible.values()) and sum(group[0].price_rub for group in eligible.values()) <= requirements.budget_rub


def _describe(change):
    field, after = change.field, change.after
    if field == "budget_rub":
        return f"увеличить бюджет до {rubles(after)} (+{rubles(after - change.before)})"
    if field == "city_id":
        return f"выбрать город {CITIES[after]}"
    if field == "categories":
        removed, = set(change.before) - set(after)
        return f"убрать из запроса «{CATEGORIES[removed]}»"
    category = next(key for key, name in FEATURES.items() if name == field)
    return f"изменить условие «{LABELS[category]}» на «{describe_condition(category, after)}»"


def recovery_response(listings, requirements, preferences, selection, note=None):
    if selection.response["status"] != "no_match" or requirements.budget_rub is None:
        return selection.response, ()
    suggestions = build_recovery_suggestions(listings, requirements, preferences, selection)
    cause = suggestions[0].reason if suggestions else _cause(listings, requirements, selection)
    paragraphs = [note, cause] if note else [cause]
    if suggestions:
        lines = []
        for i, suggestion in enumerate(suggestions, 1):
            edits = ", ".join(map(_describe, suggestion.changes))
            lines.append(f"{i}. {edits[0].upper() + edits[1:]}. "
                         f"Минимальная стоимость подбора {rubles(suggestion.minimum_total_rub)}.")
        paragraphs.extend([HEADING + "\n" + "\n".join(lines),
                           "Чтобы принять предложение, напишите «Вариант» и его номер."])
    else:
        paragraphs.append(NO_OPTIONS)
    return {"status": "no_match", "message": "\n\n".join(paragraphs)}, suggestions


def offer_block(text):
    return next((part for part in text.split("\n\n") if part.startswith(HEADING + "\n")), None)


DECLINE = re.compile(r"(?:нет(?:[,.!]?\s*оставь(?:те)?\s+как\s+было)?|"
                     r"оставь(?:те)? как было|без изменений|отказываюсь|ничего не меняй)")
UNDO = re.compile(r"(?:верни(?:те)? (?:как было|прежние условия)|отмени(?:те)? (?:изменение|последнее изменение))")
NUMBERED = re.compile(r"(?:(?:давай(?:те)?|выбираю|принимаю) )?(?:вариант )?([1-3])(?: вариант)?")
ORDINAL = re.compile(r"(?:(?:давай(?:те)?|выбираю) )?(первый|второй|третий)(?: вариант)?")
BUDGET = re.compile(r"(?:(?:увелич\w*|подним\w*|можно) (?:бюджет )?|бюджет )(?:до )?"
                    r"(\d+(?: \d{3})*)(?: ₽| руб\w*| рублей)?")
REMOVE = re.compile(r"убер(?:и|ите) (?:из комплекта )?([а-я-]+)")
MEASURE_REPLY = re.compile(r"(?:(?:можно|давай|ширина(?: стола)?|температура(?: света)?) )?"
                           r"(?:до |от |ровно |не более |не менее )?\d+(?:[.,]\d+)?\s*(?:см|к)")
AFFIRM = {"да", "согласен", "согласна", "подходит", "давай"}


def _single_edit(text, current):
    if match := BUDGET.fullmatch(text):
        return "budget_rub", int(match[1].replace(" ", ""))
    city = re.sub(r"^(?:нет,? |а )?(?:(?:давай(?:те)?|выбираю|лучше|тогда|можно|город) )?(?:в )?", "", text)
    for key, pattern in CITIES_RE.items():
        if pattern.fullmatch(city) or normalize(CITIES[key]) == city:
            return "city_id", key
    if match := REMOVE.fullmatch(text):
        removed = next((key for key, pattern in WORDS.items() if pattern.fullmatch(match[1])), None)
        if removed in current.categories:
            return "categories", tuple(key for key in current.categories if key != removed)
    if MEASURE_REPLY.fullmatch(text):
        field = "width_cm" if text.endswith("см") else "color_temperature_k"
        category = "table" if field == "width_cm" else "lamp"
        if category not in current.categories:
            return None
        measurement = re.sub(r"^(?:можно|давай) ", "", text)
        # The unit identifies the intended item even in a multi-item bundle.
        turn = parse_turn(f"{CATEGORIES[category]} {measurement}", set(), current)
        if field in turn.values:
            parsed = [Comparison(op, value) for op, value in turn.values[field]]
            old = getattr(current, field)
            directions = {"lt": "upper", "lte": "upper", "gt": "lower", "gte": "lower", "eq": "exact"}
            changed = {directions[rule.operation] for rule in parsed}
            kept = [] if "exact" in changed else [r for r in old if directions[r.operation] not in changed | {"exact"}]
            return field, tuple((*kept, *parsed))
    if text in {"без регулировки", "без регулировки высоты", "с регулировкой", "с регулировкой высоты"}:
        return "adjustable_height", text.startswith("с ")
    return None


def interpret_reply(text, current, suggestions):
    clean = normalize(text).strip(" .!")
    if DECLINE.fullmatch(clean):
        return DECLINED
    if clean in AFFIRM:
        return suggestions[0] if len(suggestions) == 1 and len(suggestions[0].changes) == 1 else CHOOSE
    ordinal = ORDINAL.fullmatch(clean)
    number = NUMBERED.fullmatch(clean)
    if number or ordinal:
        index = int(number[1]) - 1 if number else ("первый", "второй", "третий").index(ordinal[1])
        return suggestions[index] if index < len(suggestions) else CHOOSE
    answer = clean.rstrip("?").rstrip()
    if re.fullmatch(r"\d+(?: \d{3})*", answer):
        edit = ("budget_rub", int(answer.replace(" ", "")))
    else:
        edit = _single_edit(answer, current)
    if edit:
        field, value = edit
        candidates = [
            item for item in suggestions if any(
                change.field == field and (set(change.after) == set(value)
                    if field in {"width_cm", "color_temperature_k"} else change.after == value)
                for change in item.changes)
        ]
        if candidates and clean.endswith("?"):
            return CHOOSE
        if single := next((item for item in candidates if len(item.changes) == 1), None):
            return single
        if candidates:
            return CHOOSE
    if re.match(r"^(?:да[, ]|выбер|выбира|вариант|первый|второй|третий)", clean):
        return CHOOSE
    return None


class RecoveryHistory:
    """Small server-local cache for prior facts that only Gemma could resolve."""

    def __init__(self):
        self._lock = Lock()
        self._states = OrderedDict()

    @staticmethod
    def _key(messages):
        return tuple(message["text"] for message in messages)

    def remember(self, messages, response, query, suggestions):
        if not suggestions:
            return
        key = (*self._key(messages), response["message"])
        with self._lock:
            self._states[key] = (query, suggestions)
            self._states.move_to_end(key)
            if len(self._states) > 128:
                self._states.popitem(last=False)

    def find(self, messages):
        with self._lock:
            return self._states.get(self._key(messages))
