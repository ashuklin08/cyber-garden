"use strict";

const byId = (id) => document.getElementById(id);
const money = (value) => new Intl.NumberFormat("ru-RU").format(value) + " ₽";
const query = byId("query");
const messages = byId("messages");
const starter = byId("starter");
const evidence = byId("evidence");
const initialEvidence = evidence.firstElementChild.cloneNode(true);
const results = byId("results");
const reset = byId("reset");
const send = byId("send");
const errorBox = byId("error");
const history = [];
let busy = false;
let lastOkFingerprint = null;

// Dictation only edits the composer. Dialogue and network operations stay in submit().
const voiceInput = (() => {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const button = byId("voice");
  const label = byId("voice-label");
  const status = byId("voice-status");
  let session = null;
  const errors = {
    "not-allowed": "Разрешите доступ к микрофону в настройках браузера.",
    "service-not-allowed": "Сервис распознавания запрещён настройками браузера. Вы можете ввести запрос текстом.",
    "audio-capture": "Не удалось обнаружить микрофон. Проверьте его подключение и настройки.",
    "no-speech": "Речь не распознана. Попробуйте ещё раз.",
    network: "Распознавание речи временно недоступно. Вы можете ввести запрос текстом.",
    "language-not-supported": "Русский язык недоступен для распознавания в этом браузере. Введите запрос текстом.",
    aborted: "Запись остановлена.",
  };
  function sync() {
    const recording = session !== null && !session.cancelled;
    button.disabled = !Recognition || busy || !!session?.stopping || !!session?.cancelled;
    button.setAttribute("aria-pressed", String(recording));
    const title = recording ? "Остановить запись" : "Голосовой ввод";
    button.setAttribute("aria-label", title);
    label.textContent = title;
    button.title = title;
    send.disabled = busy || session !== null;
  }
  function render(current, interim) {
    const parts = current.parts.filter(part => part.final || interim).map(part => part.text);
    const spoken = parts.join(" ").trim();
    query.value = spoken ? current.base + (current.base && !/\s$/.test(current.base) ? " " : "") + spoken : current.base;
  }
  function finish(current) {
    if (session !== current) return;
    if (!current.cancelled) {
      render(current, false);
      status.textContent = current.error || (current.parts.some(part => part.final && part.text)
        ? "Речь распознана. Проверьте текст перед отправкой" : errors["no-speech"]);
    }
    session = null;
    query.readOnly = false;
    sync();
  }
  function stop() {
    if (!session || session.stopping || session.cancelled) return;
    const current = session;
    current.stopping = true;
    render(current, false);
    status.textContent = "Завершаю запись…";
    sync();
    try { current.recognition.stop(); } catch { finish(current); }
  }
  function cancel() {
    if (!session) return;
    const current = session;
    current.cancelled = true;
    query.value = current.base;
    query.readOnly = false;
    status.textContent = "";
    sync();
    try { current.recognition.abort(); } catch { finish(current); }
  }
  function start() {
    if (!Recognition || busy || session) return;
    let current;
    try {
      const recognition = new Recognition();
      current = {recognition, base: query.value, parts: [], stopping: false, cancelled: false, error: null};
      session = current;
      recognition.lang = "ru-RU";
      recognition.continuous = true;
      recognition.interimResults = true;
      recognition.maxAlternatives = 1;
      recognition.onstart = () => {
        if (session === current && !current.stopping && !current.cancelled) status.textContent = "Слушаю…";
      };
      recognition.onresult = event => {
        if (session !== current || current.cancelled || current.error) return;
        // Results are a cumulative list; replace changed slots instead of appending them.
        current.parts.length = event.results.length;
        for (let i = event.resultIndex; i < event.results.length; i++) {
          current.parts[i] = {text: event.results[i][0].transcript.trim(), final: event.results[i].isFinal};
        }
        render(current, !current.stopping);
      };
      recognition.onerror = event => {
        if (session !== current || current.cancelled) return;
        current.error = errors[event.error] || errors.network;
        render(current, false);
        status.textContent = current.error;
      };
      recognition.onend = () => finish(current);
      query.readOnly = true;
      status.textContent = "Слушаю…";
      sync();
      recognition.start();
    } catch {
      if (current) { current.error = errors.network; finish(current); }
      else status.textContent = errors.network;
    }
  }
  button.addEventListener("click", () => session ? stop() : start());
  if (!Recognition) status.textContent = "Голосовой ввод доступен в поддерживаемом браузере. Вы можете ввести запрос текстом.";
  sync();
  return {sync, stop, cancel, isActive: () => session !== null,
    explain: () => { status.textContent = "Сначала завершите запись кнопкой микрофона, затем отправьте текст."; }};
})();

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function post(path, data) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 125000);
  try {
    const response = await fetch(path, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(data), signal: controller.signal,
    });
    const body = await response.json().catch(() => null);
    if (!response.ok) throw new Error(body?.error || (response.status === 429
      ? "Слишком много запросов. Попробуйте чуть позже." : "Сервис временно недоступен."));
    if (!body || typeof body !== "object") throw new Error("Сервер вернул непонятный ответ.");
    const timings = Object.fromEntries((response.headers?.get("Server-Timing") || "").split(",")
      .map(value => /^\s*(gemma|resolve|search|total);dur=(\d+(?:\.\d+)?)\s*$/.exec(value))
      .filter(Boolean).map(([, stage, duration]) => [stage, Number(duration)]));
    return {...body, timings};
  } catch (error) {
    if (controller.signal.aborted) throw new Error("Сервер не ответил вовремя. Попробуйте отправить запрос ещё раз.");
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

function bubble(role, text) {
  const note = element("div", "bubble " + role);
  note.append(element("div", "speaker", role === "user" ? "Вы" : "Комплект"),
    element("p", null, text));
  return note;
}

function seconds(ms) {
  return (ms / 1000).toLocaleString("ru-RU", {maximumFractionDigits: 3}) + " с";
}

function renderEvidence(data) {
  const list = element("dl", "conditions");
  for (const requirement of data.requirements || []) {
    const value = requirement.value;
    const row = element("div");
    row.append(element("dt", null, requirement.label),
      element("dd", value === null ? "awaits" : null,
        value === null ? "Требует уточнения" : typeof value === "number" ? money(value) : value));
    list.append(row);
  }
  evidence.replaceChildren(list.firstElementChild ? list : initialEvidence.cloneNode(true));
  markCity(data.requirements?.find(row => row.label === "Город самовывоза")?.value);
  if (data.eligible?.length) {
    const funnel = element("div", "funnel-box");
    funnel.append(element("h3", "funnel-head", "После обязательных фильтров"));
    if (Number.isInteger(catalogTotal)) funnel.append(element("p", "model-trace", "Всего в каталоге · " + catalogTotal));
    for (const {category, count} of data.eligible) {
      const row = element("div", "funnel-row");
      row.append(element("span", null, category));
      if (catalogTotal > 0 && Number.isInteger(count)) {
        const meter = element("progress", "funnel-track");
        meter.max = Math.max(catalogTotal, count); meter.value = count;
        meter.setAttribute("aria-label", category + " после фильтрации");
        row.append(meter);
      }
      row.append(element("strong", null, count));
      funnel.append(row);
    }
    if (data.response?.status === "ok") {
      const kind = singleItemResult(data.response) ? "Товаров" : "Комплектов";
      funnel.append(element("p", "model-trace", kind + " в подборе · " + data.response.options.length));
    }
    evidence.append(funnel);
  }
  const trace = data.trace;
  if (trace?.model) evidence.append(element("p", "model-trace", trace.model));
  if (trace?.category_source) evidence.append(element("p", "model-trace", "Категории · " + trace.category_source));
  const timing = data.timings || {};
  const telemetry = element("div", "telemetry-grid");
  function metric(label, value) {
    if (value == null) return;
    const panel = element("div", "tele-card");
    panel.append(element("strong", null, value), element("span", null, label));
    telemetry.append(panel);
  }
  const gemmaTime = timing.gemma > 0 ? timing.gemma
    : trace?.model && trace.model !== "Локальный разбор" ? trace.elapsed_ms : null;
  for (const [label, ms] of [["Gemma API", gemmaTime], ["Поиск", timing.search], ["Ответ сервера", timing.total]]) {
    if (Number.isFinite(ms) && ms > 0) metric(label, seconds(ms));
  }
  if (Number.isInteger(trace?.prompt_tokens) && Number.isInteger(trace?.completion_tokens)) {
    metric("Токенов", trace.prompt_tokens + trace.completion_tokens);
  }
  if (telemetry.firstElementChild) evidence.append(telemetry);
  if (data.preferences?.length) {
    evidence.append(element("p", "model-trace", "Пожелания · " +
      [...new Set(data.preferences.map(({text}) => text))].join(", ")));
  }
}

const photoPath = /^products\/[a-zA-Z0-9_-]+\.(png|jpe?g|webp)$/i;
let catalogPhotos = {};
let catalogTotal = null;
let confirmedCity = null;
const cityButtons = new Map();
function markCity(city) {
  confirmedCity = city;
  for (const [name, button] of cityButtons) button.setAttribute("aria-pressed", String(city === name));
}

function productPhoto(container, item, photo) {
  if (typeof photo?.path !== "string" || !photoPath.test(photo.path)) return;
  const image = element("img");
  image.src = "/visual-images/" + photo.path;
  image.alt = (photo.synthetic ? "Демонстрационное фото: " : "") + item.title;
  image.loading = "lazy";
  const badge = photo.synthetic ? element("span", "photo-badge", "Демонстрационное фото") : null;
  image.addEventListener("error", () => { image.remove(); badge?.remove(); });
  container.append(image);
  if (badge) container.append(badge);
}

const selected = new Map();
const textBundleUpdates = new Set(), photoBundleUpdates = new Set();
const dock = byId("bundle-dock"), bundleDetail = byId("bundle-detail"), bundleToggle = byId("bundle-toggle");
let toastTimer;
function notify(text) {
  const toast = byId("toast");
  toast.hidden = false; toast.textContent = text;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { toast.hidden = true; }, 2800);
}

