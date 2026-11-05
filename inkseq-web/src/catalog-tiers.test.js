import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { catalogs } from './i18n/catalogs.js';
import { partitionKeys, reachable, referencedKeys } from './i18n/catalog-tiers.js';

const src = path.dirname(fileURLToPath(import.meta.url));
const keys = Object.keys(catalogs.en);

test('every word lands in exactly one tier', () => {
  const tiers = partitionKeys(src, keys);
  const all = [...tiers.shell, ...tiers.app, ...tiers.ops];
  assert.equal(all.length, keys.length);
  assert.deepEqual(new Set(all), new Set(keys));
});

test('the first script carries the public words and leaves operator words behind', () => {
  const tiers = partitionKeys(src, keys);
  assert.ok(tiers.shell.includes('notFound.title'));
  assert.ok(tiers.shell.some((key) => key.startsWith('landing.')));
  assert.ok(tiers.shell.length < keys.length / 2, 'the first set stays small');
  assert.ok(tiers.ops.some((key) => key.startsWith('legacyOp.')));
  assert.ok(!tiers.shell.some((key) => key.startsWith('legacyOp.')));
});

test('words people can download without being an admin name no vendor, seed or sampling', () => {
  const tiers = partitionKeys(src, keys);
  const banned =
    /dstack|RunPod|Lium|Targon|CloudRift|EveryFrame|OpenRouter|ComfyUI|WanGP|TaoMate|\bVast\b|\bseed\b|[Ss]ampling|sampler|种子|采样/;
  const found = [...tiers.shell, ...tiers.app].filter(
    (key) => banned.test(catalogs.en[key]) || banned.test(catalogs.zh[key]),
  );
  assert.deepEqual(found, []);
});

test('the first script reaches no workspace or admin page', () => {
  const first = reachable(path.join(src, 'main.jsx')).map((file) => path.basename(file));
  for (const name of [
    'SeriesApp.jsx',
    'StoryboardConnected.jsx',
    'Library.jsx',
    'UniversalTrash.jsx',
    'SpendingSettings.jsx',
    'AdminConsole.jsx',
    'P4OperatorConsole.jsx',
    'CloudStudio.jsx',
  ])
    assert.ok(!first.includes(name), name + ' is in the first script');
});

test('keys built at run time count through their fixed start', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'tiers-'));
  const file = path.join(dir, 'page.jsx');
  const quote = String.fromCharCode(96);
  fs.writeFileSync(
    file,
    [
      "t('a.exact');",
      "t('b.' + kind);",
      'const c = ' + quote + 'c.${kind}' + quote + ';',
      "t('d.copy' + n);",
    ].join('\n'),
  );
  const found = referencedKeys(
    [file],
    ['a.exact', 'a.other', 'b.one', 'b.two', 'c.one', 'd.copy1', 'e.never'],
  );
  assert.deepEqual([...found].sort(), ['a.exact', 'b.one', 'b.two', 'c.one', 'd.copy1']);
  fs.rmSync(dir, { recursive: true });
});
