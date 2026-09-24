/**
 * _sendAction must attach the chat stream only AFTER the run is registered.
 *
 * Regression: it dispatched `chat-ui:action-started` (which opens the
 * EventSource) BEFORE POSTing to /api/agent-bridge. The stream endpoint answers
 * `done` immediately when no run is registered yet, and a run's event queue is
 * single-consumer with no replay — so `dashboard_refresh` and `agent_done` were
 * stranded: the RFQ page never updated and the working badge spun for ten
 * minutes. In the self-heal case (page had no bound thread) it attached nothing
 * at all, because the follow-up branch read members of a different Alpine
 * component and threw into a bare .catch.
 */

'use strict';

const {
  readTemplate, extract, quietConsole, assert, eq, tick, check, finish,
} = require('./harness');

const BASE = 'templates/base.html';

class CustomEventStub {
  constructor(type, opts) {
    this.type = type;
    this.detail = (opts && opts.detail) || {};
  }
}

/**
 * Build _sendAction with stubs. The POST is held open so the check can assert
 * what has and has not happened before it resolves — that ordering is the bug.
 */
function build(src, pageThreadId, serverThreadId) {
  const dispatched = [];
  const documentStub = {
    dispatchEvent: (e) => { dispatched.push({ type: e.type, detail: e.detail }); return true; },
    querySelector: (sel) => (sel === '[data-dashboard-context]'
      ? {
        dataset: {
          dashboardContext: JSON.stringify({
            view: 'rfq_detail', id: 'RFQ-2026-1054', thread_id: pageThreadId,
          }),
        },
      }
      : null),
    getElementById: () => null, // same-document embed: there is no #agent-iframe
  };
  const windowStub = { dispatchEvent: () => true, eaToast: () => {} };

  let release = null;
  const fetchStub = () => new Promise((res) => {
    release = () => res({
      ok: true,
      status: 200,
      redirected: false,
      headers: { get: () => 'application/json' },
      json: () => Promise.resolve({ started: true, thread_id: serverThreadId }),
    });
  });

  const code = extract(src, '_sendAction(action, refreshAfter) {');
  const factory = new Function(
    'document', 'window', 'fetch', 'htmx', 'CustomEvent', 'console',
    'var c = { rfqId: "RFQ-2026-1054", _actionFailed: function () {}, '
      + code + '}; return c;'
  );
  const c = factory(documentStub, windowStub, fetchStub, { ajax: () => {} },
    CustomEventStub, quietConsole);

  const starts = () => dispatched.filter((d) => d.type === 'chat-ui:action-started');
  return { c, starts, release: () => release && release() };
}

const ACTION = { name: 'rfq_find_brand_suppliers', label: 'Finding brand-linked suppliers…' };

(async () => {
  const src = readTemplate(BASE);

  await check('the POST is sent with the page thread as a hint', async () => {
    const h = build(src, 'T-page', 'T-page');
    h.c._sendAction(ACTION);
    const posted = h.starts().length === 0;
    assert(posted, 'nothing should be attached yet');
    h.release();
    await tick();
    eq(h.starts().length, 1, 'exactly one attach after the POST resolves');
  });

  await check('normal: attaches only after the POST resolves, to the server thread', async () => {
    const h = build(src, 'T-page', 'T-page');
    h.c._sendAction(ACTION);
    eq(h.starts().length, 0,
      'the stream must not be attached before the run is registered — the server '
      + 'answers `done` immediately and the run\'s events are then unreachable');
    h.release();
    await tick();
    eq(h.starts().length, 1, 'exactly one attach after the POST resolves');
    eq(h.starts()[0].detail.threadId, 'T-page', 'attach to the thread the server used');
  });

  await check('self-heal: attaches to the thread the server resolved, not the page', async () => {
    const h = build(src, '', 'T-server');
    h.c._sendAction(ACTION);
    eq(h.starts().length, 0, 'nothing may attach before the run is registered');
    h.release();
    await tick();
    eq(h.starts().length, 1,
      'the page had no bound thread, so the server resolved one — the panel must '
      + 'still attach (this used to dispatch nothing at all)');
    eq(h.starts()[0].detail.threadId, 'T-server', 'follow the server-resolved thread');
  });

  finish();
})();