function renderBundle() {
  dock.hidden = selected.size === 0;
  if (dock.hidden) {
    bundleDetail.hidden = true;
    bundleToggle.setAttribute("aria-expanded", "false");
  }
  byId("bundle-count").textContent = selected.size + " " + plural(selected.size, "товар", "товара", "товаров");
  byId("bundle-total").textContent = money([...selected.values()].reduce((sum, item) => sum + item.price_rub, 0));
  const list = byId("bundle-items");
  list.replaceChildren();
  for (const [id, item] of selected) {
    const row = element("li");
    const remove = element("button", "bundle-remove", "Убрать");
    remove.type = "button";
    remove.setAttribute("aria-label", "Убрать: " + item.title);
    remove.addEventListener("click", () => { selected.delete(id); renderBundle(); notify("Товар убран"); });
    row.append(element("span", "bundle-item-name", item.title), element("strong", null, money(item.price_rub)), remove);
    list.append(row);
  }
  for (const updates of [textBundleUpdates, photoBundleUpdates]) {
    for (const update of updates) update();
  }
}

function bundleAction(id, item, updates) {
  const button = element("button", "add-bundle");
  button.type = "button";
  function update() {
    const picked = selected.has(id);
    button.textContent = picked ? "В комплекте" : "В мой комплект";
    button.setAttribute("aria-pressed", String(picked));
    button.className = picked ? "add-bundle selected" : "add-bundle";
  }
  updates.add(update);
  update();
  button.addEventListener("click", () => {
    if (selected.has(id)) { selected.delete(id); notify("Товар убран"); }
    else { selected.set(id, item); notify("Добавлено в мой комплект"); }
    renderBundle();
  });
  return button;
}

