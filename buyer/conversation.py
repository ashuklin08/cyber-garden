"""Replay buyer turns and reconcile local facts with a single Gemma result."""

import re
from dataclasses import dataclass, replace

from buyer.anchors import WORDS, accessory_context, categories_from_dialog
from buyer.contracts import CATEGORIES, CITIES, Requirements, read_requirements
from buyer.query_parser import (ATTRIBUTES, BUDGET_WORD, CITIES_RE, CORE, MONEY, OWNER,
                                CITY_CLEAR, PROPER_LOCATION, PURCHASE_REQUEST, RELATIVE_BUDGET,
                                TEMPERATURE, WIDTH, MEASURE, PRICE_PER_ITEM,
                                unsupported_measurement, Preference, normalize, parse_turn, single_item_quote, tighten)
from buyer.preferences import category_text, evolve, grounded, purpose_wishes
from buyer.recovery import (DECLINE, DECLINED, UNDO, Suggestion, interpret_reply, offer_block,
                            recovery_response)
from buyer.selection import QUESTIONS as SEARCH_QUESTIONS, select

EMPTY = dict(city_id=None, budget_rub=None, categories=None, width_cm=[],
             adjustable_height=None, color_temperature_k=[])
MISSING_CATEGORY = "В запросе есть товар вне каталога"
UNSUPPORTED_MESSAGE = "Такого каталога нет. Пожалуйста, выберите из имеющихся"
UNSUPPORTED_CITY_MESSAGE = "Такого города нет. Пожалуйста, выберите из имеющихся. Доступны: "
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
    r"хочу|ищу|найти|подобрать|купить|покажи|покажите|поищи|давай|давайте|"
    r"тогда|пусть|будет|можно|нет|нуж\w*|бюджет\w*|город\w*|самовывоз\w*|рубл\w*|тысяч\w*|"
    r"миллиметр\w*|сантиметр\w*|метр\w*|кельвин\w*|температур\w*|ширин\w*|"
    r"высот\w*|регулиров\w*|регулиру\w*|фиксированн\w*|комплект\w*|покупк\w*|"
    r"желательн\w*|предпочтительн\w*|возможност\w*|идеал\w*|важ\w*|"
    r"сними|ограничени\w*|принципиальн\w*|без|компьютерн\w*|письменн\w*|"
    r"офисн\w*|удобн\w*|эргономичн\w*|мягк\w*|настольн\w*|тепл\w*|"
    r"холодн\w*|свет\w*|деревянн\w*|углов\w*|одн\w*|две|два|три|"
    r"пять|десять|пятнадцать|полтор\w*|двадцать|полностью))$"
)


RESET_DIALOG = re.compile(r"^(?:(?:начни|начать|начнем)\s+)?новый\s+диалог[.!?\s]*$")
OFF_TOPIC = re.compile(r"\b(?:рецепт\w*|приготов\w*|погод\w*|анекдот\w*|гороскоп\w*|теорем\w*|пифагор\w*|переведи|напиши\s+код)\b")
REJECT_BUNDLE = re.compile(r"\b(?:нахера|зачем|нафига|почему)\s+(?:мне\s+)?комплект\b|\bне\s+нужен\s+комплект\b")
SHOPPING_CONTEXT = re.compile(
    r"\b(?:нуж\w*|хочу|ищу|подбери|подобрать|найди|куп\w*|добавь|убери|оставь|"
    r"бюджет\w*|город\w*|ширин\w*|регулиров\w*|температур\w*|пожелан\w*|"
    r"комплект\w*|вариант\w*|как\s+раньше|тот\s+же|все\s+то\s+же|table|desk|chair|armchair|lamp)\b")
OFF_TOPIC_QUESTION = ("Я специализируюсь на подборе товаров из каталога и не отвечаю на вопросы "
                      "по другим темам. Могу помочь выбрать стол, кресло или лампу. Что вас интересует?")
DOMAIN_QUESTION = ("Не удалось понять запрос. Пожалуйста, напишите отдельно, какой товар "
                   "вам нужен, город самовывоза и максимальный бюджет.")
CHANGE_QUESTION = ("Не удалось понять, что нужно изменить. Пожалуйста, перепишите это "
                   "условие другими словами.")
