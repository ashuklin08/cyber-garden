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
  const response = await fetch(path, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(data),
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || "Сервис временно недоступен.");
  return body;
}

function bubble(role, text) {
  const note = element("div", "bubble " + role);
  note.append(element("div", "speaker", role === "user" ? "Вы" : "Комплект"),
    element("p", null, text));
  return note;
}

function traceText(trace) {
  if (trace.model === "Локальный разбор") return trace.model + " · без вызова Gemma";
  const tokens = trace.prompt_tokens !== null && trace.completion_tokens !== null
    ? " · " + (trace.prompt_tokens + trace.completion_tokens) + " токенов" : "";
  return trace.model.split("/").pop() + " · " +
    (trace.elapsed_ms / 1000).toLocaleString("ru-RU", {maximumFractionDigits: 2}) + " с" + tokens;
}

function renderEvidence(data) {
  const list = element("dl", "conditions");
  for (const requirement of data.requirements) {
    const value = requirement.value;
    const row = element("div");
    row.append(element("dt", null, requirement.label),
      element("dd", value === null ? "awaits" : null,
        value === null ? "Требует уточнения" : typeof value === "number" ? money(value) : value));
    list.append(row);
  }
  evidence.replaceChildren(list);
  if (data.eligible.length) {
    const counts = element("p", "counts");
    counts.textContent = "После фильтрации · " +
      data.eligible.map((item) => item.category + " " + item.count).join(" / ");
    evidence.append(counts);
  }
  evidence.append(element("p", "model-trace", traceText(data.trace)));
  evidence.append(element("p", "model-trace", "Категории · " + data.trace.category_source));
  if (data.preferences?.length) {
    evidence.append(element("p", "model-trace", "Пожелания · " +
      [...new Set(data.preferences.map(({text}) => text))].join(", ")));
  }
}

