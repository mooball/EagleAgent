/**
 * The Add supplier card's typeahead: search, pick, or create.
 *
 * Why these checks exist — every one of these was invisible to `pytest` and to
 * `node --check`, and each is silent when it breaks:
 *
 *  1. `data-show-when` is recomputed from a HIDDEN field here (the card's
 *     `mode`). Setting a hidden input's `.value` fires no `change` event, so a
 *     view switch that only assigns the value reveals nothing — the card looks
 *     frozen on whatever view it opened with.
 *  2. Views hide with an inline `display:none`, not the `hidden` attribute: a
 *     Tailwind display class (grid/block) outranks `[hidden]`, so the attribute
 *     would quietly do nothing on exactly the sections that need it.
 *  3. Two keystrokes can be in flight at once. Without a sequence token the
 *     slower, older response paints last and the list shows matches for a prefix
 *     the user has moved past — they then pick from stale rows.
 *  4. A result row is a `<button>` inside the form. Without `type="button"` a
 *     click submits the card before a supplier is chosen.
 *  5. Enter in the lookup box does not submit a form, but a form with a single
 *     text input submits implicitly on Enter — so the box must swallow it.
 *  6. Editing the name after picking must drop the picked id: the server rejects
 *     a mismatched {id, name} pair, so a stale one is a guaranteed failed submit.
 *
 * The checks extract the real functions from the template by brace-matching and
 * drive them against a small DOM stub, so a template reshuffle fails here rather
 * than in a browser nobody is watching. Run one directly:
 *
 *     node tests/client/check_widget_lookup.js
 */

'use strict';

const {
  readTemplate, extract, assert, eq, check, finish, tick,
} = require('./harness');

const TEMPLATE = 'templates/chat_ui/embed.html';

// --- a small DOM ------------------------------------------------------------

const camel = (k) =>
  k.replace(/^data-/, '').replace(/-([a-z])/g, (_, c) => c.toUpperCase());

const ATTR_RE = /\[([^\]=]+)(?:="([^"]*)")?\]/g;

/** Supports the selector shapes the widget code actually uses. */
function matches(el, sel) {
  const m = /^([a-z]*)((?:\[[^\]]+\])*)(:checked)?$/.exec(sel);
  if (!m) { throw new Error('unsupported selector in the check: ' + sel); }
  if (m[1] && el.tag !== m[1]) { return false; }
  let a;
  ATTR_RE.lastIndex = 0;
  while ((a = ATTR_RE.exec(m[2]))) {
    const have = el.attrs[a[1]];
    if (have === undefined) { return false; }
    if (a[2] !== undefined && String(have) !== a[2]) { return false; }
  }
  if (m[3] && (el.checked !== true || (el.type !== 'radio' && el.type !== 'checkbox'))) {
    return false;
  }
  return true;
}

function descendants(el) {
  const out = [];
  el.children.forEach((c) => { out.push(c); out.push(...descendants(c)); });
  return out;
}

