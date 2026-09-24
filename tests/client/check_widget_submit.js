/**
 * Widget card behaviour, driven against the real template source.
 *
 * Why these checks exist — each was invisible to both `pytest` and
 * `node --check`:
 *
 *  1. `new FormData(form)` does NOT include the submit button that was pressed.
 *     The widget protocol relies on that value to tell "Add supplier" from
 *     "Cancel", so without an explicit `fd.set(actionField, …)` a Cancel would
 *     arrive as a submit and create the supplier anyway.
 *  2. A rejected submission (validation errors render per-field, so the response
 *     carries no HTML) must hand the buttons back; leaving them disabled strands
 *     the user with a form they cannot retry.
 *  3. `data-show-when` is how a widget's conditional sections work. Injected
 *     markup does not execute its own <script>, so if this wiring breaks the
 *     "which lines?" picker simply never appears — silently.
 */

'use strict';

const {
  readTemplate, extract, assert, eq, check, finish, tick,
} = require('./harness');

const TEMPLATE = 'templates/chat_ui/embed.html';

// --- stubs -----------------------------------------------------------------

function stubEl(tag) {
  return {
    tag: tag || 'div',
    dataset: {},
    hidden: false,
    innerHTML: '',
    className: '',
    disabled: false,
    children: [],
    listeners: {},
    appendChild(n) { this.children.push(n); return n; },
    addEventListener(type, fn) {
      (this.listeners[type] = this.listeners[type] || []).push(fn);
    },
    querySelectorAll() { return []; },
    querySelector() { return null; },
    closest() { return null; },
    // Mirrors harness.makeDom(): a removal is recorded, not performed.
    remove() { this.removed = true; },
  };
}

/** Builds the {form, card, buttons} triple onWidgetSubmit operates on. */
function makeWidget(opts) {
  const o = opts || {};
  const card = stubEl('div');
  card.dataset.widgetId = 'w1';

  const form = stubEl('form');
  form.action = '/chat-ui/widgets/w1/submit';
  form.dataset.actionField = '__widget_action';
  form.dataset.busy = '';
  form.closest = () => card;

  const buttons = [stubEl('button'), stubEl('button')];
  form.querySelectorAll = (sel) => (sel === 'button' ? buttons : []);

  return {card, form, buttons, seed: o.seed || {}};
}

/** FormData stand-in: the point of the check is what gets SET on it. */
let lastFormData = null;
function FakeFormData() {
  this.entries = {};
  this.set = (key, value) => { this.entries[key] = value; };
  this.get = (key) => this.entries[key];
  lastFormData = this;
}

function buildRunner(src, deps) {
  // The threshold lives at template scope, so brace-matching cannot capture it —
  // read its real value rather than duplicating it here, or the check keeps
  // passing after someone changes the number.
  const threshold = /var NARROW_CARD_PX = (\d+)/.exec(src);
  if (!threshold) throw new Error('NARROW_CARD_PX not found in the template');

  const code = 'var NARROW_CARD_PX = ' + threshold[1] + ';\n'
    + extract(src, 'function wireWidget(scope) {')
    + '\n' + extract(src, 'function wireShowWhen(target) {')
    + '\n' + extract(src, 'function onWidgetSubmit(ev) {')
    + '\n' + extract(src, 'function widgetRow(id) {')
    + '\n' + extract(src, 'function dropWidgetRow(el) {')
    + '\n' + extract(src, 'function watchWidgetWidth(card) {')
    + '\n' + extract(src, 'function widgetCardEl(scope) {')
    + '\n' + extract(src, 'function renderWidget(entry, widget) {')
    + '\n' + extract(src, 'function openWidget(widget) {');
  const factory = new Function(
    'document', 'FormData', 'fetch', 'getRow', 'setText', 'showError', 'messages',
    'toolsMenu', 'active', 'runs', 'rows', 'CustomEvent', 'ResizeObserver', 'window',
    code
      + '\nreturn {wireWidget: wireWidget, wireShowWhen: wireShowWhen, '
      + 'onWidgetSubmit: onWidgetSubmit, widgetRow: widgetRow, '
      + 'dropWidgetRow: dropWidgetRow, watchWidgetWidth: watchWidgetWidth, '
      + 'widgetCardEl: widgetCardEl, renderWidget: renderWidget, '
      + 'openWidget: openWidget};'
  );
  const api = factory(
    deps.document, FakeFormData, deps.fetch, deps.getRow, deps.setText,
    deps.showError, deps.messages, deps.toolsMenu, deps.active, deps.runs,
    deps.rows, FakeCustomEvent, deps.ResizeObserver, deps.window
  );
  api.threshold = Number(threshold[1]);
  return api;
}