function buildOption(option, index, data) {
  const card = element("article", "pane option");
  const summary = element("div", "option-summary");
  summary.append(element("div", "micro", "ВАРИАНТ " + (index + 1)),
    element("h3", "total", money(option.total_rub)),
    element("p", "reserve", option.total_rub === data.budget_rub ? "Ровно в бюджете"
      : "Запас " + money(data.budget_rub - option.total_rub)));
  const itemList = element("ul", "items");
  for (const id of option.listing_ids) {
    const item = data.items[id];
    const row = element("li");
    const icon = element("span", "product-icon " + item.category_id,
      {table: "▰", chair: "◩", lamp: "◉"}[item.category_id]);
    icon.setAttribute("aria-hidden", "true");
    const content = element("div");
    content.append(element("p", "category", item.category),
      element("p", "item-title", item.title),
      element("p", "item-feature", item.feature || "Характеристика не указана"));
    row.append(icon, content, element("strong", "item-price", money(item.price_rub)));
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

function renderResults(data) {
  const response = data.response;
  results.replaceChildren();
  results.hidden = false;
  if (response.status === "ok") {
    const bar = element("div", "result-heading");
    const title = element("div");
    const count = response.options.length;
    const single = singleItemResult(response);
    title.append(element("h2", null, single ? "Подходящие товары" : "Подходящие комплекты"),
      element("p", null, single ? (count === 1 ? "Найдено 1 объявление" : "Найдено " + count + " объявления")
        : (count === 1 ? "Найден 1 комплект" : "Найдено " + count + " комплекта")));
    bar.append(title, element("span", "pill", "Проверено по каталогу"));
    const grid = element("div", "option-grid");
    response.options.forEach((option, index) => grid.append(buildOption(option, index, data)));
    results.append(bar, grid);
  }
  if (response.status === "no_match") {
    results.append(element("h2", null, "Подходящих комплектов нет"),
      element("p", null, response.message));
  } else if (response.status === "clarify") {
    results.append(element("p", null, response.questions.map(question => question.text).join("\n")));
  }
  results.append(jsonDetails(response));
}

function assistantReply(response, unchanged = false) {
  if (response.status === "clarify") return response.questions.map((question) => question.text).join("\n");
  if (response.status === "no_match") return response.message;
  const single = singleItemResult(response);
  if (unchanged) return "Подходящие " + (single ? "товары" : "комплекты") +
    " не изменились: подтверждённые условия и результат совпадают с предыдущим подбором.";
  return (single ? "Нашёл подходящие объявления. Самый доступный вариант — "
    : "Подобрал комплект с самовывозом. Самый доступный вариант — ") +
    money(response.options[0].total_rub) + ". Можно сравнить позиции ниже.";
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
  results.replaceChildren();
  results.hidden = true;
  const user = bubble("user", turn.text);
  const waiting = bubble("assistant", "Извлекаю условия и проверяю каталог…");
  waiting.classList.add("waiting");
  messages.append(user, waiting);
  messages.scrollTop = messages.scrollHeight;
  try {
    const data = await post("/api/buy", {messages: [...history, turn]});
    const clarification = data.clarification?.length > 0;
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
      results.replaceChildren();
      results.hidden = true;
    } else {
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
byId("prompts").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-prompt]");
  if (button) submit(button.dataset.prompt);
});
function resetDialogue() {
  voiceInput.cancel();
  history.length = 0;
  lastOkFingerprint = null;
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

const buyerTab = byId("buyer-tab");
const labTab = byId("lab-tab");
function view(lab) {
  if (lab) voiceInput.stop();
  buyerTab.setAttribute("aria-pressed", String(!lab));
  labTab.setAttribute("aria-pressed", String(lab));
  byId("buyer-view").hidden = lab;
  byId("lab-view").hidden = !lab;
}
buyerTab.addEventListener("click", () => view(false));
labTab.addEventListener("click", () => view(true));

function summarizeLab(response, catalog) {
  if (response.status !== "ok") return response.status;
  const names = new Map(catalog.map((item) => [item.id, item.title]));
  return response.options.map((option) => option.listing_ids.map((id) => names.get(id)).join(", ")
    + " · " + money(option.total_rub)).join("\n");
}

const labButton = byId("run-lab");
labButton.addEventListener("click", async () => {
  const panel = byId("lab-result");
  const error = byId("lab-error");
  error.hidden = true;
  labButton.disabled = true;
  labButton.textContent = "Сравниваем…";
  try {
    const data = await post("/api/lab", {});
    panel.replaceChildren(element("h2", null, "Результат проверки"),
      element("p", data.passed ? "pill" : "pill warning",
        data.passed ? "Подбор совпал" : "Ожидание проверки не выполнено"));
    const comparison = element("dl", "conditions comparison");
    for (const [label, result] of [["Обычные описания", data.before], ["Описания со вставкой", data.after]]) {
      const row = element("div");
      row.append(element("dt", null, label), element("dd", null, summarizeLab(result, data.catalog)));
      comparison.append(row);
    }
    panel.append(comparison);
    if (data.passed) {
      panel.append(element("p", "muted",
        "Стол за 5 400 ₽ подходит. Стол за 9 500 ₽ превышает бюджет. Подбор использует цены из структурированных полей."));
    }
    panel.append(element("p", "model-trace", traceText(data.trace)),
      jsonDetails({before: data.before, after: data.after}));
  } catch (problem) {
    error.textContent = problem.message;
    error.hidden = false;
  } finally {
    labButton.disabled = false;
    labButton.textContent = "Сравнить результаты";
  }
});

fetch("/api/catalog").then((response) => response.ok ? response.json() : null).then((catalog) => {
  if (catalog) byId("catalog-label").textContent = "Учебный каталог · " +
    catalog.total.toLocaleString("ru-RU") + " объявлений · " + catalog.cities.length + " города";
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
    cards.replaceChildren(); controls();
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
        readiness.textContent = data.message + progress;
        readiness.setAttribute("data-state", "checking");
        setup.hidden = true;
        if (!panel.hidden) setupTimer = setTimeout(() => refreshReadiness(), 1500);
        return;
      }
      readiness.textContent = data.message + (data.coverage ? " Покрытие: " + data.coverage.indexed + " из " + data.coverage.total + " объявлений." : "");
      readiness.setAttribute("data-state", data.status === "ready" ? "ready" : "unavailable");
      setup.hidden = data.status === "ready";
      setupHint.textContent = data.setup_hint || "Проверьте установку и подготовку визуального модуля.";
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
        setup.hidden = false; setupHint.textContent = "Проверьте запуск сервера и подготовку модуля по README.";
      }
    } finally { clearTimeout(timeout); healthPending = false; controls(); }
  }
  recheck.addEventListener("click", () => refreshReadiness(true));
  function choose(files) {
    clear();
    if (!files || files.length !== 1 || !files[0].size || files[0].size > 5 * 1024 * 1024) {
      status.textContent = "Выберите одно изображение размером до 5 МБ."; return;
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
    if (opening) { voiceInput.stop(); refreshReadiness(); }
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
    pending = true; controls(); cards.replaceChildren();
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
        if (result.status === "visual_unavailable") { setup.hidden = false; setupHint.textContent = result.setup_hint || "Повторите подготовку модуля по README."; }
        return;
      }
      status.textContent = result.message + " Покрытие каталога: " + result.coverage.indexed + " из " + result.coverage.total + " объявлений.";
      for (const item of result.results) {
        const card = element("article", "visual-card");
        const image = element("img");
        // Only the demo's allowlisted relative product images can be displayed.
        if (typeof item.image_path === "string" && /^products\/[a-zA-Z0-9_-]+\.(png|jpe?g|webp)$/.test(item.image_path)) {
          image.src = "/visual-images/" + item.image_path; image.alt = "Демонстрационное изображение: " + item.title;
          image.loading = "lazy";
          card.append(image);
        }
        card.append(element("h3", null, item.rank + ". " + item.title),
          element("p", null, item.category + " · " + item.city), element("strong", null, money(item.price_rub)),
          element("p", null, item.synthetic ? "Синтетическое демо-изображение" : "Демонстрационное изображение"));
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
