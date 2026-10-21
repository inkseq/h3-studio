import test from 'node:test';
import assert from 'node:assert/strict';
import {
  checkedGenerationDefaults,
  generationDefaultsEditor,
} from './settings-generation-defaults.js';
import { checkedLibraryPage } from './library-commands.js';
import { build } from 'esbuild';
import { fileURLToPath } from 'node:url';
import { Module } from 'node:module';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

test('account-defaults link targets only the current owner story and an actual shot without opening or modifying it', () => {
  const state = {
    workspace: { mode: 'cloud', account: 'owner' },
    project: { id: 'story +1', entities: [{ id: 'shot/1', type: 'shot' }] },
  };
  const before = structuredClone(state),
    url = new URL(generationDefaultsEditor(state, 'owner'), 'https://local.invalid');
  assert.equal(url.searchParams.get('project'), 'story +1');
  assert.equal(url.searchParams.get('entity'), 'shot/1');
  assert.equal(url.searchParams.get('view'), 'workspace');
  assert.equal(url.searchParams.get('defaults'), 'account');
  assert.equal(generationDefaultsEditor(state, 'other'), null);
  assert.equal(generationDefaultsEditor({ ...state, workspace: { mode: 'local' } }, 'owner'), null);
  assert.equal(
    generationDefaultsEditor({ ...state, project: { id: 'empty', entities: [] } }, 'owner'),
    null,
  );
  assert.deepEqual(state, before);
  assert.equal(checkedGenerationDefaults({ version: 0, defaults: {} }).version, 0);
  for (const value of [
    { version: -1, defaults: {} },
    { version: 1, defaults: [] },
    { version: 1, defaults: { controls: [] } },
    { version: 1, defaults: { secret: 'not a setting' } },
  ])
    assert.throws(() => checkedGenerationDefaults(value));
});

const counts = {
  character: 2,
  location: 3,
  prop: 4,
  file: 137,
  image: 120,
  video: 12,
  audio: 5,
  inbox: 17,
};
test('library count contract rejects fabricated or partial totals and preserves exact server values across pages', () => {
  const page = {
    storage_scope: 'project',
    usage_scope: 'authorized_projects',
    aggregate_read_only: true,
    items: [],
    next_offset: null,
    counts,
    counts_scope: 'authorized_filtered_except_type',
  };
  assert.deepEqual(checkedLibraryPage(page).counts, counts);
  for (const change of [
    { counts: { ...counts, inbox: -1 } },
    { counts: { ...counts, image: 1.2 } },
    { counts: { image: 120 } },
    { counts_scope: 'all_accounts' },
  ])
    assert.throws(() => checkedLibraryPage({ ...page, ...change }));
  assert.equal(
    checkedLibraryPage({ ...page, counts: undefined, counts_scope: undefined }).counts,
    undefined,
    'an older server remains unknown, never zero',
  );
});

const directory = fileURLToPath(new URL('.', import.meta.url)),
  filename = directory + 'ui285-test.cjs';
const built = await build({
  stdin: {
    contents:
      "import React from 'react';import LibraryView from './v2/LibraryView.jsx';import {I18nProvider} from './i18n/I18nProvider.jsx';export const View=props=><I18nProvider><LibraryView {...props}/></I18nProvider>;",
    resolveDir: directory,
    loader: 'jsx',
  },
  bundle: true,
  write: false,
  loader: { '.css': 'empty' },
  format: 'cjs',
  platform: 'node',
  external: ['react', 'react-dom'],
});
const mod = new Module(filename);
mod.filename = filename;
mod.paths = Module._nodeModulePaths(directory);
mod._compile(built.outputFiles[0].text, filename);
test('library sidebar renders every authorized category total independent of the selected page size, with no separate Inbox', () => {
  const prior = globalThis.localStorage;
  globalThis.localStorage = { getItem: () => null };
  try {
    const html = renderToStaticMarkup(
      React.createElement(mod.exports.View, {
        account: 'owner',
        filters: { type: 'image' },
        counts,
        rows: [],
        selected: [],
        next: 100,
      }),
    );
    // "To sort" was dropped (owner, 2026-10-22): files are set as references from the card.
    for (const [key, number] of Object.entries(counts))
      if (key !== 'inbox') assert.match(html, new RegExp('>' + number + '</small>'));
    assert.doesNotMatch(html, />Inbox</);
    assert.doesNotMatch(html, /—|inboxUnavailable/);
  } finally {
    if (prior === undefined) delete globalThis.localStorage;
    else globalThis.localStorage = prior;
  }
});