/** Deterministic stand-in for the browser's CustomEvent. */
function FakeCustomEvent(type, opts) {
  return {type: type, detail: (opts || {}).detail};
}

/** ResizeObserver stand-in that records its targets and exposes the callback. */
function makeResizeObserver() {
  const created = [];
  function Stub(callback) {
    this.cb = callback;
    this.observe = (el) => { this.target = el; };
    created.push(this);
  }
  Stub.created = created;
  return Stub;
}

function deps(overrides) {
  const o = overrides || {};
  const events = o.events || [];
  return {
    document: {
      createElement: stubEl,
      dispatchEvent: (e) => events.push(e),
    },
    events: events,
    fetch: o.fetch || (() => Promise.resolve({ok: true, json: () => Promise.resolve({})})),
    getRow: o.getRow || (() => stubEl('div')),
    setText: o.setText || (() => {}),
    showError: o.showError || (() => {}),
    messages: {scrollTop: 0},
    toolsMenu: o.toolsMenu || stubEl('div'),
    active: o.active === undefined ? {id: 'thread-1'} : o.active,
    runs: o.runs || {},
    rows: o.rows || {},
    ResizeObserver: o.ResizeObserver || makeResizeObserver(),
    window: o.window || {addEventListener: () => {}},
  };
}

function submitEvent(form, submitter) {
  return {
    currentTarget: form,
    submitter: submitter,
    preventDefault() { this.prevented = true; },
  };
}

// --- checks ----------------------------------------------------------------

