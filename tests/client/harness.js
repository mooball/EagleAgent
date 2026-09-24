/**
 * Shared helpers for the client-side JS checks in tests/client/.
 *
 * Why these exist: the Python suite cannot see client behaviour, and every bug
 * they cover was invisible to BOTH `pytest` and `node --check` —
 *   - a function returning two different shapes (rowEl),
 *   - an EventSource attached before its run was registered (_sendAction),
 *   - a redirected non-JSON response read as success (_jsonFetch),
 *   - an Alpine binding evaluating to `0` instead of `false` (which SETS a
 *     boolean attribute rather than removing it).
 *
 * Each check extracts the REAL function source out of the template by
 * brace-matching, then drives it with stubs. Never copy the function into a
 * test: a copy drifts, and then the test passes while production breaks.
 *
 * Run one directly:  node tests/client/check_rowel.js
 * Run them all:      uv run pytest tests/test_client_js.py
 */

'use strict';

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..', '..');

function readTemplate(rel) {
  return fs.readFileSync(path.join(ROOT, rel), 'utf8');
}

/** Brace-match a declaration by its signature, e.g. 'function getRow(a, b) {' */
function extract(src, sig) {
  const start = src.indexOf(sig);
  if (start < 0) throw new Error('declaration not found in template: ' + sig);
  let depth = 0;
  for (let j = src.indexOf('{', start); j < src.length; j++) {
    if (src[j] === '{') depth++;
    else if (src[j] === '}') {
      depth--;
      if (depth === 0) return src.slice(start, j + 1);
    }
  }
  throw new Error('unbalanced braces for: ' + sig);
}

/**
 * Minimal DOM, enough for rowEl/getRow. appendChild rejects anything that is
 * not a node and throws the browser's exact TypeError, so a failure here is
 * indistinguishable from production.
 */
function makeDom() {
  const el = (tag) => ({
    tag,
    className: '',
    textContent: '',
    children: [],
    appendChild(n) {
      if (!n || typeof n !== 'object' || !n.tag) {
        throw new TypeError(
          "Failed to execute 'appendChild' on 'Node': parameter 1 is not of type 'Node'."
        );
      }
      this.children.push(n);
      return n;
    },
    remove() {
      this.removed = true;
    },
  });
  return { createElement: el };
}

/** Response-like stub for fetch replacements. */
function fakeResponse(opts) {
  const o = opts || {};
  const status = o.status === undefined ? 200 : o.status;
  const ctype = o.ctype === undefined ? 'application/json' : o.ctype;
  const isJson = ctype.indexOf('application/json') !== -1;
  return {
    status,
    ok: o.ok === undefined ? status < 400 : o.ok,
    redirected: !!o.redirected,
    headers: { get: (h) => (h.toLowerCase() === 'content-type' ? ctype : null) },
    json: () => (isJson
      ? Promise.resolve(o.body || {})
      : Promise.reject(new Error('not json'))),
  };
}

/** A console that stays quiet, so check output is only PASS/FAIL lines. */
const quietConsole = { log() {}, error() {}, warn() {}, info() {} };

const tick = (ms) => new Promise((r) => setTimeout(r, ms === undefined ? 20 : ms));

// --- tiny runner -----------------------------------------------------------

const results = [];

function assert(cond, what) {
  if (!cond) throw new Error(what);
}

function eq(actual, expected, what) {
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error(
      what + ': expected ' + JSON.stringify(expected) + ', got ' + JSON.stringify(actual)
    );
  }
}

async function check(name, fn) {
  try {
    await fn();
    results.push(true);
    console.log('  PASS  ' + name);
  } catch (e) {
    results.push(false);
    console.log('  FAIL  ' + name + '\n          ' + e.message);
  }
}

function finish() {
  const failed = results.filter((r) => !r).length;
  console.log(`\n${results.length - failed}/${results.length} checks passed`);
  process.exit(failed ? 1 : 0);
}

module.exports = {
  ROOT,
  readTemplate,
  extract,
  makeDom,
  fakeResponse,
  quietConsole,
  tick,
  assert,
  eq,
  check,
  finish,
};