function node(tag, attrs) {
  const a = Object.assign({}, attrs);
  const el = {
    tag: tag,
    attrs: a,
    dataset: {},
    children: [],
    listeners: {},
    style: {},
    classList: {
      _on: {},
      toggle(cls, on) { this._on[cls] = !!on; },
      contains(cls) { return !!this._on[cls]; },
    },
    parentNode: null,
    value: '',
    textContent: '',
    className: '',
    hidden: false,
    checked: false,
    disabled: false,
    removed: false,
    focused: false,
    type: a.type || (tag === 'input' ? 'text' : ''),
    // Real elements reflect these from their attributes, and the template reads
    // them as properties (e.target.name decides whether a change is its business).
    name: a.name || '',
    id: a.id || '',
    // Geometry. Nothing here lays anything out, so a check sets `rect` to the
    // coordinates it wants the code to see. `offsetWidth`/`offsetHeight` are the
    // template's visibility test, so they follow style.display — inherited, like
    // the browser, because hiding a view must hide the fields inside it.
    rect: {top: 0, bottom: 0, left: 0, right: 0},
    selection: null,
    // A browser gives every element a scroll position; the code adjusts it
    // relative to where it already is.
    scrollTop: 0,
    getBoundingClientRect() { return this.rect; },
    setSelectionRange(start, end) { this.selection = [start, end]; },
    appendChild(child) { child.parentNode = this; this.children.push(child); return child; },
    remove() { this.removed = true; },
    focus() { this.focused = true; },
    click() { this.dispatchEvent({type: 'click', target: this}); },
    addEventListener(type, fn) {
      (this.listeners[type] = this.listeners[type] || []).push(fn);
    },
    // Bubbling, like the browser: a listener on the form sees an event fired at
    // a field inside it. That is how wireShowWhen hears a mode change at all —
    // and the browser sets `target` to the dispatcher, which wireShowWhen reads
    // to decide whether the event is about the field it watches.
    dispatchEvent(ev) {
      if (ev && ev.target === undefined) { ev.target = this; }
      let el2 = this;
      while (el2) {
        (el2.listeners[ev.type] || []).forEach((fn) => fn(ev));
        el2 = el2.parentNode;
      }
      return true;
    },
    closest(sel) {
      let el2 = this;
      while (el2) {
        if (matches(el2, sel)) { return el2; }
        el2 = el2.parentNode;
      }
      return null;
    },
    matches(sel) { return matches(this, sel); },
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; },
    // Walks the tree once, so the result is in document order as a browser
    // returns it. A per-selector concat would reorder `input, textarea, select`
    // and change which field "the first visible one" turns out to be.
    querySelectorAll(sel) {
      const groups = String(sel).split(',').map((s) => s.trim()).filter(Boolean);
      if (!groups.length) { return []; }
      const wanted = groups.map((group) => {
        const parts = group.split(' ').filter(Boolean);
        return {ancestors: parts.slice(0, -1), last: parts[parts.length - 1]};
      });
      return descendants(this).filter((candidate) => wanted.some((w) => {
        if (!matches(candidate, w.last)) { return false; }
        return w.ancestors.every((ancestorSel) => {
          let node = candidate.parentNode;
          while (node) {
            if (matches(node, ancestorSel)) { return true; }
            node = node.parentNode;
          }
          return false;
        });
      }));
    },
  };
  // data-* attributes populate dataset, exactly as the browser does — the
  // template reads dataset.showWhen while the markup writes data-show-when.
  Object.keys(a).forEach((k) => {
    if (k.indexOf('data-') === 0) { el.dataset[camel(k)] = a[k]; }
  });
  let html = '';
  Object.defineProperty(el, 'innerHTML', {
    get() { return html; },
    // The code clears the list with innerHTML = '' before re-rendering.
    set(v) { html = v; if (v === '') { el.children = []; } },
  });
  ['offsetWidth', 'offsetHeight'].forEach((prop) => {
    Object.defineProperty(el, prop, {
      get() { return isDisplayed(el) ? 1 : 0; },
    });
  });
  return el;
}

/** True when neither this node nor an ancestor is hidden. */
function isDisplayed(el) {
  let node = el;
  while (node) {
    if (node.hidden || (node.style && node.style.display === 'none')) { return false; }
    node = node.parentNode;
  }
  return true;
}

/** All text under a node, so a check can assert on what a row actually says. */
function textOf(el) {
  if (!el) { return ''; }
  if (el.children && el.children.length) { return el.children.map(textOf).join('|'); }
  return String(el.textContent || '');
}

/**
 * The card's three views, mirroring the real markup: one name input shared by
 * search and create (not two), one set of line radios, and the mode field the
 * server decides on load.
 */
function buildCard(opts) {
  const o = opts || {};
  const card = node('div', {'data-widget': '1', 'data-widget-id': 'w1'});
  const form = node('form', {'data-widget-form': '1'});
  card.appendChild(form);

  const mode = node('input', {name: 'mode', type: 'hidden'});
  mode.value = o.mode || 'search';
  const supplierId = node('input', {name: 'supplier_id', type: 'hidden'});
  form.appendChild(mode);
  form.appendChild(supplierId);

  const nameBlock = node('div', {'data-show-when': 'mode=search,create'});
  const nameInput = node('input', {name: 'name', 'data-widget-lookup': '1'});
  nameInput.value = o.name || '';
  nameBlock.appendChild(nameInput);

  const searchBlock = node('div', {'data-show-when': 'mode=search'});
  const status = node('p', {'data-widget-lookup-status': '1'});
  const results = node('div', {'data-widget-results': '1'});
  const createBtn = node('button', {'data-widget-create-new': '1', type: 'button'});
  searchBlock.appendChild(status);
  searchBlock.appendChild(results);
  searchBlock.appendChild(createBtn);

  const chosenBlock = node('div', {'data-show-when': 'mode=chosen'});
  const chosenName = node('span', {'data-widget-chosen-name': '1'});
  const chosenMeta = node('span', {'data-widget-chosen-meta': '1'});
  // Where the client puts a contact picker when the picked supplier has several
  // contacts — the options arrive with the search result, so it cannot be
  // rendered by the server.
  const chosenContacts = node('div', {'data-widget-contacts': '1'});
  const changeBtn = node('button', {'data-widget-change': '1', type: 'button'});
  chosenBlock.appendChild(chosenName);
  chosenBlock.appendChild(chosenMeta);
  chosenBlock.appendChild(chosenContacts);
  chosenBlock.appendChild(changeBtn);

  const createBlock = node('div', {'data-show-when': 'mode=create'});
  const contact = node('input', {name: 'contact_name'});
  createBlock.appendChild(contact);

  [nameBlock, searchBlock, chosenBlock, createBlock].forEach((b) => form.appendChild(b));

  return {
    card, form, mode, supplierId, nameInput, status, results, createBtn,
    nameBlock, searchBlock, chosenBlock, createBlock, chosenName, chosenMeta,
    chosenContacts, changeBtn, contact,
  };
}