(async () => {
  const src = readTemplate(TEMPLATE);
  console.log(TEMPLATE);

  await check('the pressed button travels with the form data', async () => {
    const w = makeWidget();
    const r = buildRunner(src, deps());
    const cancel = {value: 'cancel'};
    lastFormData = null;

    r.onWidgetSubmit(submitEvent(w.form, cancel));
    await tick();

    assert(lastFormData, 'the submission must build a FormData');
    eq(lastFormData.get('__widget_action'), 'cancel',
      'without an explicit set(), FormData omits the submitter and the server '
      + 'reads this as an "Add supplier" click');
  });

  await check('no submitter still submits rather than cancelling', async () => {
    const w = makeWidget();
    const r = buildRunner(src, deps());
    lastFormData = null;

    r.onWidgetSubmit(submitEvent(w.form, undefined));
    await tick();

    assert(lastFormData, 'the submission must build a FormData');
    assert(lastFormData.get('__widget_action') === undefined,
      'an absent submitter must leave the action unset so the server defaults to submit');
  });

  await check('a rejected submission hands the controls back', async () => {
    const w = makeWidget();
    const seen = [];
    const r = buildRunner(src, deps({
      fetch: () => Promise.resolve({
        ok: false, status: 400,
        json: () => Promise.resolve({error: 'Please fix the highlighted fields.'}),
      }),
      showError: (msg) => seen.push(msg),
    }));

    r.onWidgetSubmit(submitEvent(w.form, {value: 'submit'}));
    await tick();

    assert(w.buttons.every((b) => b.disabled === false),
      'buttons must be re-enabled when there is no card markup to replace');
    assert(w.form.dataset.busy === '',
      'the in-flight guard must be cleared or the form is permanently stuck');
    eq(seen, ['Please fix the highlighted fields.'], 'the error should be surfaced');
  });

  await check('a successful submission swaps in the returned card', async () => {
    const w = makeWidget();
    const rows = [];
    const printed = [];
    const r = buildRunner(src, deps({
      fetch: () => Promise.resolve({
        ok: true,
        json: () => Promise.resolve({
          ok: true, status: 'submitted', html: '<div data-widget="add_supplier">done</div>',
          notice: {id: 'step-9', content: '✅ Created supplier **Acme**.'},
        }),
      }),
      getRow: (id) => { rows.push(id); return stubEl('div'); },
      setText: (entry, text) => printed.push(text),
    }));

    r.onWidgetSubmit(submitEvent(w.form, {value: 'submit'}));
    await tick();

    assert(w.card.innerHTML.indexOf('done') !== -1,
      'the card must render the HTML the server returned');
    eq(rows, ['step-9'],
      'the transcript line must be keyed on its persisted step id, or a reload '
      + 'renders it a second time');
    assert(printed.length === 1 && printed[0].indexOf('Created supplier') !== -1,
      'the notice text should be written into the row');
  });

  await check('a double click does not submit twice', async () => {
    const w = makeWidget();
    let calls = 0;
    const r = buildRunner(src, deps({
      fetch: () => { calls += 1; return new Promise(() => {}); }, // never settles
    }));

    r.onWidgetSubmit(submitEvent(w.form, {value: 'submit'}));
    r.onWidgetSubmit(submitEvent(w.form, {value: 'submit'}));
    await tick();

    eq(calls, 1, 'the busy guard must reject the second submit');
  });

  await check('data-show-when reveals its section only for the matching value', async () => {
    const target = stubEl('div');
    target.dataset.showWhen = 'line_mode=specific';
    target.hidden = true;

    const form = stubEl('form');
    form.closest = () => null;
    let checked = {value: 'all'};
    form.addEventListener = function (type, fn) {
      (this.listeners[type] = this.listeners[type] || []).push(fn);
    };
    form.querySelector = (sel) => (sel.indexOf('line_mode') !== -1 ? checked : null);
    target.closest = (sel) => (sel === 'form' ? form : null);

    const r = buildRunner(src, deps());
    r.wireShowWhen(target);
    assert(target.hidden === true,
      '"All lines" must leave the per-line picker hidden');

    checked = {value: 'specific'};
    form.listeners.change.forEach((fn) => fn({target: {name: 'line_mode'}}));
    assert(target.hidden === false,
      'picking "Specific lines" must reveal the line checkboxes');

    checked = {value: 'none'};
    form.listeners.change.forEach((fn) => fn({target: {name: 'line_mode'}}));
    assert(target.hidden === true, 'switching away must hide it again');
  });

  await check('opening a widget keys the row on its persisted step id', async () => {
    const calls = [];
    const rowIds = [];
    const toolsMenu = stubEl('div');
    toolsMenu.hidden = false;
    const entry = stubEl('div');
    entry.div = stubEl('div');
    entry.widgetEl = stubEl('div');
    entry.bubble = stubEl('div');

    const r = buildRunner(src, deps({
      toolsMenu: toolsMenu,
      fetch: (url, opts) => {
        calls.push({url: url, opts: opts});
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({
            ok: true, widget_id: 'step-7', html: '<div data-widget="add_supplier">form</div>',
          }),
        });
      },
      getRow: (id) => { rowIds.push(id); return entry; },
    }));

    r.openWidget({name: 'add_supplier', label: 'Add new supplier'});
    await tick();

    eq(calls.length, 1, 'opening must make exactly one request');
    assert(calls[0].url === '/chat-ui/threads/thread-1/widgets',
      'the widget opens against the thread, not the widget id: ' + calls[0].url);
    eq(JSON.parse(calls[0].opts.body), {name: 'add_supplier'},
      'the server needs the widget name to know what to open');
    eq(rowIds, ['step-7'],
      'the row must be keyed on the persisted step id — a random id renders the '
      + 'same form twice after a reload');
    assert(entry.widgetEl.innerHTML.indexOf('form') !== -1,
      'the returned card markup must land in the widget slot');
    assert(entry.bubble.hidden === true,
      'a widget row has no message text, so its empty bubble renders as a stray '
      + 'grey pill above the card unless it is hidden');
    assert(toolsMenu.hidden === true, 'the menu should close once the card opens');
  });

  await check('opening a widget while a run is active is refused', async () => {
    const seen = [];
    let fetched = 0;
    const r = buildRunner(src, deps({
      active: {id: 'thread-1'},
      runs: {'thread-1': true},
      fetch: () => { fetched += 1; return Promise.resolve({ok: true, json: () => Promise.resolve({})}); },
      showError: (m) => seen.push(m),
    }));

    r.openWidget({name: 'add_supplier'});
    await tick();

    eq(fetched, 0, 'a running turn owns the thread — no request should be sent');
    assert(seen.length === 1, 'the refusal must be explained, not silent');
  });

  await check('a submit that changed the RFQ tells the shell to refresh', async () => {
    const w = makeWidget();
    const d = deps({
      fetch: () => Promise.resolve({
        ok: true,
        json: () => Promise.resolve({
          ok: true, status: 'submitted', html: '<div>done</div>',
          dashboard: {command: 'dashboard_refresh', payload: {rfq_id: 'RFQ-2026-1231'}},
        }),
      }),
    });
    const r = buildRunner(src, d);

    r.onWidgetSubmit(submitEvent(w.form, {value: 'submit'}));
    await tick();

    eq(d.events.length, 1, 'exactly one shell command should be raised');
    eq(d.events[0].type, 'dashboard:dashboard_refresh',
      'the shell listens for this event name (base.html) — anything else is ignored');
    eq(d.events[0].detail.payload, {rfq_id: 'RFQ-2026-1231', _source_thread: 'thread-1'},
      'the payload must carry _source_thread, or the shell cannot scope the '
      + 'refresh to the RFQ it belongs to');
  });

  await check('a submit that changed nothing raises no shell command', async () => {
    const w = makeWidget();
    const d = deps({
      fetch: () => Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ok: true, status: 'submitted', html: '<div>done</div>'}),
      }),
    });
    const r = buildRunner(src, d);

    r.onWidgetSubmit(submitEvent(w.form, {value: 'submit'}));
    await tick();

    eq(d.events.length, 0,
      'a supplier added with no RFQ link leaves the page current — refreshing it '
      + 'would be a pointless re-render');
  });

  await check('a failed submit raises no shell command', async () => {
    const w = makeWidget();
    const d = deps({
      fetch: () => Promise.resolve({
        ok: false, status: 400,
        json: () => Promise.resolve({error: 'Please fix the highlighted fields.'}),
      }),
    });
    const r = buildRunner(src, d);

    r.onWidgetSubmit(submitEvent(w.form, {value: 'submit'}));
    await tick();

    eq(d.events.length, 0, 'nothing changed, so nothing needs refreshing');
  });

  await check('a widget row spans the chat width', () => {
    const entry = {
      div: stubEl('div'),
      inner: stubEl('div'),
      bubble: stubEl('div'),
      widgetEl: stubEl('div'),
      _live: false,
    };
    entry.inner.className = 'chat-bubble-inner mr-auto';
    const r = buildRunner(src, deps({getRow: () => entry}));

    const row = r.widgetRow('w1');

    assert(row.inner.className === 'w-full',
      'the row must drop chat-bubble-inner: that is a shrink-to-fit flex item, so '
      + 'the card is only ever as wide as its own max-content');
    assert(entry.bubble.hidden === true, 'the empty bubble stays hidden');
    eq(entry.div.dataset.widgetRow, 'w1',
      'the row must be findable, or a cancel cannot remove it');
  });

  await check('the card width decides the one-column layout', () => {
    const card = stubEl('div');
    card.dataset.widget = 'add_supplier';
    const RO = makeResizeObserver();
    const r = buildRunner(src, deps({ResizeObserver: RO}));

    card.clientWidth = r.threshold - 20;      // just under
    r.watchWidgetWidth(card);
    assert(card.dataset.widgetNarrow === '1',
      'a card narrower than ' + r.threshold + 'px must stack');
    eq(RO.created.length, 1, 'the card should be observed');

    card.clientWidth = r.threshold + 120;     // comfortably over
    RO.created[0].cb();
    assert(card.dataset.widgetNarrow === undefined,
      'widening past the threshold must restore two columns — a one-way switch '
      + 'would leave the form stacked forever');

    card.clientWidth = r.threshold - 20;
    RO.created[0].cb();
    assert(card.dataset.widgetNarrow === '1', 'and narrowing must stack again');
  });

  await check('re-wiring the same card does not observe it twice', () => {
    const card = stubEl('div');
    card.dataset.widget = 'add_supplier';
    card.clientWidth = 500;
    const RO = makeResizeObserver();
    const r = buildRunner(src, deps({ResizeObserver: RO}));

    r.watchWidgetWidth(card);
    r.watchWidgetWidth(card);   // every re-render calls wireWidget again

    eq(RO.created.length, 1, 'a second observer per render would leak');
  });

  await check('a hidden card is not marked narrow', () => {
    const card = stubEl('div');
    card.dataset.widget = 'add_supplier';
    card.clientWidth = 0;      // a hidden element reports 0
    const r = buildRunner(src, deps());

    r.watchWidgetWidth(card);

    assert(card.dataset.widgetNarrow === undefined,
      'a card with no measurable width must not decide it is narrow, or it '
      + 'renders stacked when it appears');
  });

  await check('wireWidget measures the card it was handed', () => {
    const card = stubEl('div');
    card.dataset.widget = 'add_supplier';
    const slot = stubEl('div');
    slot.querySelector = (sel) => (sel === '[data-widget]' ? card : null);
    slot.querySelectorAll = () => [];
    const r = buildRunner(src, deps());
    card.clientWidth = r.threshold - 20;

    // renderWidget/openWidget pass the SLOT, onWidgetSubmit passes the card.
    r.wireWidget(slot);

    assert(card.dataset.widgetNarrow === '1',
      'the slot path must find the card inside it, or a card opened from the '
      + 'Tools menu never stacks');
  });

  await check('cancelling removes the card from the conversation', async () => {
    const w = makeWidget();
    const row = stubEl('div');
    row.dataset.widgetRow = 'w1';
    w.card.closest = (sel) => (sel === '[data-widget-row]' ? row : null);
    const rows = {w1: {div: row}};
    const d = deps({
      rows: rows,
      fetch: () => Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ok: true, status: 'cancelled', removed: true}),
      }),
    });
    const r = buildRunner(src, d);

    r.onWidgetSubmit(submitEvent(w.form, {value: 'cancel'}));
    await tick();

    assert(row.removed === true,
      'the whole row must go — a "nothing was saved" note is noise');
    assert(!rows.w1,
      'and the row cache must forget it, or a later repaint resurrects it');
    eq(d.events.length, 0, 'a cancel changed nothing, so nothing to refresh');
  });

  await check('a legacy cancelled step is dropped without fetching', async () => {
    const slot = stubEl('div');
    const row = stubEl('div');
    row.dataset.widgetRow = 'old-1';
    slot.closest = (sel) => (sel === '[data-widget-row]' ? row : null);
    const rows = {'old-1': {div: row}};
    let fetches = 0;
    const d = deps({
      rows: rows,
      fetch: () => { fetches += 1; return Promise.resolve({ok: true, json: () => Promise.resolve({})}); },
    });
    const r = buildRunner(src, d);

    // Steps saved before cancel deleted them still say "cancelled".
    r.renderWidget({widgetEl: slot}, {id: 'old-1', name: 'add_supplier', status: 'cancelled'});
    await tick();

    assert(row.removed === true, 'an old tombstone must go too');
    eq(fetches, 0, 'no point fetching a card that will never be shown');
  });

  finish();
})();
