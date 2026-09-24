/**
 * NetSuite supplier modal: response handling, and the submit button's binding.
 *
 * Regressions covered:
 *  1. A redirected / non-JSON response was read as SUCCESS. require_user answers
 *     an expired session with 303 -> /login; fetch follows it, json() throws, the
 *     catch returned {}, and `if (!r.ok)` passed — so the modal closed claiming
 *     "Supplier added to NetSuite ✓" while nothing had been written.
 *  2. The duplicate guard's remedy was unreachable: the 400 carried prose only and
 *     the confirm checkbox came from page-render-time data, so a duplicate flagged
 *     at submit time blocked the user with no way to comply.
 *  3. `:disabled="… || (nsSup.duplicates.length && !nsSup.duplicateChecked)"`
 *     evaluated to `0` (a number) when there were no duplicates. Alpine removes a
 *     boolean attribute only for null/undefined/false, so 0 SET it — disabling
 *     Add to NetSuite for every supplier WITHOUT a flagged duplicate, and enabling
 *     it only when one WAS flagged and confirmed. Exactly inverted.
 */

'use strict';

const {
  readTemplate, extract, fakeResponse, quietConsole, assert, eq, tick, check, finish,
} = require('./harness');

const BASE = 'templates/base.html';
const QUOTATION = 'templates/partials/_rfq_quotation_final.html';

function baseNsSup(over) {
  return Object.assign({
    supplier_id: '05be7e07-4edb-4f39-9215-74d841914f16',
    name: 'LiftRite Hire & Sales',
    country: 'AU', addr1: '75 Naxos Way', addr2: '', city: 'Keysborough',
    state: 'VIC', zip: '3173', phone: '+61 3 8770 6555', contact: 'Cathy',
    email: 'sales@example.com', url: 'https://example.com',
    terms_id: '', currency: 'AUD',
    duplicates: [], duplicateChecked: false, submitting: false, error: '',
  }, over || {});
}

function build(src, calls, fetchImpl) {
  const windowStub = {
    HSOverlay: { close: (sel) => calls.push('close' + sel) },
    eaToast: () => {},
  };
  const code = extract(src, 'async _jsonFetch(url, options) {')
    + ','
    + extract(src, 'submitNsSupplier() {');
  const factory = new Function(
    'window', 'fetch', 'calls', 'console',
    'var c = { rfqId: "RFQ-2026-1054", nsSup: {}, syncMsg: "",'
      + ' refreshQuotationTab: function () { calls.push("refresh"); }, '
      + code + '}; return c;'
  );
  return factory(windowStub, fetchImpl, calls, quietConsole);
}

/** Submit once with the given nsSup and response; returns the observed outcome. */
async function submit(src, nsSup, resp) {
  const calls = [];
  let posted = 0;
  const c = build(src, calls, () => { posted += 1; return Promise.resolve(resp); });
  c.nsSup = baseNsSup(nsSup);
  c.submitNsSupplier();
  await tick(30);
  return {
    error: c.nsSup.error,
    duplicates: c.nsSup.duplicates,
    duplicateChecked: c.nsSup.duplicateChecked,
    submitting: c.nsSup.submitting,
    syncMsg: c.syncMsg,
    closed: calls.some((x) => String(x).indexOf('close') === 0),
    refreshed: calls.indexOf('refresh') !== -1,
    posted,
  };
}

/** The submit button's :disabled expression, straight out of the partial. */
function submitDisabledExpr() {
  const src = readTemplate(QUOTATION);
  const i = src.indexOf('type="submit"');
  assert(i !== -1, 'submit button not found in ' + QUOTATION);
  const m = /:disabled="([^"]*)"/.exec(src.slice(i, i + 400));
  assert(m, 'the submit button has no :disabled binding');
  return m[1].trim();
}