PRICE_QUESTION = "Указанное число — цена одного товара или общий бюджет на весь комплект?"
ITEM_ANSWER = re.compile(r"(?:(?:цена|стоимость)\s+)?(?:один товар|одного товара|за (?:один товар|штуку))")
TOTAL_ANSWER = re.compile(r"(?:общий(?: бюджет)?|бюджет|(?:на|за) (?:все|весь комплект)|весь комплект)")
REPEAT_SEARCH = re.compile(r"(?:(?:давай(?:те)?|тогда)\s+)?(?:покажи(?:те)?|выведи|показывай)"
                           r"(?:\s+(?:мне|тогда))?\s+(?:варианты|результаты)\s*[.!?]*")
COMPOSITION_CONTEXT = re.compile(r"\b(?:комплект\w*|добав\w*|плюс|также|еще|не|без)\b")


_THOUSAND_TYPO = re.compile(
    r"(\b\d+(?:[.,]\d+)?\s+)тисяч(?:а|и|у)?\b"
    r"(?=\s*(?:$|[.,!?;]|₽|руб\w*\b|за\s+вс[её]\b|в\s+))", re.I)
_CITY_TYPO = re.compile(
    r"(?P<prefix>\b(?:в|из|город)\s+|^\s*)(?P<city>азев[еи]?|растов[еа]?)\b"
    r"(?![-\w]|\s+велик)", re.I)
_TABLE_TYPO = re.compile(
    r"(^\s*|\b(?:нужен|ищу|купить|подбери|найди)\s+)столь\b"
    r"(?=\s*(?:$|[.,!?;]|в\s+|до\s+\d|за\s+\d))", re.I)
_PRODUCT_TYPO = re.compile(
    r"(?P<prefix>\b(?:нужен|нужна|нужно|хочу|ищу|купить|подбери|найди|покажи|добавь)\s+)"
    r"(?P<word>[а-яё]{3,9})\b", re.I)
_INITIAL_PRODUCT_TYPO = re.compile(
    r"(?P<prefix>^\s*)(?P<word>[а-яё]{3,9})\b"
    r"(?=\s*(?:$|[.,!?;]|в\s+|до\s+|за\s+|бюджет\b|не\s+(?:шире|дороже)))", re.I)
_PRODUCT_FORMS = {
    "table": ("стол", "стола", "столу", "столом", "столе", "столы"),
    "chair": ("кресло", "кресла", "креслу", "креслом", "кресле", "кресла"),
    "lamp": ("лампа", "лампу", "лампы", "лампе", "лампой"),
}
# These are ordinary words or unsupported products one edit away from a category.
_PRODUCT_LOOKALIKES = {
    "столь", "сток", "столб", "рампа", "рампу", "рампы", "рампе",
    "лапа", "лапу", "лапой", "лама", "ламу", "ламы",
}


def _one_typo(left, right):
    """Accept one insertion, deletion, substitution or adjacent transposition."""
    if left == right or abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        differences = [index for index, pair in enumerate(zip(left, right)) if pair[0] != pair[1]]
        return len(differences) == 1 or (len(differences) == 2
            and differences[1] == differences[0] + 1
            and left[differences[0]] == right[differences[1]]
            and left[differences[1]] == right[differences[0]])
    shorter, longer = (left, right) if len(left) < len(right) else (right, left)
    for index in range(len(longer)):
        if longer[:index] + longer[index + 1:] == shorter:
            return True
    return False


def _correct_product(match):
    word = match["word"].lower().replace("ё", "е")
    if word in _PRODUCT_LOOKALIKES or any(pattern.fullmatch(word) for pattern in WORDS.values()):
        return match[0]
    matches = {category for category, forms in _PRODUCT_FORMS.items()
               if any(_one_typo(word, form) for form in forms)}
    if len(matches) != 1:
        return match[0]
    canonical = {"table": "стол", "chair": "кресло", "lamp": "лампа"}[matches.pop()]
    return match["prefix"] + canonical