bundleToggle.addEventListener("click", () => {
  bundleDetail.hidden = !bundleDetail.hidden;
  bundleToggle.setAttribute("aria-expanded", String(!bundleDetail.hidden));
});
byId("bundle-clear").addEventListener("click", () => { selected.clear(); renderBundle(); notify("Комплект очищен"); });

function plural(count, one, few, many) {
  if (count % 100 >= 11 && count % 100 <= 14) return many;
  if (count % 10 === 1) return one;
  return count % 10 >= 2 && count % 10 <= 4 ? few : many;
}

const conditionLabel = item => item?.condition_label || item?.condition || "";

function buildOption(option, index, data) {
  const card = element("article", "pane option");
  const summary = element("div", "option-summary");
  const covers = element("div", "option-cover");
  for (const id of option.listing_ids) {
    const cover = element("div", "card-photo photo-placeholder");
    productPhoto(cover, data.items[id], Object.hasOwn(catalogPhotos, id) ? catalogPhotos[id] : null);
    covers.append(cover);
  }
  card.append(covers);
  summary.append(element("h3", "micro", "ВАРИАНТ " + (index + 1)));
  const price = element("div", "price-row");
  price.append(element("strong", "total", money(option.total_rub)));
  if (Number.isFinite(data.budget_rub) && option.total_rub <= data.budget_rub) {
    price.append(element("p", "reserve", option.total_rub === data.budget_rub
      ? "Ровно в бюджете" : "Запас " + money(data.budget_rub - option.total_rub)));
  }
  summary.append(price);
  if (data.budget_rub > 0) {
    const meter = element("progress", "budget-track");
    meter.max = data.budget_rub; meter.value = option.total_rub;
    meter.setAttribute("aria-label", "Сумма варианта относительно бюджета подбора");
    summary.append(meter);
  }
  const itemList = element("ul", "items");
  for (const id of option.listing_ids) {
    const item = data.items[id];
    const row = element("li");
    const content = element("div");
    content.append(element("p", "category", item.category),
      element("p", "item-title", item.title),
      element("p", "item-feature", [item.city, item.feature].filter(Boolean).join(" · ")),
      element("p", "listing-id", "ID " + id));
    if (conditionLabel(item)) content.append(element("span", "listing-condition", conditionLabel(item)));
    const actions = element("div", "listing-actions");
    actions.append(bundleAction(id, item, textBundleUpdates));
    content.append(actions);
    row.append(content, element("strong", "item-price", money(item.price_rub)));
    itemList.append(row);
  }
  const details = element("details", "proof");
  details.append(element("summary", null, "Почему подходит"),
    element("p", null, option.explanation),
    element("p", "model-trace", "ID в каталоге · " + option.listing_ids.join(", ")));
  card.append(summary, itemList, details);
  return card;
}

