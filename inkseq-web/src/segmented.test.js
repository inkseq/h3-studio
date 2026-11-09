import test from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { Module } from 'node:module';
import { build } from 'esbuild';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

const directory = fileURLToPath(new URL('.', import.meta.url)),
  filename = directory + 'segmented-test.cjs';
const compiled = await build({
  entryPoints: [directory + 'ui/Segmented.jsx'],
  bundle: true,
  write: false,
  format: 'cjs',
  platform: 'node',
  external: ['react', 'react-dom'],
});
const mod = new Module(filename);
mod.filename = filename;
mod.paths = Module._nodeModulePaths(directory);
mod._compile(compiled.outputFiles[0].text, filename);
const { Segmented, tabKeyTarget } = mod.exports;
const options = [{ value: 'a' }, { value: 'b', disabled: true }, { value: 'c' }, { value: 'd' }];

test('tab keys wrap, jump to the ends and skip disabled tabs', () => {
  assert.equal(tabKeyTarget(options, 0, 'ArrowRight'), 2);
  assert.equal(tabKeyTarget(options, 2, 'ArrowLeft'), 0);
  assert.equal(tabKeyTarget(options, 0, 'ArrowLeft'), 3);
  assert.equal(tabKeyTarget(options, 3, 'ArrowRight'), 0);
  assert.equal(tabKeyTarget(options, 2, 'Home'), 0);
  assert.equal(tabKeyTarget(options, 0, 'End'), 3);
  assert.equal(tabKeyTarget(options, 0, 'Enter'), -1);
  assert.equal(tabKeyTarget(options, 0, 'ArrowRight', true), -1);
});

test('tabs are one Tab stop on the selected tab; plain groups stay pressed buttons', () => {
  const tabs = renderToStaticMarkup(
    React.createElement(Segmented, {
      tabs: true,
      label: 'Steps',
      value: 'c',
      onChange() {},
      options: options.map((o) => ({ ...o, label: o.value })),
    }),
  );
  assert.match(tabs, /role="tablist"/);
  assert.equal((tabs.match(/tabindex="0"/g) || []).length, 1);
  assert.equal((tabs.match(/tabindex="-1"/g) || []).length, 3);
  assert.match(tabs, /aria-selected="true" tabindex="0"[^>]*>c</);
  const group = renderToStaticMarkup(
    React.createElement(Segmented, {
      value: 'a',
      onChange() {},
      options: [{ value: 'a', label: 'A' }],
    }),
  );
  assert.match(group, /aria-pressed="true"/);
  assert.doesNotMatch(group, /tabindex/);
});
