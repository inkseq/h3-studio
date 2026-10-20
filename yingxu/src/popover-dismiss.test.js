import test from 'node:test';
import assert from 'node:assert/strict';
import { closeFloatingOutside, isFloating } from './popover-dismiss.js';

// Minimal DOM stand-ins: a <details> with a summary and one panel child.
function details(position, open = true) {
  const summary = { tagName: 'SUMMARY' };
  const panel = { tagName: 'DIV', position };
  const node = {
    open,
    children: [summary, panel],
    contains: (target) => target === node || target === summary || target === panel,
    removeAttribute(name) {
      if (name === 'open') node.open = false;
    },
    panel,
  };
  return node;
}
const style = (el) => ({ position: el.position || 'static' });
const root = (nodes) => ({ querySelectorAll: () => nodes.filter((n) => n.open) });

test('floating menus close on an outside click; inline disclosures stay open', () => {
  const menu = details('absolute');
  const fixed = details('fixed');
  const section = details('static');
  const closed = closeFloatingOutside(root([menu, fixed, section]), {}, style);
  assert.equal(closed, 2);
  assert.equal(menu.open, false);
  assert.equal(fixed.open, false);
  assert.equal(section.open, true);
});

test('a click inside the open menu keeps it open', () => {
  const menu = details('absolute');
  closeFloatingOutside(root([menu]), menu.panel, style);
  assert.equal(menu.open, true);
  assert.equal(isFloating(menu, style), true);
});