function jsonDetails(response) {
  const details = element("details", "json-details");
  details.append(element("summary", null, "JSON для автопроверки"),
    element("pre", null, JSON.stringify(response, null, 2)));
  return details;
}

function singleItemResult(response) {
  return response.options.length > 0 && response.options.every(option => option.listing_ids.length === 1);
}

let visibleResult = null;
let localCondition = "", resultSort = "recommended";
const conditionInput = byId("quick-condition");

function refreshConditions(data) {
  const conditions = new Set(Object.values(data?.items || {}).map(conditionLabel).filter(Boolean));
  byId("quick-condition-control").hidden = conditions.size === 0;
  const any = element("option", null, "Любое"); any.value = "";
  conditionInput.replaceChildren(any);
  for (const condition of conditions) {
    const entry = element("option", null, condition);
    entry.value = condition;
    conditionInput.append(entry);
  }
  if (!conditions.has(localCondition)) localCondition = "";
  conditionInput.value = localCondition;
}

conditionInput.addEventListener("change", () => {
  localCondition = conditionInput.value;
  if (visibleResult?.response.status === "ok") renderResults(visibleResult);
});

function validatedReply(data) {
  if (Array.isArray(data.clarification) && data.clarification.length &&
      data.clarification.every(text => typeof text === "string")) return true;
  const response = data.response;
  if (response?.status === "no_match" && typeof response.message === "string") return false;
  if (response?.status === "clarify" && Array.isArray(response.questions) && response.questions.length &&
      response.questions.every(question => typeof question?.text === "string")) return false;
  if (response?.status === "ok" && Array.isArray(response.options) && response.options.length &&
      response.options.every(option => Number.isFinite(option?.total_rub) &&
        Array.isArray(option.listing_ids) && option.listing_ids.length && option.listing_ids.every(id => {
          const item = data.items?.[id];
          return typeof id === "string" && typeof item?.title === "string" && Number.isFinite(item.price_rub);
        }))) return false;
  throw new Error("Сервер вернул неподдерживаемый результат. Попробуйте отправить запрос ещё раз.");
}