// --- controllable collaborators ---------------------------------------------

/** A fetch whose responses are released by hand, in whatever order we like. */
function makeFetch() {
  const calls = [];
  const pending = [];
  const fetch = (url, opts) => {
    calls.push({url: url, opts: opts});
    return new Promise((resolve) => { pending.push({url: url, resolve: resolve}); });
  };
  fetch.calls = calls;
  fetch.reply = (index, body) => {
    pending[index].resolve({json: () => Promise.resolve(body)});
  };
  return fetch;
}

/** Timers we fire ourselves, so the debounce is not a sleep in a test. */
function makeClock() {
  const queue = [];
  return {
    setTimeout(fn) { queue.push({fn: fn, cancelled: false}); return queue.length; },
    clearTimeout(id) { if (queue[id - 1]) { queue[id - 1].cancelled = true; } },
    flush() {
      const due = queue.splice(0);
      due.forEach((t) => { if (!t.cancelled) { t.fn(); } });
    },
  };
}

function FakeEvent(type, opts) {
  return {type: type, detail: (opts || {}).detail};
}

const Dom = () => ({createElement: (tag) => node(tag)});

// --- runner -----------------------------------------------------------------

function buildRunner(src, deps) {
  const o = deps || {};
  // Constants live at template scope, so brace-matching cannot capture them —
  // read the real values, or the check keeps passing after someone changes them.
  const min = /var LOOKUP_MIN_CHARS = (\d+)/.exec(src);
  const debounce = /var LOOKUP_DEBOUNCE_MS = (\d+)/.exec(src);
  const gap = /var WIDGET_REVEAL_GAP = (\d+)/.exec(src);
  if (!min || !debounce || !gap) {
    throw new Error('widget client constants not found in ' + TEMPLATE);
  }

  const code = 'var LOOKUP_MIN_CHARS = ' + min[1] + ';\n'
    + 'var LOOKUP_DEBOUNCE_MS = ' + debounce[1] + ';\n'
    + 'var WIDGET_REVEAL_GAP = ' + gap[1] + ';\n'
    + 'var NARROW_CARD_PX = 400;\n'
    // wireWidget attaches this to every form. It is exercised in
    // check_widget_submit.js; here it only has to exist as a value.
    + 'var onWidgetSubmit = function () {};\n'
    + extract(src, 'function widgetCardEl(scope) {')
    + '\n' + extract(src, 'function widgetIdOf(scope) {')
    + '\n' + extract(src, 'function widgetField(scope, name) {')
    + '\n' + extract(src, 'function setWidgetMode(scope, mode) {')
    + '\n' + extract(src, 'function clearWidgetSelection(scope) {')
    + '\n' + extract(src, 'function showWidgetCreateForm(scope) {')
    + '\n' + extract(src, 'function selectExistingSupplier(scope, row) {')
    + '\n' + extract(src, 'function renderWidgetContacts(scope, row) {')
    + '\n' + extract(src, 'function widgetResultButtons(scope) {')
    + '\n' + extract(src, 'function isFocusableWidgetField(el) {')
    + '\n' + extract(src, 'function focusWidgetField(scope, within) {')
    + '\n' + extract(src, 'function revealWidgetCard(scope) {')
    + '\n' + extract(src, 'function highlightWidgetResult(scope, index) {')
    + '\n' + extract(src, 'function renderWidgetResults(scope, results, query) {')
    + '\n' + extract(src, 'function wireWidgetSearch(scope) {')
    + '\n' + extract(src, 'function wireShowWhen(target) {')
    + '\n' + extract(src, 'function watchWidgetWidth(card) {')
    + '\n' + extract(src, 'function wireWidget(scope) {');

  const factory = new Function(
    'document', 'fetch', 'Event', 'setTimeout', 'clearTimeout',
    'ResizeObserver', 'window', 'messages',
    code
      + '\nreturn {LOOKUP_MIN_CHARS: LOOKUP_MIN_CHARS, '
      + 'LOOKUP_DEBOUNCE_MS: LOOKUP_DEBOUNCE_MS, wireWidget: wireWidget, '
      + 'wireWidgetSearch: wireWidgetSearch, wireShowWhen: wireShowWhen, '
      + 'renderWidgetResults: renderWidgetResults, widgetResultButtons: widgetResultButtons, '
      + 'widgetField: widgetField, setWidgetMode: setWidgetMode, '
      + 'showWidgetCreateForm: showWidgetCreateForm, '
      + 'focusWidgetField: focusWidgetField, isFocusableWidgetField: isFocusableWidgetField, '
      + 'revealWidgetCard: revealWidgetCard, highlightWidgetResult: highlightWidgetResult};'
  );
  const api = factory(
    o.document || Dom(), o.fetch || makeFetch(), FakeEvent,
    o.setTimeout, o.clearTimeout,
    o.ResizeObserver || function StubResizeObserver() { this.observe = () => {}; },
    o.window || {addEventListener: () => {}},
    o.messages || {scrollTop: 0, rect: {top: 0, bottom: 0}, getBoundingClientRect() { return this.rect; }}
  );
  api.minChars = Number(min[1]);
  api.debounceMs = Number(debounce[1]);
  api.revealGap = Number(gap[1]);
  return api;
}