(async () => {
  const src = readTemplate(BASE);

  await check('200 success closes the modal and reports it', async () => {
    const r = await submit(src, {}, fakeResponse({
      status: 200, body: { ok: true, netsuite_id: '123' },
    }));
    assert(r.closed, 'the modal should close on success');
    assert(r.syncMsg.indexOf('NetSuite') !== -1, 'a success message should be set');
    assert(r.refreshed, 'the quotation tab should refresh');
    eq(r.error, '', 'no error on success');
  });

  await check('expired session (redirect to a 200 HTML login page) is NOT success', async () => {
    const r = await submit(src, {}, fakeResponse({
      status: 200, ok: true, redirected: true, ctype: 'text/html',
    }));
    assert(!r.closed,
      'a redirect to /login must not be treated as success — the modal used to '
      + 'close claiming the supplier had been added');
    assert(r.error.indexOf('session') !== -1,
      'it should say the session expired, got: ' + JSON.stringify(r.error));
    assert(!r.syncMsg, 'no success message');
  });

  await check('non-JSON failure body reports an error and keeps the modal open', async () => {
    const r = await submit(src, {}, fakeResponse({ status: 500, ctype: 'text/plain' }));
    assert(r.error.length > 0, 'an error message is required');
    assert(!r.closed, 'the modal must stay open so the user can retry');
  });

  await check('400 from the duplicate guard surfaces the confirm step', async () => {
    const r = await submit(src, {}, fakeResponse({
      status: 400,
      body: {
        error: 'Possible duplicate supplier(s): Acme Ltd. Confirm before continuing.',
        duplicates: [{ name: 'Acme Ltd', reasons: ['same address'] }],
      },
    }));
    eq(r.duplicates.length, 1,
      'the server-flagged duplicates must be adopted so the confirm box renders');
    eq(r.duplicateChecked, false, 'the confirm starts unticked');
    assert(r.error.indexOf('duplicate') !== -1, 'the reason should be stated');
  });

  await check('an unconfirmed duplicate is refused locally, with no request sent', async () => {
    const r = await submit(src, {
      duplicates: [{ name: 'Acme Ltd', reasons: [] }], duplicateChecked: false,
    }, fakeResponse({ status: 200, body: { ok: true } }));
    eq(r.posted, 0, 'no request should be sent while the confirm is outstanding');
    assert(r.error.length > 0, 'the refusal must be explained, not silent');
    eq(r.submitting, false, 'the button must not be left in a submitting state');
  });

  await check('the submit button is disabled only by a real boolean', () => {
    const expr = submitDisabledExpr();
    const cases = [
      { nsSup: { submitting: false, duplicates: [], duplicateChecked: false }, want: false },
      { nsSup: { submitting: true, duplicates: [], duplicateChecked: false }, want: true },
      { nsSup: { submitting: false, duplicates: [{ name: 'X' }], duplicateChecked: false }, want: false },
      { nsSup: { submitting: false, duplicates: [{ name: 'X' }], duplicateChecked: true }, want: false },
    ];
    for (const c of cases) {
      const v = new Function('nsSup', 'return (' + expr + ');')(c.nsSup);
      assert(typeof v === 'boolean',
        'the expression must yield a real boolean, got ' + JSON.stringify(v)
        + ' (' + typeof v + ') — Alpine removes a boolean attribute only for '
        + 'null/undefined/false, so a number like 0 SETS `disabled`');
      assert(v === c.want,
        'with ' + JSON.stringify(c.nsSup) + ' disabled should be ' + c.want
        + ', got ' + v);
    }
  });

  await check('no flagged duplicate means Add is enabled', () => {
    // The exact production failure: nothing flagged, nothing submitting, but the
    // button was disabled because duplicates.length && … evaluated to 0.
    const expr = submitDisabledExpr();
    const v = new Function('nsSup', 'return (' + expr + ');')({
      submitting: false, duplicates: [], duplicateChecked: false,
    });
    eq(v, false, 'a supplier with no flagged duplicate must be addable');
  });

  finish();
})();