function renderResults(data) {
  const response = data.response;
  textBundleUpdates.clear();
  results.replaceChildren();
  results.hidden = false;
  if (response.status === "ok") {
    const bar = element("div", "result-heading");
    const title = element("div");
    const single = singleItemResult(response);
    const options = response.options.filter(option =>
      !localCondition || option.listing_ids.every(id => conditionLabel(data.items[id]) === localCondition));
    if (resultSort !== "recommended") {
      const direction = resultSort === "cheap" ? 1 : -1;
      options.sort((left, right) => direction * (left.total_rub - right.total_rub));
    }
    const count = options.length;
    const filtered = count !== response.options.length;
    title.append(element("h2", null, single ? "Подходящие товары" : "Подходящие комплекты"),
      element("p", null, filtered ? "Показано " + count + " из " + response.options.length
        : single ? "Найдено " + count + " " + plural(count, "объявление", "объявления", "объявлений")
        : (count === 1 ? "Найден 1 комплект" : "Найдено " + count + " " + plural(count, "комплект", "комплекта", "комплектов"))));
    const sort = element("div", "sort-group");
    sort.setAttribute("role", "group");
    sort.setAttribute("aria-label", "Сортировка вариантов");
    for (const [value, label] of [["recommended", "По подбору"], ["cheap", "Сначала дешевле"], ["expensive", "Сначала дороже"]]) {
      const button = element("button", resultSort === value ? "active" : null, label);
      button.type = "button";
      button.id = "sort-" + value;
      button.setAttribute("aria-pressed", String(resultSort === value));
      button.addEventListener("click", () => {
        resultSort = value;
        renderResults(data);
        byId(button.id).focus();
      });
      sort.append(button);
    }
    bar.append(title, sort);
    const grid = element("div", "option-grid");
    options.forEach((option, index) => grid.append(buildOption(option, index, data)));
    results.append(bar, count ? grid : element("p", "filter-empty", "Снимите локальные фильтры, чтобы увидеть варианты."));
  }
  if (response.status === "no_match") {
    renderNoMatch(response.message);
  } else if (response.status === "clarify") {
    results.append(element("p", null, response.questions.map(question => question.text).join("\n")));
  }
  results.append(jsonDetails(response));
}

function renderNoMatch(message) {
  const panel = element("div", "results-empty");
  panel.append(element("h2", null, "Подходящих вариантов нет"));
  for (const paragraph of message.split("\n\n")) {
    if (!paragraph.startsWith("Проверенные варианты:\n")) {
      panel.append(element("p", null, paragraph));
      continue;
    }
    panel.append(element("h3", null, "Проверенные варианты"));
    const options = element("ol", "recovery-options");
    for (const line of paragraph.split("\n").slice(1)) {
      const offer = /^([1-3])\. (.+)$/.exec(line);
      if (!offer) continue;
      const item = element("li");
      const button = element("button", "subtle", "Выбрать вариант " + offer[1]);
      button.type = "button";
      button.addEventListener("click", () => submit("Вариант " + offer[1]));
      item.append(element("p", null, offer[2]), button);
      options.append(item);
    }
    panel.append(options);
  }
  results.append(panel);
}

function assistantReply(response, unchanged = false) {
  if (response.status === "clarify") return response.questions.map((question) => question.text).join("\n");
  if (response.status === "no_match") return response.message;
  const single = singleItemResult(response);
  if (unchanged) return "Подходящие " + (single ? "товары" : "комплекты") +
    " не изменились: подтверждённые условия и результат совпадают с предыдущим подбором.";
  return (single ? "Нашёл подходящие объявления. Самый доступный вариант — "
    : "Подобрал комплект с самовывозом. Самый доступный вариант — ") +
    money(Math.min(...response.options.map(option => option.total_rub))) + ". Можно сравнить позиции ниже.";
}

