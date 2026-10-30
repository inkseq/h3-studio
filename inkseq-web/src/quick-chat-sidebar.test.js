import test from 'node:test';
import assert from 'node:assert/strict';
import { Module } from 'node:module';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import {
  conversationRowContext,
  currentConversationRow,
  trashConversationRows,
} from './quick-chat-sidebar.js';

const row = (id, patch = {}) => ({
  id,
  title: 'Creation ' + id,
  version: 3,
  trash_state: 'active',
  trashable: true,
  ...patch,
});
test('row trash captures the clicked conversation only and keeps canonical preview/confirm sequence', async () => {
  const state = {
      account: 'owner',
      historyView: 'active',
      session: row('current'),
      sessions: [row('current'), row('clicked')],
    },
    calls = [];
  const controller = {
    getState: () => state,
    selectHistory: (ids) => calls.push(['select', ids]),
    previewHistory: async (action) => {
      calls.push(['preview', action]);
      return { items: [{ id: 'clicked', expected_version: 3, eligible: true }] };
    },
    confirmHistory: async () => calls.push(['confirm']),
  };
  const target = conversationRowContext(state, state.sessions[1]);
  await trashConversationRows(controller, [target]);
  assert.deepEqual(calls, [['select', ['clicked']], ['preview', 'trash'], ['confirm']]);
  assert.equal(state.session.id, 'current');
});
test('blocked or changed row trash never substitutes a different row or silently drops a batch item', async () => {
  const state = {
      account: 'owner',
      historyView: 'active',
      sessions: [row('s'), row('busy', { trashable: false, blocked_reason: 'active_work' })],
    },
    calls = [];
  const controller = {
    getState: () => state,
    selectHistory: (ids) => calls.push(ids),
    previewHistory: async () => ({ items: [] }),
    confirmHistory: async () => assert.fail('must not confirm'),
  };
  await assert.rejects(
    trashConversationRows(
      controller,
      state.sessions.map((r) => conversationRowContext(state, r)),
    ),
    /stillWorking/,
  );
  assert.deepEqual(calls, []);
  const target = conversationRowContext(state, state.sessions[0]);
  state.sessions[0].version++;
  assert.throws(() => currentConversationRow(state, target), /changed/);
  const fresh = conversationRowContext(state, state.sessions[0]);
  controller.previewHistory = async () => {
    state.account = 'other';
    return { items: [{ eligible: true }] };
  };
  await assert.rejects(trashConversationRows(controller, [fresh]), /changed/);
});

const directory = fileURLToPath(new URL('.', import.meta.url)),
  filename = directory + 'quick-sidebar-test.cjs';
const built = await build({
  stdin: {
    contents: `import React from 'react';import CreationHistory from './CreationHistory.jsx';import {I18nProvider} from './i18n/I18nProvider.jsx';export const History=props=><I18nProvider><CreationHistory {...props}/></I18nProvider>;`,
    resolveDir: directory,
    loader: 'jsx',
  },
  bundle: true,
  write: false,
  format: 'cjs',
  platform: 'node',
  external: ['react', 'react-dom'],
  loader: { '.css': 'empty' },
});
const compiled = new Module(filename);
compiled.filename = filename;
compiled.paths = Module._nodeModulePaths(directory);
compiled._compile(built.outputFiles[0].text, filename);
test('active duplicate history view is retired while embedded Trash retains restore/purge controls', () => {
  const state = {
    account: 'owner',
    historyView: 'trash',
    historySelection: [],
    historyReceipt: null,
    historyPreview: null,
    trashSessions: [row('old', { trash_state: 'trashed' })],
    trashCursor: null,
  };
  const active = renderToStaticMarkup(
    React.createElement(compiled.exports.History, { state, controller: {}, busy: false }),
  );
  assert.doesNotMatch(active, /Creation history|qc-history-list|Hide test|Batch select/);
  const trash = renderToStaticMarkup(
    React.createElement(compiled.exports.History, {
      state,
      controller: {},
      busy: false,
      embeddedTrash: true,
    }),
  );
  assert.match(trash, /Creation old/);
  assert.match(trash, /Restore/);
  assert.match(trash, /forever/);
  assert.match(trash, /Empty trash/);
});