def _correct_buyer_text(text):
    # «столь» is also an adverb. Require an item slot with a clear right boundary.
    text = re.sub(r"\bлумп([ауые])\b", r"ламп\1", text, flags=re.I)
    text = re.sub(r"\b(?:в|из)\s+тагане\b", "в Таганроге", text, flags=re.I)
    text = re.sub(r"\bсотк[ау]\s+руб(?:асов|ликов)\b", "100 рублей", text, flags=re.I)
    if (sum(bool(pattern.search(text)) for pattern in WORDS.values()) > 1
            and re.search(r"\b(?:найди|подбери|подыщи|ищу)\b", text, re.I)):
        text = re.sub(r"\bза\s+(\d+(?:[.,]\d+)?)\s+руб\w*\b",
                      r"бюджет \1 рублей", text, flags=re.I)
    text = _TABLE_TYPO.sub(r"\1стол", text)
    text = _PRODUCT_TYPO.sub(_correct_product, text)
    text = _INITIAL_PRODUCT_TYPO.sub(_correct_product, text)
    text = _THOUSAND_TYPO.sub(r"\1тысяч", text)
    return _CITY_TYPO.sub(lambda m: m["prefix"] + (
        "Азов" if m["city"].lower().startswith("аз") else "Ростов"), text)


def active_dialog(messages):
    """A buyer's explicit reset is a boundary for both local replay and Gemma."""
    boundary = 0
    for index, message in enumerate(messages):
        if message["role"] == "user" and RESET_DIALOG.fullmatch(normalize(message["text"])):
            boundary = index
    return [dict(message, text=_correct_buyer_text(message["text"]))
            if message["role"] == "user" else message for message in messages[boundary:]]


def _buyer_turn(messages, index):
    """Resolve a price-scope answer only against the preceding buyer's quote."""
    original = messages[index]["text"]
    if index < 2 or messages[index - 1]["role"] != "assistant" or messages[index - 2]["role"] != "user":
        return original
    if PRICE_QUESTION not in messages[index - 1]["text"]:
        return original
    answer = normalize(original).strip(" .!?")
    item = ITEM_ANSWER.fullmatch(answer)
    if not item and not TOTAL_ANSWER.fullmatch(answer):
        return original
    quote = single_item_quote(normalize(messages[index - 2]["text"]))
    if not quote:
        return original
    return (f"Только {CATEGORIES[quote.category]}. " if item else "") + f"Бюджет {quote.rubles}"


def _unscoped_price(text):
    if BUDGET_WORD.search(text) or not any(pattern.search(text) for pattern in WORDS.values()):
        return False
    spans = [hit.span() for pattern in (WIDTH, TEMPERATURE, MEASURE) for hit in pattern.finditer(text)]
    for hit in MONEY.finditer(text):
        if any(lo <= hit.start() < hi for lo, hi in spans):
            continue
        if hit["u"] and not PRICE_PER_ITEM.search(text) and re.search(
                r"\b(?:до|не\s+дороже|максимум|не\s+более)\s*$", text[:hit.start()]):
            continue
        # Comparisons without a unit are handled by attribute ambiguity rules.
        if re.search(r"\b(?:до|от|ровно|больше|меньше|шире|уже)\s*$", text[:hit.start()]):
            continue
        return True
    return False