async function submit(text) {
  if (voiceInput.isActive()) { voiceInput.explain(); return; }
  if (busy || !text.trim()) return;
  if (/^(?:(?:начни|начать|начнем)\s+)?новый\s+диалог[.!?\s]*$/i.test(text.trim())) resetDialogue();
  if (history.length >= 18) {
    errorBox.textContent = "Достигнут лимит диалога. Начните новый диалог.";
    errorBox.hidden = false;
    return;
  }
  busy = true;
  voiceInput.sync();
  const turn = {role: "user", text: text.trim()};
  errorBox.hidden = true;
  send.disabled = true;
  reset.disabled = true;
  send.textContent = "Проверяем…";
  starter.hidden = true;
  visibleResult = null;
  textBundleUpdates.clear();
  results.replaceChildren();
  results.hidden = true;
  const user = bubble("user", turn.text);
  const waiting = bubble("assistant", "Извлекаю условия и проверяю каталог…");
  waiting.classList.add("waiting");
  messages.append(user, waiting);
  messages.scrollTop = messages.scrollHeight;
  try {
    const data = await post("/api/buy", {messages: [...history, turn]});
    const clarification = validatedReply(data);
    const fingerprint = data.response?.status === "ok" ? JSON.stringify({
      options: data.response.options.map(({listing_ids, total_rub}) => ({listing_ids, total_rub})),
      requirements: data.requirements, preferences: data.preferences,
    }) : null;
    const reply = clarification ? data.clarification.join("\n")
      : assistantReply(data.response, fingerprint !== null && fingerprint === lastOkFingerprint);
    if (fingerprint !== null) lastOkFingerprint = fingerprint;
    else if (data.response?.status === "no_match") lastOkFingerprint = null;
    history.push(turn, {role: "assistant", text: reply});
    query.value = "";
    waiting.replaceWith(bubble("assistant", reply));
    messages.scrollTop = messages.scrollHeight;
    renderEvidence(data);
    if (clarification) {
      refreshConditions(null);
      results.replaceChildren();
      results.hidden = true;
    } else {
      visibleResult = data;
      refreshConditions(data);
      renderResults(data);
    }
    reset.hidden = false;
    send.textContent = clarification || data.response.status === "clarify" ? "Ответить" : "Уточнить подбор";
  } catch (error) {
    user.remove();
    waiting.remove();
    query.value = turn.text;
    starter.hidden = history.length > 0;
    errorBox.textContent = error instanceof TypeError
      ? "Нет соединения с локальным сервером. Проверьте, что демо запущено." : error.message;
    errorBox.hidden = false;
    send.textContent = history.length ? "Продолжить" : "Подобрать";
  } finally {
    busy = false;
    send.disabled = false;
    reset.disabled = false;
    voiceInput.sync();
  }
}

byId("composer").addEventListener("submit", (event) => {
  event.preventDefault();
  submit(query.value);
});
query.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    submit(query.value);
  }
});
for (const group of ["prompts", "hero-chips"]) {
  byId(group).addEventListener("click", (event) => {
    const button = event.target.closest("button[data-prompt]");
    if (button) submit(button.dataset.prompt);
  });
}
function resetDialogue() {
  voiceInput.cancel();
  history.length = 0;
  lastOkFingerprint = null;
  visibleResult = null; resultSort = "recommended";
  textBundleUpdates.clear();
  refreshConditions(null); markCity(null);
  messages.replaceChildren(starter);
  starter.hidden = false;
  reset.hidden = true;
  errorBox.hidden = true;
  results.hidden = true;
  results.replaceChildren();
  evidence.replaceChildren(initialEvidence.cloneNode(true));
  send.textContent = "Подобрать";
  query.value = "";
  query.focus();
}
reset.addEventListener("click", resetDialogue);

fetch("/api/catalog").then((response) => response.ok ? response.json() : null).then((catalog) => {
  if (!catalog) return;
  catalogPhotos = catalog.photos || {};
  if (Number.isInteger(catalog.total)) {
    catalogTotal = catalog.total;
    const label = byId("catalog-label");
    label.textContent = "Каталог · " + catalogTotal.toLocaleString("ru-RU") + " " +
      plural(catalogTotal, "объявление", "объявления", "объявлений");
    label.hidden = false;
  }
  const bar = byId("city-bar");
  for (const city of catalog.cities || []) {
    const button = element("button", null, city);
    button.type = "button";
    button.addEventListener("click", () => submit("Город " + city));
    cityButtons.set(city, button);
    bar.append(button);
  }
  bar.hidden = !cityButtons.size;
  markCity(confirmedCity);
  if (visibleResult) { renderEvidence(visibleResult); renderResults(visibleResult); }
}).catch(() => {});

