/**
 * rowEl/getRow shape contract, for both chat templates.
 *
 * Regression: rowEl() returned a bare element for transient status rows but
 * `{div, bubble, actionsEl}` otherwise. getRow() then appended `undefined` —
 * and cached the malformed entry BEFORE the throw, so each transient row
 * produced a paired `appendChild` / `reading 'remove'` TypeError and no status
 * row ever rendered. Nothing in the Python suite could see this.
 */

'use strict';

const {
  readTemplate, extract, makeDom, assert, eq, check, finish,
} = require('./harness');

const TEMPLATES = [
  { file: 'templates/chat_ui/embed.html', container: 'messages', hasActions: true },
  { file: 'templates/chat_ui/thread.html', container: 'container', hasActions: false },
];

function build(src, containerName) {
  const dom = makeDom();
  const container = dom.createElement('div');
  const code = extract(src, 'function rowEl(id, author, transient) {')
    + '\n'
    + extract(src, 'function getRow(id, author, transient) {');
  const factory = new Function(
    'document', 'USER_EMAIL', 'rows', containerName,
    code
      + '\nreturn {rowEl: rowEl, getRow: getRow, rows: rows, container: '
      + containerName + '};'
  );
  return factory(dom, 'me@example.com', {}, container);
}

(async () => {
  for (const t of TEMPLATES) {
    const src = readTemplate(t.file);
    console.log(t.file);

    await check('transient row appends a real node, with no bubble', () => {
      const c = build(src, t.container);
      const el = c.getRow('t1', 'EagleAgent', true);
      assert(el.div && el.div.tag === 'div',
        'rowEl must return the {div, …} wrapper for a transient row, not a bare element');
      eq(c.container.children.length, 1, 'the transient row must be appended');
      assert(c.container.children[0] === el.div, 'the appended node must be rowEl.div');
      assert(!el.bubble, 'a transient row has no bubble');
      if (t.hasActions) assert(!el.actionsEl, 'a transient row has no action bar');
    });

    await check('removing a transient row does not throw', () => {
      const c = build(src, t.container);
      c.getRow('t1', 'EagleAgent', true);
      c.rows.t1.div.remove(); // the follow-up call that used to throw
      assert(c.rows.t1.div.removed === true, 'remove() should run on the row node');
    });

    await check('a failed append leaves no cached row', () => {
      const c = build(src, t.container);
      c.container.appendChild = () => { throw new TypeError('append failed'); };
      let threw = false;
      try {
        c.getRow('x', 'EagleAgent', false);
      } catch (e) {
        threw = true;
      }
      assert(threw, 'the append failure should propagate');
      assert(!c.rows.x,
        'a half-built row must not be cached — caching before the append is what '
        + "produced the follow-up \"reading 'remove'\" TypeError");
    });

    await check(
      'normal row keeps its bubble' + (t.hasActions ? ' and action bar' : ''),
      () => {
        const c = build(src, t.container);
        const el = c.getRow('n1', 'EagleAgent', false);
        assert(el.div && el.bubble, 'a normal row needs div + bubble');
        if (t.hasActions) assert(el.actionsEl, 'embed rows carry an action bar');
      }
    );
  }

  finish();
})();
