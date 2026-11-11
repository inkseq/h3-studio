import { test } from 'node:test';
import assert from 'node:assert/strict';
import { classUse, sheetClasses } from '../tools/css-class-use.mjs';

const read = (...sources) => {
  const use = classUse();
  for (const source of sources) use.script(source);
  return use;
};

test('a quote in a comment or a regular expression hides no later class', () => {
  // src/i18n/catalog-tiers.js holds this pattern; its odd backtick once hid every later file
  const use = read(
    "// don't read `this as a string\nconst KEY = /['\"`]([a-z]+)['\"`]/g;\nexport const key = KEY;",
    "export const Who = ({ on }) => <span className={'who' + (on ? ' on' : '')} />;",
    'export const Dock = () => <div className="dock dock-right" />;',
  );
  for (const name of ['who', 'on', 'dock', 'dock-right']) assert.ok(use.renders(name), name);
});

test('classes built at run time count by their fixed start', () => {
  const use = read(
    "const a = 'tile-' + kind, b = 'c' + n;",
    'const c = `plist ${open ? `p4-${row}` : ""} tseg`;',
  );
  for (const name of ['tile-wide', 'c20', 'p4-head', 'plist', 'tseg'])
    assert.ok(use.renders(name), name);
  assert.equal(use.renders('card'), false, "'c' + n builds numbered classes only");
  assert.equal(use.renders('tile'), false);
});

test('copy, comments and page text keep no class alive', () => {
  const use = read(
    "// 'ghost' was removed\nconst label = 'Edit as a new proposal';",
    "export const Note = () => <p title=\"Old banner\">it's the 'legacy' banner</p>;",
  );
  for (const name of ['ghost', 'proposal', 'legacy', 'banner'])
    assert.equal(use.renders(name), false, name);
});

test('index.html classes count', () => {
  const use = classUse();
  use.markup('<div id="root" class="boot dark"></div>');
  assert.ok(use.renders('boot') && use.renders('dark'));
  assert.equal(use.renders('root'), false);
});

test('a stylesheet lists its classes, not its numbers or file names', () => {
  const names = sheetClasses(
    '/* .gone */ .a > .b-c:hover { margin: 0.5em; background: url(x.png); } @media (max-width: 700.5px) { .d { top: 0 } }',
  );
  assert.deepEqual([...names].sort(), ['a', 'b-c', 'd']);
});