// Optional photo mode owns no dialogue state. Its errors cannot escape into submit().
(() => {
  const toggle = byId("visual-toggle"), panel = byId("visual-panel");
  const input = byId("visual-file"), preview = byId("visual-preview");
  const search = byId("visual-search"), remove = byId("visual-remove");
  const status = byId("visual-status"), cards = byId("visual-results");
  const readiness = byId("visual-readiness"), examples = byId("visual-examples");
  const setup = byId("visual-setup"), setupHint = byId("visual-setup-hint");
  const recheck = byId("visual-recheck");
  let file = null, objectURL = null, pending = false, selecting = false, controller = null, generation = 0;
  let exampleController = null, healthGeneration = 0, healthPending = false, exampleButtons = [];
  let setupTimer, preparing = false;
  function controls() {
    search.disabled = !file || pending || selecting || healthPending || preparing; remove.disabled = !file && !selecting; input.disabled = pending;
    recheck.disabled = pending || selecting || healthPending;
    for (const button of exampleButtons) button.disabled = pending || selecting || healthPending;
    search.textContent = pending ? "Ищу похожие товары…" : "Найти похожие товары";
  }
  function clear() {
    generation++;
    controller?.abort(); controller = null; pending = false; file = null;
    exampleController?.abort(); exampleController = null; selecting = false;
    if (objectURL) URL.revokeObjectURL(objectURL);
    objectURL = null; preview.removeAttribute("src"); preview.hidden = true; input.value = "";
    preview.onload = preview.onerror = null;
    photoBundleUpdates.clear(); cards.replaceChildren(); controls();
    status.textContent = "Фото удалено. Можно продолжить обычный поиск или выбрать другое изображение.";
  }
  async function chooseExample(url) {
    if (pending || selecting) return;
    clear(); selecting = true; controls();
    const selectedGeneration = generation;
    exampleController = new AbortController();
    const selectedController = exampleController;
    const timeout = setTimeout(() => selectedController.abort(), 15000);
    status.textContent = "Загружаю демо-пример…";
    try {
      const response = await fetch(url, {signal: exampleController.signal});
      if (!response.ok) throw new Error("Example unavailable");
      const blob = await response.blob();
      if (generation !== selectedGeneration) return;
      choose([blob]);
      status.textContent = "Демо-пример выбран. Нажмите «Найти похожие товары».";
    } catch {
      if (generation === selectedGeneration) status.textContent = "Не удалось загрузить пример. Выберите фото с компьютера.";
    } finally {
      clearTimeout(timeout);
      if (generation === selectedGeneration) { selecting = false; exampleController = null; controls(); }
    }
  }
  async function refreshReadiness(retry = false) {
    if (healthPending || pending || selecting) return;
    clearTimeout(setupTimer);
    healthPending = true; controls();
    const check = ++healthGeneration;
    if (!preparing) {
      readiness.textContent = "Проверяю готовность визуального поиска…";
      readiness.setAttribute("data-state", "checking");
    }
    const health = new AbortController();
    const timeout = setTimeout(() => health.abort(), 10000);
    try {
      const response = await fetch(retry ? "/api/visual-status?retry=1" : "/api/visual-status", {signal: health.signal});
      const data = await response.json();
      if (check !== healthGeneration) return;
      if (!response.ok) throw new Error("Readiness unavailable");
      preparing = ["not_started", "checking", "preparing", "downloading", "verifying", "building_index", "calibrating"].includes(data.status);
      if (preparing) {
        const progress = Number.isFinite(data.downloaded_bytes) && data.downloaded_bytes >= 0
          && data.total_bytes > 0 && data.downloaded_bytes <= data.total_bytes
          ? " · " + Math.floor(data.downloaded_bytes / data.total_bytes * 100) + "%" : "";
        readiness.textContent = (data.message || "Подготавливаем поиск по фото…") + progress;
        readiness.setAttribute("data-state", data.status);
        setup.hidden = true;
        if (!panel.hidden) setupTimer = setTimeout(() => refreshReadiness(), 1500);
        return;
      }
      readiness.textContent = data.message + (data.coverage ? " Покрытие: " + data.coverage.indexed + " из " + data.coverage.total + " объявлений." : "");
      readiness.setAttribute("data-state", data.status);
      setup.hidden = data.status === "ready";
      setupHint.textContent = data.setup_hint || "Проверьте установку и подготовку визуального модуля.";
      if (data.status !== "ready") setup.open = true;
      examples.replaceChildren(); exampleButtons = [];
      for (const sample of data.examples || []) {
        if (!/^\/visual-demo\/(table|chair|lamp|landscape)$/.test(sample.url)) continue;
        const button = element("button", "visual-example"); button.type = "button";
        const image = element("img"); image.src = sample.url; image.alt = "Синтетический демо-пример: " + sample.label;
        image.loading = "lazy";
        button.append(image, element("span", null, sample.label));
        button.addEventListener("click", () => chooseExample(sample.url));
        exampleButtons.push(button); examples.append(button);
      }
      controls();
    } catch {
      if (check === healthGeneration) {
        preparing = false;
        readiness.textContent = "Готовность фото-поиска не подтверждена. Обычный поиск доступен.";
        readiness.setAttribute("data-state", "unavailable");
        setup.hidden = false; setup.open = true; setupHint.textContent = "Проверьте запуск сервера и подготовку модуля по README.";
      }
    } finally { clearTimeout(timeout); healthPending = false; controls(); }
  }
  recheck.addEventListener("click", () => refreshReadiness(true));
  function choose(files) {
    clear();
    if (!files || files.length !== 1 || !files[0].size || files[0].size > 5 * 1024 * 1024) {
      status.textContent = "Выберите одно изображение размером до 5 МБ."; return;
    }
    if (files[0].type && !["image/jpeg", "image/png", "image/webp"].includes(files[0].type)) {
      status.textContent = "Выберите изображение JPEG, PNG или WebP."; return;
    }
    try {
      file = files[0];
      objectURL = URL.createObjectURL(file);
      const selectedURL = objectURL;
      preview.onload = () => {
        if (objectURL !== selectedURL) return;
        if (preview.naturalWidth > 2048 || preview.naturalHeight > 2048) {
          clear(); status.textContent = "Максимальная сторона изображения — 2048 пикселей.";
        }
      };
      preview.onerror = () => {
        if (objectURL !== selectedURL) return;
        clear(); status.textContent = "Не удалось прочитать фото. Выберите JPEG, PNG или WebP.";
      };
      preview.src = objectURL; preview.hidden = false; controls();
      status.textContent = "Фото выбрано. Нажмите «Найти похожие товары».";
    } catch {
      clear(); status.textContent = "Предпросмотр недоступен. Вы можете воспользоваться обычным поиском.";
    }
  }
  toggle.addEventListener("click", () => {
    const opening = panel.hidden;
    panel.hidden = !opening; toggle.setAttribute("aria-expanded", String(opening));
    if (opening) {
      voiceInput.stop();
      panel.scrollIntoView?.({block: "nearest"});
      refreshReadiness();
    }
    else clearTimeout(setupTimer);
  });
  input.addEventListener("change", () => choose(input.files));
  const drop = byId("visual-drop");
  drop.addEventListener("dragover", event => event.preventDefault());
  drop.addEventListener("drop", event => { event.preventDefault(); if (!pending) choose(event.dataTransfer.files); });
  remove.addEventListener("click", clear);
  search.addEventListener("click", async () => {
    if (!file || pending || preparing || healthPending) return;
    const requestGeneration = generation;
    pending = true; controls(); photoBundleUpdates.clear(); cards.replaceChildren();
    status.textContent = "Ищу похожие товары…";
    let requestTimeout;
    try {
      controller = new AbortController();
      const requestController = controller;
      requestTimeout = setTimeout(() => requestController.abort(), 40000);
      const body = new FormData(); body.append("image", file);
      const response = await fetch("/api/visual-search", {method: "POST", body, signal: controller.signal});
      const result = await response.json();
      if (generation !== requestGeneration) return;
      if (!response.ok || result.status !== "ok") {
        status.textContent = result.message || "Визуальный поиск недоступен. Вы можете воспользоваться обычным поиском.";
        if (result.status === "visual_unavailable") {
          setup.hidden = false; setup.open = true;
          setupHint.textContent = result.setup_hint || "Повторите подготовку модуля по README.";
        }
        return;
      }
      status.textContent = result.message + " Покрытие каталога: " + result.coverage.indexed + " из " + result.coverage.total + " объявлений.";
      for (const item of result.results) {
        const card = element("article", "visual-card");
        const cover = element("div", "card-photo photo-placeholder");
        productPhoto(cover, item, {path: item.image_path, synthetic: item.synthetic});
        const content = element("div", "option-summary");
        content.append(element("h3", null, item.rank + ". " + item.title),
          element("p", null, item.category + " · " + item.city), element("strong", null, money(item.price_rub)));
        if (typeof item.listing_id === "string") {
          content.append(element("p", "listing-id", "ID " + item.listing_id),
            bundleAction(item.listing_id, item, photoBundleUpdates));
        }
        card.append(cover, content);
        cards.append(card);
      }
    } catch {
      if (generation === requestGeneration) status.textContent = "Визуальный поиск временно недоступен. Вы можете воспользоваться обычным поиском.";
    } finally {
      clearTimeout(requestTimeout);
      if (generation === requestGeneration) { pending = false; controller = null; controls(); }
    }
  });
  controls();
})();