@dataclass(frozen=True)
class LocalState:
    requirements: Requirements
    preferences: tuple[Preference, ...]
    confirmed: frozenset[str]
    unresolved: frozenset[str]
    ambiguous_attributes: frozenset[str]
    needs_model: bool
    blocked_categories: bool = False
    turn_issue: str | None = None
    unsupported_items: tuple[str, ...] = ()
    unsupported_city: str | None = None
    recovery_note: str | None = None
    recovery_handled: bool = False

    def decision_questions(self):
        if any(re.search(r"(.)\1{2,}", item) for item in self.unsupported_items):
            return ({"field": "categories", "text":
                     "Не удалось разобрать название товара. Пожалуйста, напишите его ещё раз."},)
        if self.turn_issue == "lumens":
            return ({"field": "categories", "text":
                     "В каталоге нет данных о яркости в люменах. Подобрать лампу без этого требования?"},)
        if self.turn_issue == "recovery_no_undo":
            return ({"field": "categories", "text":
                     "Нет принятого предложения, которое можно отменить. Текущие условия сохранены. "
                     "Какое условие вы хотите изменить?"},)
        if self.turn_issue == "recovery_stale":
            return ({"field": "categories", "text":
                     "Предложение больше не подтверждается текущим каталогом. Укажите, какое условие изменить."},)
        if self.turn_issue in {"distance", "unsupported_size"}:
            name = "Расстояние" if self.turn_issue == "distance" else "Указанный размер (высота, длина или глубина)"
            return ({"field": "categories", "text": name +
                     " не поддерживается условиями каталога. Уточните запрос без этого ограничения."},)
        if self.unsupported_city:
            return ({"field": "city_id", "text": UNSUPPORTED_CITY_MESSAGE + ", ".join(CITIES.values())
                     + f". Вы указали: «{self.unsupported_city}»."},)
        if self.blocked_categories:
            names = ", ".join(CATEGORIES.values())
            return ({"field": "categories", "text": UNSUPPORTED_MESSAGE + ". Доступны: " + names + "."},)
        if self.turn_issue == "bundle_rejected":
            return ({"field": "categories", "text":
                     "Понял, вам нужен один товар. Что именно показать — стол, кресло или лампу?"},)
        if self.turn_issue == "off_topic":
            return ({"field": "categories", "text": OFF_TOPIC_QUESTION},)
        if self.turn_issue == "ambiguous":
            missing = self.questions()
            if set(missing) == set(CORE):
                return ({"field": "categories", "text": DOMAIN_QUESTION},)
            if missing:
                return tuple({"field": field, "text": SEARCH_QUESTIONS[field]}
                             for field in missing)
            return ({"field": "categories", "text": CHANGE_QUESTION},)
        if self.turn_issue in {"price", "chair_price"}:
            questions = [{"field": "budget_rub", "text": PRICE_QUESTION}]
            if self.turn_issue == "chair_price":
                questions.append({"field": "categories", "text":
                    "В каталоге поддерживается категория «Кресло». Подойдёт ли вам кресло вместо стула?"})
            return tuple(questions)
        return ()

    def questions(self):
        values = self.requirements
        return tuple(key for key in CORE if getattr(values, key) in (None, ()))

    def attribute_questions(self):
        return tuple(question for attr, question in ATTRIBUTE_QUESTIONS.items()
                     if OWNER[attr] in self.requirements.categories and attr in self.unresolved
                     and getattr(self.requirements, attr) in (None, ()))


def _pending(text):
    text = normalize(text)
    questions = {key for sentence in text.split("?")[:-1]
                 for key, pattern in ANSWER_QUESTIONS.items() if re.search(pattern, sentence)}
    # The actual API prompts are often instructions ending with a period.
    questions.update(key for key, prompt in SEARCH_QUESTIONS.items()
                     if normalize(prompt) in text)
    return questions


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


def _recovery_values(requirements):
    values = {key: getattr(requirements, key) for key in EMPTY}
    values["categories"] = list(requirements.categories)
    for field in ("width_cm", "color_temperature_k"):
        values[field] = [[rule.operation, rule.value] for rule in values[field]]
    return values


