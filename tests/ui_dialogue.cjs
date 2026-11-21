// Exercise the actual browser script without adding frontend dependencies.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
class Node {
  constructor(tag = 'div') { this.tag = tag; this.children = []; this.handlers = {}; this.classList = {add() {}}; this.value = ''; this.attributes = {}; this.readOnly = false; }
  append(...nodes) { for (const node of nodes) { node.parent = this; this.children.push(node); } }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  get firstElementChild() { return this.children[0]; }
  cloneNode() { return new Node(this.tag); }
  addEventListener(type, callback) { this.handlers[type] = callback; }
  setAttribute(name, value) { this.attributes[name] = value; }
  removeAttribute(name) { delete this.attributes[name]; delete this[name]; }
  focus() {}
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(n => n !== this); }
  replaceWith(node) { const p = this.parent; const i = p.children.indexOf(this); node.parent = p; p.children[i] = node; }
}
function all(node) { return [node, ...node.children.flatMap(all)]; }
function create(speech = 'standard') {
  const ids = new Map();
  function byId(id) { if (!ids.has(id)) ids.set(id, new Node()); return ids.get(id); }
  byId('evidence').append(new Node());
  byId('messages').append(byId('starter'));
  const requests = [];
  const replies = [];
  const visualRequests = [], visualReplies = [], revokedURLs = [], healthReplies = [], healthRequests = [], demoRequests = [], demoReplies = [];
  const timers = new Map(); let nextTimer = 0;
  let objectNumber = 0;
  class MockFormData { constructor() {this.items = [];} append(...args) {this.items.push(args);} }
  class MockAbortController { constructor() {this.signal = {aborted: false};} abort() {this.signal.aborted = true; this.signal.onabort?.();} }
  async function abortable(reply, signal) {
    if (!signal) return await reply;
    return await Promise.race([Promise.resolve(reply), new Promise((_, reject) => {
      signal.onabort = () => reject(new Error('aborted'));
      if (signal.aborted) signal.onabort();
    })]);
  }
  const ready = {status: 'ready', message: 'Фото-поиск готов', coverage: {indexed: 3, total: 111},
    examples: [{name: 'table', label: 'Стол', url: '/visual-demo/table'}, {name: 'landscape', label: 'Пейзаж', url: '/visual-demo/landscape'}]};
  const recognitions = [];
  class Recognition {
    constructor() { recognitions.push(this); this.starts = 0; this.stops = 0; this.aborts = 0; }
    start() { this.starts++; this.onstart?.(); }
    stop() { this.stops++; }
    abort() { this.aborts++; }
    end() { this.onend?.(); }
    result(parts, resultIndex = 0) {
      this.onresult({resultIndex, results: parts.map(([transcript, isFinal]) => ({0: {transcript}, isFinal}))});
    }
    error(error) { this.onerror({error}); }
  }
  const window = speech === 'none' ? {} : {[speech === 'webkit' ? 'webkitSpeechRecognition' : 'SpeechRecognition']: Recognition};
  const context = vm.createContext({window, document: {getElementById: byId, createElement: tag => new Node(tag)}, Intl,
    FormData: MockFormData, AbortController: MockAbortController,
    setTimeout: fn => {const id = ++nextTimer; timers.set(id, fn); return id;}, clearTimeout: id => timers.delete(id),
    URL: {createObjectURL: () => 'blob:demo-' + ++objectNumber, revokeObjectURL: url => revokedURLs.push(url)},
    fetch: async (path, options) => {
      if (path === '/api/catalog') return {ok: true, json: async () => ({total: 1, cities: ['Таганрог']})};
      if (path === '/api/visual-status') {
        healthRequests.push(path);
        const result = await abortable(healthReplies.length ? healthReplies.shift() : ready, options?.signal);
        return {ok: true, json: async () => result};
      }
      if (path.startsWith('/visual-demo/')) {
        demoRequests.push(path);
        const blob = await abortable(demoReplies.length ? demoReplies.shift() : {size: 1234, type: 'image/png'}, options?.signal);
        return {ok: true, blob: async () => blob};
      }
      if (path === '/api/visual-search') {
        visualRequests.push(options);
        const result = await abortable(visualReplies.shift(), options?.signal);
        return {ok: !result?.httpError, json: async () => result};
      }
      requests.push(JSON.parse(options.body));
      const body = replies.shift();
      const resolved = await body;
      return {ok: !resolved?.httpError, json: async () => resolved?.httpError ? {error: resolved.httpError} : resolved};
    }});
  vm.runInContext(fs.readFileSync('buyer/ui/app.js', 'utf8'), context);
  const state = {requirements: [{label: 'Город', value: 'Таганрог'}], eligible: [], budget_rub: 5000,
    preferences: [], trace: {model: 'offline', prompt_tokens: 1, completion_tokens: 1, elapsed_ms: 1},
    items: {t: {category: 'Стол', category_id: 'table', title: 'Стол', price_rub: 1000, city: 'Таганрог'}}};
  const ok = {...state, response: {status: 'ok', options: [{listing_ids: ['t'], total_rub: 1000, explanation: 'Проверено'}]}};
  byId('visual-panel').hidden = true;
  return {byId, context, requests, replies, state, ok, recognitions, window, visualRequests, visualReplies, revokedURLs,
    healthReplies, healthRequests, demoRequests, demoReplies, ready, timers};
}
async function main() {
  const failures = [];
  let executed = 0;
  async function test(name, run) { executed++; try { await run(); console.log('PASS', name); } catch (e) { failures.push(name); console.log('FAIL', name, e.message); } }
  await test('stale cards disappear while a new request is pending', async () => {
    const t = create(); t.replies.push(t.ok); await t.context.submit('Стол');
    let release; t.replies.push(new Promise(resolve => {release = resolve;}));
    const pending = t.context.submit('рецепт гуляша');
    const wasCleared = t.byId('results').children.length === 0;
    release({...t.state, items: {}, response: {status: 'clarify', questions: [{field: 'categories', text: 'Уточните товар'}]}});
    await pending;
    assert.equal(wasCleared, true);
    assert.equal(all(t.byId('results')).some(n => n.className === 'pane option'), false);
    assert.equal(t.requests[1].messages.at(-1).text, 'рецепт гуляша');
  });
  await test('no_match displays an explanation outside raw JSON', async () => {
    const t = create(); t.replies.push(t.ok); await t.context.submit('Стол');
    t.replies.push({...t.state, items: {}, response: {status: 'no_match', message: 'Подходящих ламп нет'}});
    await t.context.submit('Добавь лампу');
    assert.equal(all(t.byId('results')).some(n => n.tag === 'p' && n.textContent === 'Подходящих ламп нет'), true);
    assert.equal(all(t.byId('results')).some(n => n.className === 'pane option'), false);
  });
  await test('equal verified results are described honestly', async () => {
    const t = create(); t.replies.push(t.ok, t.ok);
    await t.context.submit('Стол'); await t.context.submit('Столик');
    assert.equal(all(t.byId('messages')).some(n => n.tag === 'p' && /не изменились/.test(n.textContent)), true);
  });
  await test('reset button clears history, cards and evidence', async () => {
    const t = create(); t.replies.push(t.ok); await t.context.submit('Стол');
    t.byId('reset').handlers.click();
    assert.equal(vm.runInContext('history.length', t.context), 0);
    assert.equal(t.byId('results').children.length, 0);
    t.replies.push(t.ok); await t.context.submit('Лампа');
    assert.equal(t.requests.at(-1).messages.length, 1);
  });
  await test('typed new dialogue starts an isolated request', async () => {
    const t = create(); t.replies.push(t.ok); await t.context.submit('Стол');
    t.replies.push({...t.state, items: {}, response: {status: 'clarify', questions: [{field: 'categories', text: 'Что приобрести?'}]}});
    await t.context.submit('новый диалог');
    assert.equal(t.requests.at(-1).messages.length, 1);
    assert.equal(t.byId('messages').children.filter(n => n.className === 'bubble user').length, 1);
  });
  function dataWith(t, options) {
    const items = {};
    for (const option of options) for (const id of option.listing_ids) {
      const category = id[0] === 'l' ? 'lamp' : id[0] === 'c' ? 'chair' : 'table';
      items[id] = {...t.state.items.t, category_id: category, category, title: id};
    }
    return {...t.state, items, response: {status: 'ok', options}};
  }
  function option(ids, total = 1000) { return {listing_ids: ids, total_rub: total, explanation: 'Проверено'}; }
  function textPresent(t, tag, text) { return all(t.byId('results')).some(n => n.tag === tag && n.textContent === text); }
  await test('lamp alternatives are individual listings', async () => {
    const t = create(); t.replies.push(dataWith(t, [option(['l1']), option(['l2']), option(['l3'])]));
    await t.context.submit('Лампа');
    assert.equal(textPresent(t, 'h2', 'Подходящие товары'), true);
    assert.equal(textPresent(t, 'p', 'Найдено 3 объявления'), true);
    assert.equal(all(t.byId('messages')).some(n => n.tag === 'p' && n.textContent?.startsWith('Нашёл подходящие объявления.')), true);
  });
  await test('one table is one listing', async () => {
    const t = create(); t.replies.push(dataWith(t, [option(['t1'])]));
    await t.context.submit('Только стол');
    assert.equal(textPresent(t, 'h2', 'Подходящие товары'), true);
    assert.equal(textPresent(t, 'p', 'Найдено 1 объявление'), true);
  });
  await test('table chair lamp remain a bundle', async () => {
    const t = create(); t.replies.push(dataWith(t, [option(['t1', 'c1', 'l1'])]));
    await t.context.submit('Стол, кресло и лампа');
    assert.equal(textPresent(t, 'h2', 'Подходящие комплекты'), true);
    assert.equal(textPresent(t, 'p', 'Найден 1 комплект'), true);
    assert.equal(all(t.byId('messages')).some(n => n.tag === 'p' && n.textContent?.startsWith('Подобрал комплект')), true);
  });
  await test('changed bundle receives a fresh result message', async () => {
    const t = create(); t.replies.push(dataWith(t, [option(['t1', 'c1'])]), dataWith(t, [option(['t2', 'c1'], 2000)]));
    await t.context.submit('Стол и кресло'); await t.context.submit('Другой стол');
    const texts = all(t.byId('messages')).filter(n => n.tag === 'p').map(n => n.textContent);
    assert.equal(texts.at(-1).startsWith('Подобрал комплект'), true);
    assert.equal(texts.at(-1).includes('не изменились'), false);
  });
  await test('unchanged results name goods and bundles separately', async () => {
    for (const [ids, noun] of [[['t1'], 'товары'], [['t1', 'c1', 'l1'], 'комплекты']]) {
      const t = create(); const data = dataWith(t, [option(ids)]); t.replies.push(data, data);
      await t.context.submit('Подбери'); await t.context.submit('Те же условия');
      const texts = all(t.byId('messages')).filter(n => n.tag === 'p').map(n => n.textContent);
      assert.equal(texts.at(-1).startsWith(`Подходящие ${noun} не изменились`), true);
    }
  });
  function mic(t) { t.byId('voice').handlers.click(); return t.recognitions.at(-1); }
  function status(t) { return t.byId('voice-status').textContent; }
  await test('speech API feature detection supports standard and webkit variants without auto-start', () => {
    for (const variant of ['standard', 'webkit']) {
      const t = create(variant);
      assert.equal(t.byId('voice').disabled, false);
      assert.equal(t.recognitions.length, 0);
      assert.equal(mic(t).starts, 1);
    }
  });
  await test('unsupported speech API leaves text submission working', async () => {
    const t = create('none');
    assert.equal(t.byId('voice').disabled, true);
    assert.match(status(t), /поддерживаемом браузере/);
    mic(t); assert.equal(t.recognitions.length, 0);
    t.replies.push(t.ok); await t.context.submit('Стол');
    assert.equal(t.requests.length, 1);
  });
  await test('microphone click configures Russian continuous interim recognition', () => {
    const t = create(); const r = mic(t);
    assert.equal(r.lang, 'ru-RU'); assert.equal(r.continuous, true);
    assert.equal(r.interimResults, true); assert.equal(r.maxAlternatives, 1);
    assert.equal(t.byId('voice').attributes['aria-pressed'], 'true');
    assert.equal(t.byId('voice').attributes['aria-label'], 'Остановить запись');
    assert.equal(t.byId('query').readOnly, true); assert.equal(status(t), 'Слушаю…');
  });
  await test('second click stops and end releases the instance for another dictation', () => {
    const t = create(); const r = mic(t); mic(t);
    assert.equal(r.stops, 1); mic(t); assert.equal(r.stops, 1);
    assert.equal(t.recognitions.length, 1);
    r.end(); assert.equal(t.byId('voice').attributes['aria-pressed'], 'false');
    assert.equal(t.byId('query').readOnly, false);
    mic(t); assert.equal(t.recognitions.length, 2);
  });
  await test('interim text is visible but never enters history or triggers POST', () => {
    const t = create(); const r = mic(t); r.result([['Найди сто', false]]);
    assert.equal(t.byId('query').value, 'Найди сто');
    assert.equal(vm.runInContext('history.length', t.context), 0);
    assert.equal(vm.runInContext('lastOkFingerprint', t.context), null);
    assert.equal(t.requests.length, 0);
  });
  await test('cumulative resultIndex updates replace interim slots without duplicating finals', () => {
    const t = create(); const r = mic(t);
    r.result([['Стол', false]]); r.result([['Стол', true]]);
    r.result([['Стол', true], ['в А', false]], 1);
    r.result([['Стол', true], ['в Азове', true]], 1);
    r.result([['Стол', true], ['в Азове', true], ['за тысячу', false]], 2);
    assert.equal(t.byId('query').value, 'Стол в Азове за тысячу');
    r.end(); assert.equal(t.byId('query').value, 'Стол в Азове');
    assert.match(status(t), /Речь распознана. Проверьте текст/);
    assert.equal(t.requests.length, 0);
  });
  await test('shrinking interim result list removes obsolete provisional text', () => {
    const t = create(); const r = mic(t);
    r.result([['Стол', true], ['возможно', false]], 0);
    r.result([['Стол', true]], 1);
    assert.equal(t.byId('query').value, 'Стол');
  });
  await test('dictation preserves existing text and allows editing after end', () => {
    const t = create(); t.byId('query').value = 'До 5000 рублей';
    const r = mic(t); r.result([['стол в Азове', true]]); r.end();
    assert.equal(t.byId('query').value, 'До 5000 рублей стол в Азове');
    assert.equal(t.byId('query').readOnly, false);
    t.byId('query').value += ', деревянный';
    const second = mic(t); second.result([['не шире метра', true]]); second.end();
    assert.equal(t.byId('query').value, 'До 5000 рублей стол в Азове, деревянный не шире метра');
  });
  await test('manual stop strips interim text and retains final results arriving before end', () => {
    const t = create(); const r = mic(t);
    r.result([['Стол', true], ['в А', false]]); mic(t);
    assert.equal(t.byId('query').value, 'Стол');
    r.result([['Стол', true], ['в Азове', true]], 1); r.end();
    assert.equal(t.byId('query').value, 'Стол в Азове');
  });
  await test('empty dictation preserves original field and sends nothing', () => {
    const t = create(); t.byId('query').value = 'Мой текст'; const r = mic(t); r.end();
    assert.equal(t.byId('query').value, 'Мой текст'); assert.equal(t.requests.length, 0);
  });
  await test('explicit form submission sends edited final text through existing history', async () => {
    const t = create(); t.replies.push(t.ok); await t.context.submit('Стол');
    const prior = vm.runInContext('history.length', t.context);
    const fingerprint = vm.runInContext('lastOkFingerprint', t.context);
    const r = mic(t); r.result([['В Азове', true], ['временное', false]]); r.end();
    assert.equal(vm.runInContext('history.length', t.context), prior);
    assert.equal(vm.runInContext('lastOkFingerprint', t.context), fingerprint);
    assert.equal(t.requests.length, 1);
    t.byId('query').value += ', до 5000'; t.replies.push(t.ok);
    await t.byId('composer').handlers.submit({preventDefault() {}});
    // Event listeners invoke the existing async submit without returning its promise.
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(t.requests.length, 2);
    assert.equal(t.requests[1].messages.length, 3);
    assert.equal(t.requests[1].messages[0].text, 'Стол');
    assert.equal(t.requests[1].messages.at(-1).text, 'В Азове, до 5000');
  });
  await test('Enter sends short dictated clarification through the existing submit handler', async () => {
    const t = create(); const r = mic(t); r.result([['Азов', true]]); r.end();
    t.replies.push(t.ok);
    t.byId('query').handlers.keydown({key: 'Enter', preventDefault() {}});
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(t.requests[0].messages[0].text, 'Азов');
  });
  await test('active recording blocks direct, Enter, and form submission with a hint', async () => {
    const t = create(); const r = mic(t); r.result([['Стол', false]]);
    await t.context.submit('Стол');
    t.byId('query').handlers.keydown({key: 'Enter', preventDefault() {}});
    t.byId('composer').handlers.submit({preventDefault() {}});
    assert.equal(t.requests.length, 0); assert.equal(t.byId('send').disabled, true);
    assert.match(status(t), /Сначала завершите запись/);
  });
  for (const [code, expected] of [['no-speech', /Речь не распознана/], ['not-allowed', /Разрешите доступ/],
    ['network', /временно недоступно/], ['audio-capture', /обнаружить микрофон/],
    ['service-not-allowed', /запрещён/], ['language-not-supported', /Русский язык недоступен/],
    ['aborted', /Запись остановлена/], ['unknown-error', /временно недоступно/]]) {
    await test(`speech error ${code} preserves prior text and final fragments`, () => {
      const t = create(); t.byId('query').value = 'До 5000'; const r = mic(t);
      r.result([['стол', true], ['ненужное', false]]); r.error(code);
      assert.match(status(t), expected); r.end();
      assert.match(status(t), expected); assert.equal(t.byId('query').value, 'До 5000 стол');
      assert.equal(t.byId('query').readOnly, false); assert.equal(t.requests.length, 0);
      assert.equal(t.byId('voice').disabled, false);
    });
  }
  await test('new dialogue aborts recording and ignores late recognition callbacks', () => {
    const t = create(); t.byId('query').value = 'Предыдущий текст'; const r = mic(t);
    r.result([['новая речь', false]]); t.byId('reset').handlers.click();
    assert.equal(r.aborts, 1); assert.equal(t.byId('query').value, '');
    assert.equal(t.byId('voice').attributes['aria-pressed'], 'false');
    r.result([['поздний результат', true]]); r.error('aborted'); r.end();
    assert.equal(t.byId('query').value, ''); assert.equal(status(t), '');
    assert.equal(vm.runInContext('history.length', t.context), 0);
    assert.equal(t.byId('voice').disabled, false);
  });
  await test('defence tab stops recognition and keeps finalized text', () => {
    const t = create(); const r = mic(t); r.result([['Стол', true], ['возможно', false]]);
    t.byId('lab-tab').handlers.click(); assert.equal(r.stops, 1); r.end();
    assert.equal(t.byId('query').value, 'Стол');
    assert.equal(t.byId('buyer-view').hidden, true);
  });
  await test('sending blocks microphone until the existing request finishes', async () => {
    const t = create(); let release; t.replies.push(new Promise(resolve => {release = resolve;}));
    const pending = t.context.submit('Стол');
    assert.equal(t.byId('voice').disabled, true); mic(t);
    assert.equal(t.recognitions.length, 0); release(t.ok); await pending;
    assert.equal(t.byId('voice').disabled, false); mic(t); assert.equal(t.recognitions.length, 1);
  });
  await test('server busy error keeps dictated text available for retry', async () => {
    const t = create(); const r = mic(t); r.result([['Стол в Азове', true]]); r.end();
    t.replies.push({httpError: 'Gemma уже обрабатывает запрос'});
    await t.context.submit(t.byId('query').value);
    assert.equal(t.byId('query').value, 'Стол в Азове');
    assert.equal(vm.runInContext('history.length', t.context), 0);
    assert.equal(t.byId('voice').disabled, false);
  });
  await test('start exceptions restore editing without clearing text', () => {
    const t = create(); t.byId('query').value = 'Стол';
    t.window.SpeechRecognition.prototype.start = () => {throw new Error('private technical details');};
    mic(t); assert.equal(t.byId('query').value, 'Стол');
    assert.equal(t.byId('query').readOnly, false); assert.match(status(t), /временно недоступно/);
    assert.equal(t.byId('voice').disabled, false);
  });
  await test('automatic end never restarts and old callbacks cannot overwrite a new recording', () => {
    const t = create(); const first = mic(t); first.result([['Стол', true]]); first.end();
    assert.equal(t.recognitions.length, 1); assert.equal(first.starts, 1);
    const next = mic(t); next.result([['в Азове', true]]);
    first.result([['устаревший текст', true]]); first.error('network'); first.end();
    next.end(); assert.equal(t.byId('query').value, 'Стол в Азове');
  });
  await test('error before any final result preserves only the preexisting input', () => {
    const t = create(); t.byId('query').value = 'Нужен стол'; const r = mic(t);
    r.result([['временная расшифровка', false]]); r.error('not-allowed'); r.end();
    assert.equal(t.byId('query').value, 'Нужен стол');
    assert.equal(t.requests.length, 0);
  });
  function choosePhoto(t, files = [{name: 'photo.png', size: 1234, type: 'image/png'}]) {
    t.byId('visual-file').files = files; t.byId('visual-file').handlers.change(); return files[0];
  }
  function photoResult() { return {status: 'ok', message: 'Демонстрационные изображения', coverage: {indexed: 3, total: 250},
    results: [{rank: 1, listing_id: 't', title: 'Стол', category: 'Стол', city: 'Азов', price_rub: 6500,
      image_path: 'products/table.png', synthetic: true}]}; }
  await test('photo mode opens without changing dialogue and stops active dictation', () => {
    const t = create(); const r = mic(t); r.result([['Стол', true]]);
    t.byId('visual-toggle').handlers.click(); assert.equal(r.stops, 1); r.end();
    assert.equal(t.byId('visual-panel').hidden, false);
    assert.equal(t.byId('query').value, 'Стол'); assert.equal(t.requests.length, 0);
    assert.equal(vm.runInContext('history.length', t.context), 0);
  });
  await test('photo selection creates preview and removal revokes local URL', () => {
    const t = create(); choosePhoto(t);
    assert.equal(t.byId('visual-preview').hidden, false); assert.equal(t.byId('visual-search').disabled, false);
    t.byId('visual-remove').handlers.click();
    assert.equal(t.revokedURLs.length, 1); assert.equal(t.byId('visual-preview').hidden, true);
    assert.equal(t.byId('visual-search').disabled, true); assert.equal(t.visualRequests.length, 0);
  });
  await test('photo POST is multipart and does not mutate history or normal result fingerprint', async () => {
    const t = create(); t.replies.push(t.ok); await t.context.submit('Стол');
    const before = vm.runInContext('JSON.stringify({history,lastOkFingerprint})', t.context);
    const file = choosePhoto(t); t.visualReplies.push(photoResult());
    await t.byId('visual-search').handlers.click();
    assert.equal(t.visualRequests[0].body.items[0][0], 'image');
    assert.equal(t.visualRequests[0].body.items[0][1], file);
    assert.equal(t.requests.length, 1);
    assert.equal(vm.runInContext('JSON.stringify({history,lastOkFingerprint})', t.context), before);
    assert.equal(t.byId('visual-results').children.length, 1);
    assert.match(t.byId('visual-status').textContent, /3 из 250/);
    t.replies.push(t.ok); await t.context.submit('Азов');
    assert.equal(t.requests[1].messages.length, 3);
  });
  await test('unavailable photo search leaves selected file, typed text and speech working', async () => {
    const t = create(); t.byId('query').value = 'Мой запрос'; choosePhoto(t);
    t.visualReplies.push({status: 'visual_unavailable', message: 'Визуальный поиск недоступен'});
    await t.byId('visual-search').handlers.click();
    assert.match(t.byId('visual-status').textContent, /недоступен/);
    assert.equal(t.byId('query').value, 'Мой запрос'); assert.equal(t.byId('visual-search').disabled, false);
    const r = mic(t); r.result([['в Азове', true]]); r.end();
    assert.equal(t.byId('query').value, 'Мой запрос в Азове');
  });
  await test('photo removal aborts pending request and ignores stale results', async () => {
    const t = create(); choosePhoto(t); let release;
    t.visualReplies.push(new Promise(resolve => {release = resolve;}));
    const request = t.byId('visual-search').handlers.click();
    assert.equal(t.byId('visual-search').disabled, true);
    t.byId('visual-remove').handlers.click();
    assert.equal(t.visualRequests[0].signal.aborted, true); release(photoResult()); await request;
    assert.equal(t.byId('visual-results').children.length, 0);
    assert.equal(t.byId('visual-search').disabled, true);
  });
  await test('drop validates file count, byte size and image dimensions', () => {
    const t = create();
    t.byId('visual-drop').handlers.drop({preventDefault() {}, dataTransfer: {files: [{name: 'photo.png', size: 12}]}});
    assert.equal(t.byId('visual-search').disabled, false);
    choosePhoto(t, [{size: 6 * 1024 * 1024}]); assert.equal(t.byId('visual-search').disabled, true);
    choosePhoto(t, [{size: 12}, {size: 12}]); assert.equal(t.byId('visual-search').disabled, true);
    choosePhoto(t); t.byId('visual-preview').naturalWidth = 2049; t.byId('visual-preview').onload();
    assert.equal(t.byId('visual-search').disabled, true);
    assert.match(t.byId('visual-status').textContent, /2048/);
  });
  await test('visual mode never renders untrusted remote image URLs', async () => {
    const t = create(); choosePhoto(t); const response = photoResult();
    response.results[0].image_path = 'https://untrusted.example/picture.png';
    t.visualReplies.push(response); await t.byId('visual-search').handlers.click();
    assert.equal(all(t.byId('visual-results')).some(node => node.tag === 'img'), false);
  });
  await test('network failure in photo request leaves normal text submission working', async () => {
    const t = create(); choosePhoto(t); t.visualReplies.push(Promise.reject(new Error('network')));
    await t.byId('visual-search').handlers.click();
    assert.match(t.byId('visual-status').textContent, /обычным поиском/);
    t.replies.push(t.ok); await t.context.submit('Стол'); assert.equal(t.requests.length, 1);
  });
  async function openPhoto(t) {
    t.byId('visual-toggle').handlers.click();
    await new Promise(resolve => setImmediate(resolve));
  }
  await test('photo readiness is checked only on opening and shows honest coverage', async () => {
    const t = create(); assert.equal(t.healthRequests.length, 0);
    await openPhoto(t);
    assert.equal(t.healthRequests.length, 1); assert.match(t.byId('visual-readiness').textContent, /3 из 111/);
    assert.equal(t.byId('visual-examples').children.length, 2);
    assert.equal(t.byId('visual-setup').hidden, true);
    assert.equal(t.requests.length + t.visualRequests.length, 0);
  });
  await test('demo sample fills preview but never auto-submits a photo or dialogue', async () => {
    const t = create(); t.byId('query').value = 'Мой текст'; await openPhoto(t);
    await t.byId('visual-examples').children[0].handlers.click();
    assert.deepEqual(t.demoRequests, ['/visual-demo/table']);
    assert.equal(t.byId('visual-search').disabled, false);
    assert.equal(t.byId('query').value, 'Мой текст');
    assert.equal(t.requests.length + t.visualRequests.length, 0);
    t.visualReplies.push(photoResult()); await t.byId('visual-search').handlers.click();
    assert.equal(t.visualRequests.length, 1);
  });
  await test('not-ready photo mode explains setup while ordinary input stays usable', async () => {
    const t = create(); t.healthReplies.push({status: 'visual_unavailable', message: 'Фото-поиск не подготовлен', setup_hint: 'Подготовьте индекс'});
    await openPhoto(t);
    assert.equal(t.byId('visual-setup').hidden, false);
    assert.equal(t.byId('visual-setup-hint').textContent, 'Подготовьте индекс');
    t.replies.push(t.ok); await t.context.submit('Стол'); assert.equal(t.requests.length, 1);
  });
  await test('photo request deadline aborts a hung response and restores controls', async () => {
    const t = create(); choosePhoto(t); t.visualReplies.push(new Promise(() => {}));
    const pending = t.byId('visual-search').handlers.click();
    for (const callback of t.timers.values()) callback();
    await pending;
    assert.equal(t.byId('visual-search').disabled, false);
    assert.match(t.byId('visual-status').textContent, /временно недоступен/);
    assert.equal(t.timers.size, 0);
  });
  await test('a late demo sample cannot overwrite a manually selected photo', async () => {
    const t = create(); await openPhoto(t); let release;
    t.demoReplies.push(new Promise(resolve => {release = resolve;}));
    const pending = t.byId('visual-examples').children[0].handlers.click();
    const manual = choosePhoto(t); release({size: 321, type: 'image/png'}); await pending;
    t.visualReplies.push(photoResult()); await t.byId('visual-search').handlers.click();
    assert.equal(t.visualRequests[0].body.items[0][1], manual);
  });
  await test('readiness rejects remote URLs in demo example metadata', async () => {
    const t = create(); t.healthReplies.push({...t.ready, examples: [{label: 'X', url: 'https://untrusted.example/photo'}]});
    await openPhoto(t); assert.equal(t.byId('visual-examples').children.length, 0);
    assert.equal(t.demoRequests.length, 0);
  });
  await test('readiness can recover after setup without resetting the dialogue', async () => {
    const t = create(); t.healthReplies.push({status: 'visual_unavailable', message: 'Нет индекса'});
    t.byId('query').value = 'Сохранённый запрос'; await openPhoto(t);
    t.healthReplies.push(t.ready); await t.byId('visual-recheck').handlers.click();
    assert.equal(t.byId('visual-setup').hidden, true);
    assert.equal(t.byId('visual-readiness').attributes['data-state'], 'ready');
    assert.equal(t.byId('query').value, 'Сохранённый запрос');
  });
  await test('pending readiness prevents a competing upload and duplicate checks', async () => {
    const t = create(); let release; t.healthReplies.push(new Promise(resolve => {release = resolve;}));
    await openPhoto(t); choosePhoto(t);
    assert.equal(t.byId('visual-search').disabled, true);
    await t.byId('visual-recheck').handlers.click(); assert.equal(t.healthRequests.length, 1);
    release(t.ready); await new Promise(resolve => setImmediate(resolve));
    assert.equal(t.byId('visual-search').disabled, false);
  });
  console.log(`Completed ${executed} dialogue, speech and photo scenarios; failures: ${failures.length}`);
  assert.equal(failures.length, 0, failures.join(', '));
}
main().catch(error => {console.error(error.message); process.exitCode = 1;});