/** Wires the card the way production does: reveal first, then behaviour. */
function wire(runner, card) {
  [card.nameBlock, card.searchBlock, card.chosenBlock, card.createBlock]
    .forEach(runner.wireShowWhen);
  runner.wireWidgetSearch(card.card);
}

function type(card, value) {
  card.nameInput.value = value;
  card.nameInput.dispatchEvent({type: 'input', target: card.nameInput});
}

function press(card, key) {
  const ev = {type: 'keydown', key: key, target: card.nameInput, prevented: false,
    preventDefault() { this.prevented = true; }};
  card.nameInput.dispatchEvent(ev);
  return ev;
}

const ROW = {
  id: 7, name: 'Kraft Parts', country: 'Australia', currency: 'AUD',
  email: 'sales@kraft.example',
};

// --- checks -----------------------------------------------------------------

(async () => {
  const src = readTemplate(TEMPLATE);
  console.log(TEMPLATE);

  await check('a mode change reveals one view and hides the others', () => {
    const card = buildCard({mode: 'search'});
    const r = buildRunner(src);
    wire(r, card);

    eq(card.searchBlock.style.display, '', 'the search view is live in search mode');
    eq(card.chosenBlock.style.display, 'none', 'the chosen view starts hidden');
    eq(card.createBlock.style.display, 'none', 'the create view starts hidden');

    r.setWidgetMode(card.card, 'chosen');

    eq(card.mode.value, 'chosen', 'the mode field carries the view');
    eq(card.chosenBlock.style.display, '',
      'setting a hidden field value fires no change event, so the card must '
      + 'announce the switch itself — otherwise the view never appears');
    eq(card.searchBlock.style.display, 'none', 'the search view must withdraw');
    eq(card.chosenBlock.hidden, false,
      'revealing via the hidden attribute alone does nothing where a Tailwind '
      + 'display class is in play, so display itself must be cleared');
    eq(card.chosenBlock.style.display, '',
      'and an inline display is what the server-rendered style="display:none" '
      + 'actually competes with');
  });

  await check('a view can list several modes', () => {
    const card = buildCard({mode: 'search'});
    const r = buildRunner(src);
    wire(r, card);

    eq(card.nameBlock.style.display, '', 'the name field shows while searching');
    r.setWidgetMode(card.card, 'create');
    eq(card.nameBlock.style.display, '',
      'the name field is shared by search and create, so "mode=search,create" '
      + 'must accept both values');
    r.setWidgetMode(card.card, 'chosen');
    eq(card.nameBlock.style.display, 'none', 'but not the chosen view');
  });

  await check('typing searches and a pick fills the card', async () => {
    const card = buildCard({mode: 'search'});
    const f = makeFetch();
    const clock = makeClock();
    const r = buildRunner(src, {fetch: f, setTimeout: clock.setTimeout,
      clearTimeout: clock.clearTimeout});
    wire(r, card);

    type(card, 'kraft');
    clock.flush();
    await tick();

    eq(f.calls.length, 1, 'a settled keystroke issues one search');
    eq(f.calls[0].url, '/chat-ui/widgets/w1/lookup?q=kraft',
      'the search must hit the widget lookup route with the card\'s own id');

    f.reply(0, {ok: true, results: [ROW]});
    await tick();

    const btns = r.widgetResultButtons(card.card);
    eq(btns.length, 1, 'one result renders one row');
    assert(textOf(btns[0]).indexOf('Kraft Parts') !== -1, 'the row names the supplier');
    assert(textOf(btns[0]).indexOf('Australia') !== -1,
      'the row must carry something to tell same-named suppliers apart');

    eq(btns[0].type, 'button',
      'a bare <button> in a form submits it, so a click would submit the card '
      + 'before anything is chosen');

    let submitted = 0;
    card.form.addEventListener('submit', () => { submitted += 1; });
    btns[0].click();

    eq(submitted, 0, 'picking a result must not submit the card');
    eq(card.supplierId.value, 7, 'the id travels in a hidden field');
    eq(card.nameInput.value, 'Kraft Parts',
      'the canonical name replaces what was typed — the server compares the two');
    eq(card.mode.value, 'chosen', 'the card switches to the chosen view');
    eq(card.chosenBlock.style.display, '', 'and that view is revealed');
    eq(textOf(card.chosenName), 'Kraft Parts', 'the chosen view names the supplier');
    assert(textOf(card.chosenMeta).indexOf('Australia') !== -1,
      'the chosen view repeats the disambiguating detail');
  });

  await check('rapid keystrokes send one search', async () => {
    const card = buildCard({mode: 'search'});
    const f = makeFetch();
    const clock = makeClock();
    const r = buildRunner(src, {fetch: f, setTimeout: clock.setTimeout,
      clearTimeout: clock.clearTimeout});
    wire(r, card);

    type(card, 'k');
    type(card, 'kr');
    type(card, 'kra');
    clock.flush();
    await tick();

    eq(f.calls.length, 1, 'the debounce must coalesce keystrokes into one request');
    assert(f.calls[0].url.indexOf('q=kra') !== -1,
      'and the request that survives must be the newest: ' + f.calls[0].url);
  });

  await check('a slow earlier response cannot overwrite a newer one', async () => {
    const card = buildCard({mode: 'search'});
    const f = makeFetch();
    const clock = makeClock();
    const r = buildRunner(src, {fetch: f, setTimeout: clock.setTimeout,
      clearTimeout: clock.clearTimeout});
    wire(r, card);

    type(card, 'kr');
    clock.flush();
    await tick();
    type(card, 'kraft');
    clock.flush();
    await tick();
    eq(f.calls.length, 2, 'two searches are in flight');

    f.reply(1, {ok: true, results: [ROW]});
    await tick();
    f.reply(0, {ok: true, results: [{id: 99, name: 'Stale Result'}]});
    await tick();

    const btns = r.widgetResultButtons(card.card);
    eq(btns.length, 1, 'the list holds one response, not two appended lists');
    assert(textOf(btns[0]).indexOf('Kraft Parts') !== -1,
      'the older response arrived last and must not paint: without a sequence '
      + 'token the user picks from rows for a prefix they typed past');
  });

  await check('a query under the minimum is never sent', async () => {
    const card = buildCard({mode: 'search'});
    const f = makeFetch();
    const clock = makeClock();
    const r = buildRunner(src, {fetch: f, setTimeout: clock.setTimeout,
      clearTimeout: clock.clearTimeout});
    wire(r, card);

    type(card, 'k');
    clock.flush();
    await tick();

    eq(f.calls.length, 0,
      'one character matches most of the table, so the client must not ask');
    assert(card.status.textContent.indexOf('Start typing') !== -1,
      'and it should say what to do instead: ' + card.status.textContent);
    eq(card.results.children.length, 0, 'and show no rows');
    assert(r.minChars === 2, 'the check assumes a two-character floor');
  });

  await check('Enter picks the first result instead of submitting', async () => {
    const card = buildCard({mode: 'search'});
    const f = makeFetch();
    const clock = makeClock();
    const r = buildRunner(src, {fetch: f, setTimeout: clock.setTimeout,
      clearTimeout: clock.clearTimeout});
    wire(r, card);

    let submitted = 0;
    card.form.addEventListener('submit', () => { submitted += 1; });

    type(card, 'kraft');
    clock.flush();
    await tick();
    f.reply(0, {ok: true, results: [ROW]});
    await tick();

    const ev = press(card, 'Enter');

    eq(ev.prevented, true,
      'a form with one text input submits implicitly on Enter, so the lookup box '
      + 'must swallow it');
    eq(submitted, 0, 'and nothing may be submitted from the search box');
    eq(card.supplierId.value, 7, 'the first result is picked');
    eq(card.mode.value, 'chosen', 'and the card moves on to the chosen view');
  });

  await check('Enter with no matches offers the create form', async () => {
    const card = buildCard({mode: 'search'});
    const f = makeFetch();
    const clock = makeClock();
    const r = buildRunner(src, {fetch: f, setTimeout: clock.setTimeout,
      clearTimeout: clock.clearTimeout});
    wire(r, card);

    type(card, 'zzz');
    clock.flush();
    await tick();

    // Still in flight: Enter must not jump to "create" on a search that has not
    // answered yet — the supplier may well exist.
    press(card, 'Enter');
    eq(card.mode.value, 'search',
      'an unanswered search is not the same as "no matches"');
    assert(card.status.textContent.indexOf('Searching') !== -1,
      'and the status says a search is running');

    f.reply(0, {ok: true, results: []});
    await tick();
    assert(card.status.textContent.indexOf('No supplier matches') !== -1,
      'an empty result set is reported: ' + card.status.textContent);

    press(card, 'Enter');
    eq(card.mode.value, 'create', 'only a finished, empty search offers the form');
    eq(card.createBlock.style.display, '', 'and reveals it');
    eq(card.contact.focused, true, 'focusing the first field so typing can continue');
  });

  await check('editing the name drops a previous pick', async () => {
    const card = buildCard({mode: 'search'});
    const f = makeFetch();
    const clock = makeClock();
    const r = buildRunner(src, {fetch: f, setTimeout: clock.setTimeout,
      clearTimeout: clock.clearTimeout});
    wire(r, card);

    type(card, 'kraft');
    clock.flush();
    await tick();
    f.reply(0, {ok: true, results: [ROW]});
    await tick();
    r.widgetResultButtons(card.card)[0].click();
    eq(card.supplierId.value, 7, 'picked');

    type(card, 'kraft par');

    eq(card.supplierId.value, '',
      'the id no longer describes the typed name, and the server refuses a '
      + 'mismatched pair — so the stale id must be dropped here');
  });

  await check('the create and change buttons switch views', async () => {
    const card = buildCard({mode: 'search'});
    const f = makeFetch();
    const clock = makeClock();
    const r = buildRunner(src, {fetch: f, setTimeout: clock.setTimeout,
      clearTimeout: clock.clearTimeout});
    wire(r, card);

    type(card, 'craft');
    clock.flush();
    await tick();
    f.reply(0, {ok: true, results: [ROW]});
    await tick();
    r.widgetResultButtons(card.card)[0].click();

    // "Change" returns to the search box with the query intact, so it re-searches
    // straight away rather than leaving an empty list behind.
    const before = f.calls.length;
    card.changeBtn.click();
    eq(card.mode.value, 'search', '"Change" goes back to searching');
    eq(card.supplierId.value, '', 'and drops the pick');
    eq(card.searchBlock.style.display, '', 'revealing the search view');
    eq(f.calls.length, before + 1, 'and searching again for what is in the box');
    eq(card.nameInput.focused, true, 'with the cursor back in the name field');

    card.createBtn.click();
    eq(card.mode.value, 'create', '"Create a new supplier" opens the form');
    eq(card.createBlock.style.display, '', 'and reveals it');
    eq(card.contact.focused, true, 'focusing the first field');
    eq(card.supplierId.value, '',
      'the create path must not carry a picked id, or the server reads it as an '
      + 'attempt to link the existing supplier');
  });

  await check('a re-rendered search card re-runs its query', async () => {
    const card = buildCard({mode: 'search', name: 'kraft'});
    const f = makeFetch();
    const clock = makeClock();
    const r = buildRunner(src, {fetch: f, setTimeout: clock.setTimeout,
      clearTimeout: clock.clearTimeout});
    wire(r, card);
    await tick();

    eq(f.calls.length, 1,
      'a card that comes back in search mode with a name in the box (a stale '
      + 'pick, say) must show its matches again — otherwise the user faces an '
      + 'empty list and retypes');
    assert(f.calls[0].url.indexOf('q=kraft') !== -1, 'for what is in the box');

    const chosenCard = buildCard({mode: 'chosen', name: 'Kraft Parts'});
    const f2 = makeFetch();
    const r2 = buildRunner(src, {fetch: f2, setTimeout: clock.setTimeout,
      clearTimeout: clock.clearTimeout});
    wire(r2, chosenCard);
    await tick();
    eq(f2.calls.length, 0,
      'but a card showing a chosen supplier must not search behind the summary');
  });

  await check('a growing results list is scrolled out from under the composer', async () => {
    const card = buildCard({mode: 'search'});
    const f = makeFetch();
    const clock = makeClock();
    // The card's bottom edge sits 100px below the messages box's bottom: the last
    // rows (and the create button) are behind the composer until it scrolls.
    const box = {top: 0, bottom: 320, left: 0, right: 400};
    const view = {top: 0, bottom: 300, left: 0, right: 400};
    const messages = {
      scrollTop: 0, rect: view, getBoundingClientRect() { return this.rect; },
    };
    card.card.rect = {top: 20, bottom: 400, left: 0, right: 400};
    card.results.rect = box;
    const r = buildRunner(src, {fetch: f, setTimeout: clock.setTimeout,
      clearTimeout: clock.clearTimeout, messages: messages});
    wire(r, card);

    type(card, 'kraft');
    clock.flush();
    await tick();
    f.reply(0, {ok: true, results: [ROW]});
    await tick();

    eq(messages.scrollTop, 400 - (300 - r.revealGap),
      'the panel must move up by exactly the amount the card overflows — any '
      + 'more and it jumps past content the user was reading');

    // Long enough to sit fully below the visible area (scrolled up in a long
    // thread): must not drag the panel to the bottom.
    messages.scrollTop = 0;
    card.card.rect = {top: 900, bottom: 1200, left: 0, right: 400};
    r.revealWidgetCard(card.card);
    eq(messages.scrollTop, 0, 'a card the user cannot see must be left alone');

    // Already visible: nothing to do at all.
    messages.scrollTop = 50;
    card.card.rect = {top: 20, bottom: 100, left: 0, right: 400};
    r.revealWidgetCard(card.card);
    eq(messages.scrollTop, 50, 'a card that already fits must not move the panel');
  });

  await check('the list scrolls internally to follow the keyboard highlight', () => {
    const card = buildCard({mode: 'search'});
    const r = buildRunner(src);
    wire(r, card);

    r.renderWidgetResults(card.card, [ROW, ROW, ROW], 'kraft');
    const rows = r.widgetResultButtons(card.card);
    // A 100px-tall list holding three 40px rows: the third one pokes 20px below.
    card.results.rect = {top: 100, bottom: 200, left: 0, right: 400};
    const rowRects = rows.map((row, index) => {
      const top = 100 + index * 40;
      row.rect = {top: top, bottom: top + 40, left: 0, right: 400};
      return row.rect;
    });

    r.highlightWidgetResult(card.card, 0);
    eq(card.results.scrollTop, 0, 'the first row is already in view');

    r.highlightWidgetResult(card.card, 2);
    eq(card.results.scrollTop, 20,
      'arrowing to a row below the fold must scroll the list by just enough to '
      + 'show it — never the page, and never further than it needs');

    // Scrolling the list moves the rows up, which is what a browser would do.
    card.results.scrollTop = 20;
    rowRects.forEach((rect) => { rect.top -= 20; rect.bottom -= 20; });

    r.highlightWidgetResult(card.card, 0);
    eq(card.results.scrollTop, 0, 'and back up again when the highlight returns');
  });

  await check('the caret goes to the first field the user can see', () => {
    const card = buildCard({mode: 'search', name: 'kraft'});
    const r = buildRunner(src);
    wire(r, card);

    assert(r.focusWidgetField(card.card) === true, 'the card has a field to focus');
    eq(card.nameInput.focused, true,
      'the typeahead is the card\'s first visible field — the hidden mode and '
      + 'supplier_id inputs come first in the DOM');
    eq(card.nameInput.selection, [5, 5],
      'the caret goes to the end of what is already typed, not selecting it');

    // A view that is not showing must not be focusable: its fields are in the DOM
    // the whole time, so focus would land somewhere invisible.
    card.nameInput.focused = false;
    r.setWidgetMode(card.card, 'chosen');
    assert(r.focusWidgetField(card.card) === false,
      'in the chosen view the only fields are hidden ones — focus nothing');

    r.setWidgetMode(card.card, 'create');
    r.focusWidgetField(card.card);
    eq(card.nameInput.focused, true, 'back in create, the shared name field is first');

    // "Create a new supplier" targets the create view's own first field instead:
    // the name is already filled in, so the next thing to fill in is below it.
    card.nameInput.focused = false;
    r.showWidgetCreateForm(card.card);
    eq(card.contact.focused, true, 'the create button continues at the next field');
    eq(card.nameInput.focused, false, 'and does not go back to the name');
    assert(r.isFocusableWidgetField(card.mode) === false, 'a hidden input is not a field');
  });

  await check('a supplier with several contacts is asked about', async () => {
    const card = buildCard({mode: 'search'});
    const f = makeFetch();
    const clock = makeClock();
    const r = buildRunner(src, {fetch: f, setTimeout: clock.setTimeout,
      clearTimeout: clock.clearTimeout});
    wire(r, card);

    // The options travel with the search result: the pick happens in the browser,
    // so nothing the server rendered could know who to offer.
    const multi = Object.assign({}, ROW, {
      contacts: [
        {id: 'c-source', label: 'Source', name: 'Corey', email: 'corey@iveco.example'},
        {id: 'c-parts', label: 'Main', name: '', email: 'parts@iveco.example'},
      ],
    });
    type(card, 'iveco');
    clock.flush();
    await tick();
    f.reply(0, {ok: true, results: [multi]});
    await tick();
    r.widgetResultButtons(card.card)[0].click();

    const select = card.chosenContacts.querySelector('select');
    assert(select, 'the picker must be built where the card can show it');
    eq(select.name, 'contact_id', 'so the choice submits with the form');
    eq(select.children.map((o) => o.value), ['c-source', 'c-parts'],
      'the Go Source contact leads, as it does everywhere else');
    eq(select.children[0].selected, true, 'and is what the card would have used');
    eq(select.children[0].textContent, 'Source — Corey — corey@iveco.example',
      'role, then the person, then the address: the name is how you know who you '
      + 'are writing to, and two addresses at one supplier look alike without it');
    eq(select.children[1].textContent, 'Main — parts@iveco.example',
      'a contact with no name skips it rather than showing a blank');
    eq(card.chosenMeta.style.display, 'none',
      'the summary line steps aside: one place has to say who this goes to');
  });

  await check('one contact is not a question', async () => {
    const card = buildCard({mode: 'search'});
    const f = makeFetch();
    const clock = makeClock();
    const r = buildRunner(src, {fetch: f, setTimeout: clock.setTimeout,
      clearTimeout: clock.clearTimeout});
    wire(r, card);

    const single = Object.assign({}, ROW, {
      contacts: [{id: 'c1', label: 'Source', email: 'sales@kraft.example'}],
    });
    type(card, 'kraft');
    clock.flush();
    await tick();
    f.reply(0, {ok: true, results: [single]});
    await tick();
    r.widgetResultButtons(card.card)[0].click();

    assert(!card.chosenContacts.querySelector('select'),
      'a picker with one option is a fake choice');
    eq(card.chosenMeta.style.display, '',
      'so the summary line stays, and it is the address that an email would use');
    assert(card.chosenMeta.textContent.indexOf('sales@kraft.example') !== -1,
      card.chosenMeta.textContent);
  });

  await check('re-picking replaces the question rather than stacking it', async () => {
    const card = buildCard({mode: 'search'});
    const f = makeFetch();
    const clock = makeClock();
    const r = buildRunner(src, {fetch: f, setTimeout: clock.setTimeout,
      clearTimeout: clock.clearTimeout});
    wire(r, card);

    const two = [
      Object.assign({}, ROW, {
        name: 'First Co',
        contacts: [{id: 'a1', email: 'one@a.example'}, {id: 'a2', email: 'two@a.example'}],
      }),
      Object.assign({}, ROW, {
        id: 8, name: 'Second Co',
        contacts: [{id: 'b1', email: 'one@b.example'}, {id: 'b2', email: 'two@b.example'}],
      }),
    ];
    type(card, 'co');
    clock.flush();
    await tick();
    f.reply(0, {ok: true, results: two});
    await tick();

    const rows = r.widgetResultButtons(card.card);
    rows[0].click();
    rows[1].click();

    const selects = card.chosenContacts.querySelectorAll('select');
    eq(selects.length, 1, 'two picks must not leave two pickers submitting two ids');
    eq(selects[0].children[0].value, 'b1', 'and it belongs to the supplier picked last');

    // A supplier with one contact afterwards: the picker must go, not linger.
    type(card, 'co');
    clock.flush();
    await tick();
    f.reply(1, {ok: true, results: [Object.assign({}, ROW, {id: 9, name: 'Solo Co',
      contacts: [{id: 'c9', email: 'solo@c.example'}]})]});
    await tick();
    r.widgetResultButtons(card.card)[0].click();

    eq(card.chosenContacts.querySelectorAll('select').length, 0,
      'the previous supplier\'s picker must not be left behind submitting its id');
    eq(card.chosenMeta.style.display, '');
  });

  await check('wireWidget is what actually attaches the search', async () => {
    const card = buildCard({mode: 'search'});
    const f = makeFetch();
    const clock = makeClock();
    const r = buildRunner(src, {fetch: f, setTimeout: clock.setTimeout,
      clearTimeout: clock.clearTimeout});

    // The slot holds the card, as it does after renderWidget injects the markup.
    r.wireWidget(card.card);

    type(card, 'kraft');
    clock.flush();
    await tick();
    eq(f.calls.length, 1,
      'wireWidget must reach wireWidgetSearch, or the card is inert in a browser '
      + 'even though every function above passes on its own');

    // Idempotent: a submit re-renders and re-wires the same card.
    r.wireWidget(card.card);
    type(card, 'kraft p');
    clock.flush();
    await tick();
    eq(f.calls.length, 2, 're-wiring must not double every request');
  });

  finish();
})();