class ConversationManager:
    """Stateless replay also works when CLI launches a fresh process per turn."""

    def local(self, messages, *, recovery_listings=None, recovery_history=None):
        messages = active_dialog(messages)
        values = EMPTY.copy()
        confirmed = set()
        unresolved = set()
        ambiguous_attributes = set()
        preferences = ()
        pending = set()
        needs_model = False
        composition_history = []
        blocked_categories = False
        semantic_refusal = False
        unsupported_items = ()
        turn_issue = None
        unsupported_city = None
        recovery_message = None
        last_accepted = None
        recovery_handled = False
        recovery_note = None
        for index, message in enumerate(messages):
            text = normalize(message["text"])
            if message["role"] == "assistant":
                recovery_message = offer_block(message["text"]) if recovery_listings is not None else None
                pending = set() if recovery_message else _pending(text)
                if UNSUPPORTED_CITY_MESSAGE.casefold() in text.casefold():
                    source = re.search(r"Вы указали: «([^»]+)»", message["text"])
                    unsupported_city = source[1] if source else "неизвестный город"
                    values["city_id"] = None
                    confirmed.discard("city_id")
                    unresolved.add("city_id")
                # Semantic product recognition is replayed via the backend's
                # refusal, even when the local item grammar missed that product.
                if normalize(UNSUPPORTED_MESSAGE) in text and not blocked_categories:
                    semantic_refusal = True
                    blocked_categories = True
                    unresolved.add("categories")
                    confirmed.discard("categories")
                continue

            previous_issue, turn_issue = turn_issue, None
            recovery_handled = False
            recovery_note = None
            current = read_requirements(values)
            if recovery_message:
                base, base_preferences = current, preferences
                cached = recovery_history.find(messages[:index]) if recovery_history else None
                if cached:
                    previous, suggestions = cached
                    base, base_preferences = previous.requirements, previous.preferences
                else:
                    strict = select(recovery_listings, base)
                    response, suggestions = recovery_response(recovery_listings, base, (), strict)
                    if recovery_message != offer_block(response.get("message", "")):
                        suggestions = ()
                unrelated = bool(suggestions and OFF_TOPIC.search(text)
                                 and not SHOPPING_CONTEXT.search(text)
                                 and not any(pattern.search(text) for pattern in WORDS.values()))
                reaction = interpret_reply(message["text"], base, suggestions)
                if reaction is not None or unrelated:
                    recovery_handled = True
                    prior_categories = current.categories
                    if unrelated:
                        current = base
                        turn_issue = "off_topic"
                    elif isinstance(reaction, Suggestion):
                        revised = reaction.apply(base)
                        checked = select(recovery_listings, revised)
                        if (checked.response["status"] == "ok"
                                and checked.minimum_total_rub == reaction.minimum_total_rub):
                            current = revised
                            last_accepted = (base, current, base_preferences)
                        else:
                            current = base
                            turn_issue = "recovery_stale"
                    else:
                        current = base
                        recovery_note = reaction
                        if not suggestions and reaction != DECLINED:
                            turn_issue = "recovery_stale"
                    values = _recovery_values(current)
                    preferences = _filter_preferences(base_preferences, current.categories)
                    if prior_categories != current.categories:
                        composition_history = [{"role": "user", "text":
                                                ", ".join(CATEGORIES[key] for key in current.categories)}]
                    confirmed.update(EMPTY)
                    unresolved.clear()
                    ambiguous_attributes.clear()
                    if suggestions:
                        unsupported_city = None
                        blocked_categories = semantic_refusal = False
                        unsupported_items = ()
                    pending = set()
                    recovery_message = None
                    continue
            recovery_message = None
            recovery_command = text.strip(" .!?")
            if (recovery_listings is not None and not pending and current.city_id
                    and current.categories and current.budget_rub is not None
                    and DECLINE.fullmatch(recovery_command)):
                recovery_handled = True
                recovery_note = DECLINED
                continue
            if recovery_listings is not None and UNDO.fullmatch(recovery_command):
                if last_accepted is None:
                    turn_issue = "recovery_no_undo"
                    recovery_handled = True
                    pending = set()
                    continue
                before, applied, previous_preferences = last_accepted
                modified = {key: getattr(before, key) for key in EMPTY
                            if getattr(before, key) != getattr(applied, key)}
                if all(getattr(current, key) == getattr(applied, key) for key in modified):
                    restored = replace(current, **modified)
                    values = _recovery_values(restored)
                    if restored.categories != current.categories:
                        composition_history = [{"role": "user", "text":
                                                ", ".join(CATEGORIES[key] for key in restored.categories)}]
                        added = set(restored.categories) - set(current.categories)
                        preferences = tuple(dict.fromkeys((*preferences,
                            *(item for item in previous_preferences if item.category in added))))
                    confirmed.update(modified)
                    unresolved.difference_update(modified)
                    ambiguous_attributes.difference_update(modified)
                    recovery_handled = True
                    recovery_note = "Вернул условия до последнего принятого предложения."
                    last_accepted = None
                    pending = set()
                    continue
                turn_issue = "recovery_stale"
                recovery_handled = True
                continue
            if REJECT_BUNDLE.search(text):
                turn_issue = "bundle_rejected"
                needs_model = True
                pending = set()
                continue
            if RESET_DIALOG.fullmatch(text):
                confirmed.update(EMPTY)
                needs_model = True
                pending = set()
                continue
            if REPEAT_SEARCH.fullmatch(text):
                if previous_issue in {"price", "chair_price", "distance", "unsupported_size", "lumens"}:
                    turn_issue = previous_issue
                pending = set()
                continue
            buyer_text = _buyer_turn(messages, index)
            text = normalize(buyer_text)
            turn = parse_turn(buyer_text, pending, current)
            single_search = turn.single_purchase
            if (single_search and index + 1 < len(messages) and messages[index + 1]["role"] == "assistant"
                    and PRICE_QUESTION in messages[index + 1]["text"]):
                # Older sessions explicitly left the scope open. Replay that decision
                # so either answer can still be applied to the original composition.
                single_search = False
            if "city_id" in turn.values:
                unsupported_city = None
            turn.preferences.extend(purpose_wishes(text, current.categories))
            mentions_product = any(pattern.search(text) for pattern in WORDS.values())
            shopping_signal = (mentions_product or turn.values or turn.preferences or turn.uncertain
                               or SHOPPING_CONTEXT.search(text))
            if (OFF_TOPIC.search(text) and not mentions_product) or not shopping_signal:
                turn_issue = "off_topic" if OFF_TOPIC.search(text) else "ambiguous"
                needs_model = True
                pending = set()
                continue
            if not single_search and _unscoped_price(text):
                turn_issue = "chair_price" if re.search(r"\bстул\w*\b", text) else "price"
                turn.values.pop("budget_rub", None)
                turn.uncertain.discard("budget_rub")
                needs_model = True
            composition = category_text(text)
            switching_product = bool(re.search(
                r"\b(?:а\s+)?что\s+(?:насчет|насчёт|по\s+поводу)\s+", composition))
            switching_product |= bool(
                current.categories and re.search(r"\bподыщи\b", composition)
                and not re.search(r"\b(?:еще|добавь|также)\b", composition))
            new_search = (not turn_issue and PURCHASE_REQUEST.search(text)
                          and not COMPOSITION_CONTEXT.search(text))
            replacement = (categories_from_dialog([{"role": "user", "text": composition}]).categories
                           if new_search or switching_product else None)
            single_search |= bool(new_search and replacement and len(replacement) == 1)
            composition_history.append({"role": "user", "text": composition})
            if single_search:
                composition_history = composition_history[-1:]
            anchor = categories_from_dialog(composition_history)
            recognized, categories = _category_update(composition, () if single_search else current.categories,
                                                       "categories" in pending)
            if semantic_refusal and recognized and categories:
                semantic_refusal = False
                composition_history = [{"role": "user", "text": composition}]
                anchor = categories_from_dialog(composition_history)
            if switching_product and replacement:
                recognized, categories = True, list(replacement)
                for field in ATTRIBUTES:
                    if field not in turn.values:
                        turn.values[field] = EMPTY[field]
                preferences = ()
            mentions = any(pattern.search(composition) for pattern in WORDS.values())
            was_blocked = blocked_categories
            blocked_categories = bool(anchor.pending_unknown) or semantic_refusal
            unsupported_items = anchor.pending_unknown
            if blocked_categories:
                # Retain the known part for replay, but never confirm a partial set.
                values["categories"] = list(anchor.supported)
                turn.uncertain.add("categories")
                turn.values.pop("categories", None)
                if not mentions:
                    for field in ATTRIBUTES:
                        turn.values.pop(field, None)
                        turn.uncertain.discard(field)
                        turn.ambiguous_attributes.discard(field)
                    turn.preferences.clear()
            elif was_blocked and anchor.categories:
                turn.values["categories"] = list(anchor.categories)
            elif recognized:
                turn.values["categories"] = categories
            elif mentions or "categories" in pending:
                turn.uncertain.add("categories")
            if turn_issue in {"price", "chair_price"} and current.categories:
                # An ambiguous item price does not confirm a composition edit.
                turn.values.pop("categories", None)
                turn.uncertain.discard("categories")
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
            unsupported = unsupported_measurement(text)
            if unsupported:
                turn_issue = unsupported
                needs_model = True

            if re.search(r"\bлюмен\w*\b", text):
                turn_issue = "lumens"
                needs_model = True

        # An uncertain last edit cannot reuse the old value in a local-only result.
        preference_categories = tuple(values["categories"] or ())
        for field in unresolved:
            values[field] = EMPTY[field]
        requirements = read_requirements(values)
        return LocalState(requirements, _filter_preferences(preferences,
                          preference_categories if blocked_categories else requirements.categories),
                          frozenset(confirmed), frozenset(unresolved),
                          frozenset(ambiguous_attributes), needs_model, blocked_categories, turn_issue, unsupported_items,
                          unsupported_city, recovery_note, recovery_handled)

    def merge(self, local, model, messages=()):
        # Bind numeric model claims to the actual unit-bearing buyer evidence.
        # A ruble amount elsewhere in the sentence cannot license a distance.
        texts = [normalize(message["text"]) for message in messages if message["role"] == "user"]
        measured = any(MEASURE.search(text) for text in texts)
        if measured:
            budget = model.requirements.budget_rub
            evidence = []
            if "budget_rub" not in local.confirmed:
                for text in texts:
                    turn = parse_turn(text, set(), read_requirements(EMPTY))
                    if "budget_rub" in turn.values and (turn.single_purchase or not _unscoped_price(text)):
                        evidence.append(turn.values["budget_rub"])
            if (budget not in evidence and not
                    (texts and RELATIVE_BUDGET.search(texts[-1]) and not MEASURE.search(texts[-1]))):
                model = replace(model, requirements=replace(model.requirements, budget_rub=local.requirements.budget_rub))
        if measured or any(MONEY.search(text) for text in texts):
            # Local replay retains verified bounds, units, clears and pending answers.
            model = replace(model, requirements=replace(model.requirements,
                            width_cm=local.requirements.width_cm,
                            color_temperature_k=local.requirements.color_temperature_k))
        unknown = tuple(item for item in (*model.requirements.categories, *(model.requested_categories or ()))
                        if item not in CATEGORIES)
        if local.blocked_categories or unknown:
            # Do not let an unsupported product overwrite compatible purchase
            # facts or create preferences for a different supported product.
            requirements = replace(local.requirements, categories=(), **{
                field: getattr(model.requirements, field)
                for field in ("city_id", "budget_rub") if field not in local.confirmed
            })
            history = messages[:-1]
            old_preferences = grounded(model.preferences, history) if history else ()
            preferences = tuple(dict.fromkeys((*local.preferences, *old_preferences)))
            return replace(local, requirements=requirements, preferences=preferences,
                           blocked_categories=True, turn_issue=None,
                           unresolved=local.unresolved | {"categories"},
                           unsupported_items=tuple(dict.fromkeys((*local.unsupported_items, *unknown))))
        if local.turn_issue:
            # Ground preserved model-only wishes in earlier buyer turns, never
            # in the unrelated/ambiguous current text or assistant suggestions.
            history = messages[:-2] if len(messages) > 1 else []
            previous_texts = [normalize(message["text"]) for message in history if message["role"] == "user"]
            budget_evidence = any(re.search(r"\bтыс(?:яч\w*)?\b", text)
                or any(hit["u"] for hit in MONEY.finditer(text))
                or (BUDGET_WORD.search(text) and MONEY.search(text)) for text in previous_texts)
            requirements, unresolved = local.requirements, local.unresolved
            if ("budget_rub" in unresolved and budget_evidence
                    and local.turn_issue not in {"price", "chair_price"}
                    and model.requirements.budget_rub is not None):
                requirements = replace(requirements, budget_rub=model.requirements.budget_rub)
                unresolved = unresolved - {"budget_rub"}
            preferences = grounded((*local.preferences, *model.preferences), history)
            preferences = tuple(pref for pref in local.preferences if pref.category is None) + preferences
            issue = "off_topic" if model.intent == "off_topic" and local.turn_issue == "ambiguous" else local.turn_issue
            return replace(local, requirements=requirements, unresolved=unresolved, turn_issue=issue,
                           preferences=_filter_preferences(preferences, requirements.categories))
        fields = {field: getattr(local.requirements, field)
                  for field in local.confirmed | local.ambiguous_attributes}
        requirements = replace(model.requirements, **fields)
        preferences = (evolve(tuple(pref for pref in local.preferences if pref.attribute),
                              grounded((*local.preferences, *model.preferences), messages), "")
                       if messages else evolve(local.preferences, model.preferences, ""))
        return replace(local, requirements=requirements, preferences=_filter_preferences(
            preferences, requirements.categories
        ))

    def resolve(self, messages, gemma, *, require_model, recovery_listings=None, recovery_history=None):
        messages = active_dialog(messages)
        local = self.local(messages, recovery_listings=recovery_listings, recovery_history=recovery_history)
        if local.recovery_handled:
            return local, None
        if not require_model and not local.needs_model:
            return local, None
        model, trace = gemma(messages, full_history=local.needs_model)
        location = model.requested_location
        last = _buyer_turn(messages, len(messages) - 1)
        raw_city = model.requirements.city_id
        if raw_city is not None and raw_city not in CITIES:
            location = location or dict(name=raw_city, type="city", city_id=raw_city)
            model = replace(model, requirements=replace(model.requirements, city_id=None))
        elif (location is None and raw_city and "city_id" in local.unresolved
              and PROPER_LOCATION.search(last) and not CITIES_RE[raw_city].search(normalize(last))):
            # A new location cue needs semantic evidence before reusing a city.
            model = replace(model, requirements=replace(model.requirements, city_id=None))
        if (location and location.get("type") in {"uncertain", "other"}
                and "city_id" in local.confirmed and "city_id" not in local.unresolved):
            # An empty location guess on a price reply cannot revoke a known city.
            location = None
        if (location and location.get("type") == "clear" and "city_id" in local.confirmed
                and not CITY_CLEAR.search(normalize(last))):
            location = None
        if location is not None:
            city = location.get("city_id")
            if location.get("type") == "city":
                name = normalize(location.get("name") or "")
                matches = [key for key in CITIES if CITIES_RE[key].fullmatch(name)
                           or name == normalize(CITIES[key])]
                city = matches[0] if len(matches) == 1 else None
            # The registry, never a model assertion of support, is authoritative.
            if location.get("type") == "city" and city not in CITIES:
                local = replace(local, unsupported_city=location.get("name") or "неизвестный город",
                                turn_issue=None, confirmed=local.confirmed - {"city_id"})
            elif location.get("type") in {"clear", "uncertain", "other"} or city in CITIES:
                local = replace(local, unsupported_city=None,
                                turn_issue=None if local.turn_issue in {"ambiguous", "off_topic"} else local.turn_issue,
                                requirements=replace(local.requirements, city_id=city if city in CITIES else None),
                                confirmed=local.confirmed | {"city_id"})
        if local.unsupported_city:
            # Merge compatible facts and wishes, then prevent both stale-city
            # selection and an unsupported ID reaching search or presentation.
            merged = self.merge(replace(local, turn_issue=None), model, messages)
            return replace(merged, unsupported_city=local.unsupported_city,
                           requirements=replace(merged.requirements, city_id=None)), trace
        if local.blocked_categories:
            # A model intent label must not override deterministic item evidence.
            return self.merge(local, model, messages), trace
        if model.intent in {"off_topic", "ambiguous"} and not local.turn_issue:
            previous_messages = messages[:-2] if len(messages) > 1 else []
            previous = self.local(previous_messages, recovery_listings=recovery_listings,
                                  recovery_history=recovery_history)
            turn = parse_turn(last, set(), previous.requirements)
            explicit = (turn.values or turn.preferences or REPEAT_SEARCH.fullmatch(normalize(last))
                        or any(pattern.search(last) for pattern in WORDS.values()))
            if not explicit:
                # Semantic classification covers topics outside the small local
                # guard vocabulary. Neither intent nor a guessed category can
                # erase previously confirmed purchase facts.
                preserved = replace(previous, turn_issue=model.intent, needs_model=True)
                return self.merge(preserved, model, messages), trace
        return self.merge(local, model, messages), trace
