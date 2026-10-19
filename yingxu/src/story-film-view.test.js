import test from 'node:test';
import assert from 'node:assert/strict';
import { Module } from 'node:module';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { translate } from './i18n/locale.js';
import { filmCatalogs } from './i18n/filmCatalogs.js';

const directory = fileURLToPath(new URL('.', import.meta.url));
const compiled = await build({
  stdin: {
    contents: `export {FilmRunView,filmProblem} from './StoryFilmView.jsx';`,
    resolveDir: directory,
  },
  bundle: true,
  write: false,
  format: 'cjs',
  platform: 'node',
  external: ['react'],
});
const module = new Module(directory + 'film-view-test.cjs');
module.filename = directory + 'film-view-test.cjs';
module.paths = Module._nodeModulePaths(directory);
module._compile(compiled.outputFiles[0].text, module.filename);
const { FilmRunView, filmProblem } = module.exports;
const run = (patch = {}) => ({
  id: 'film-original',
  project_version: 7,
  snapshot_hash: 'a'.repeat(64),
  phase: 'preflight',
  generation: [{ plan_id: 'original' }],
  chapters: [{}],
  export: { artifact_id: null },
  options: { burn_subtitles: false },
  estimate: { quote_known: true, cost_microusd: 100000 },
  counts: {
    shots: 1,
    reused: 0,
    generate: 1,
    generated_completed: 0,
    chapters_completed: 0,
    export_completed: 0,
  },
  ...patch,
});
const render = (props = {}, locale = 'en') =>
  renderToStaticMarkup(
    React.createElement(FilmRunView, {
      state: { run: run(), pending: null, busy: false },
      t: (key, values) => translate(locale, key, values),
      dirty: false,
      currentVersion: 7,
      optionsMatch: true,
      confirmed: true,
      ...props,
    }),
  );
const button = (html, label) =>
  [...html.matchAll(/<button\b([^>]*)>([\s\S]*?)<\/button>/g)].find(
    (match) => match[2].replace(/<[^>]*>/g, '') === label,
  );

test('real film confirmation binds saved revision and option state; displayed amount uses existing money precision', () => {
  const ready = render();
  assert.match(ready, /Additional estimate: \$0\.10/);
  assert.ok(!button(ready, 'Confirm and make film')[1].includes('disabled'));
  for (const patch of [
    { dirty: true },
    { currentVersion: 8 },
    { optionsMatch: false },
    { confirmed: false },
    { state: { run: run(), pending: { kind: 'film-submit' }, busy: false } },
  ])
    assert.ok(button(render(patch), 'Confirm and make film')[1].includes('disabled'));
});

test('real progress shows original counts and download only for a completed confirmed export', () => {
  const waiting = render({
    state: {
      run: run({
        phase: 'chapters',
        options: { burn_subtitles: true },
        counts: {
          shots: 2,
          reused: 1,
          generate: 1,
          generated_completed: 1,
          chapters_completed: 0,
          export_completed: 0,
        },
      }),
      pending: null,
      busy: false,
    },
  });
  assert.match(waiting, /Assemble chapters and captions/);
  assert.match(waiting, /1 \/ 1/);
  assert.equal(button(waiting, 'Download film'), undefined);
  const complete = render({
    state: {
      run: run({
        phase: 'completed',
        export: { artifact_id: 'original-export' },
        counts: {
          shots: 1,
          reused: 0,
          generate: 1,
          generated_completed: 1,
          chapters_completed: 1,
          export_completed: 1,
        },
      }),
      pending: null,
      busy: false,
    },
  });
  assert.ok(button(complete, 'Download film'));
  assert.equal(button(complete, 'Cancel film'), undefined);
});

test('cancelling has no misleading resume/cancel action, and every supplied code has localized creator guidance', () => {
  const html = render({
    state: {
      run: run({ phase: 'cancel_requested', cancel_requested: true }),
      pending: null,
      busy: false,
    },
  });
  assert.match(html, /Stopping accepted work/);
  assert.equal(button(html, 'Cancel film'), undefined);
  assert.equal(button(html, 'Resume original run'), undefined);
  assert.deepEqual(Object.keys(filmCatalogs.en).sort(), Object.keys(filmCatalogs.zh).sort());
  for (const code of [
    'film_original_job_failed',
    'film_captions_require_confirmation',
    'film_selected_recipe_unknown',
    'film_selected_take_outdated',
    'film_actor_mismatch',
    'film.pendingCorrupt',
  ])
    for (const locale of ['en', 'zh']) assert.ok(translate(locale, filmProblem(code)).length > 10);
  assert.match(render({}, 'zh'), /新增费用估算/);
  const unauthorized = render({
    state: {
      run: run({ phase: 'authorization_required', accepted_at: 100 }),
      pending: null,
      busy: false,
    },
  });
  assert.ok(button(unauthorized, 'Resume original run'));
  assert.match(unauthorized, /original authorization is no longer valid/);
});
