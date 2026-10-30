import test from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { Module } from 'node:module';
import { build } from 'esbuild';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
const directory = fileURLToPath(new URL('.', import.meta.url)),
  filename = directory + 'workbench-test.cjs';
const built = await build({
  stdin: {
    contents: `import React from 'react';import TakeRecipe from './QuickChatTakeRecipe.jsx';export function RecipePreview({take,state}){return <I18nProvider><TakeRecipe take={take} state={state}/></I18nProvider>}import {I18nProvider} from './i18n/I18nProvider.jsx';import {StoryboardUIProvider} from './storyboard-ui-context.jsx';import {WorkbenchBulk,WorkbenchTakes,WorkbenchFork} from './StoryboardWorkbench.jsx';import {QueryClient,QueryClientProvider} from '@tanstack/react-query';export function Preview({adapter,shot}){return <QueryClientProvider client={new QueryClient()}><I18nProvider><StoryboardUIProvider adapter={adapter}><WorkbenchBulk ids={['shot']} onClear={()=>{}} onSelectAll={()=>{}}/><WorkbenchTakes shot={shot}/><WorkbenchFork ids={['shot']}/></StoryboardUIProvider></I18nProvider></QueryClientProvider>}`,
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
const compiled = new Module(filename);
compiled.filename = filename;
compiled.paths = Module._nodeModulePaths(directory);
compiled._compile(built.outputFiles[0].text, filename);
test('actual workbench renders its initial unread state without generation or destructive controls becoming available', () => {
  const shot = { id: 'shot', versions: [], entity: { data: {} } };
  const adapter = {
    state: {
      contextId: 'a:p:1',
      shots: [shot],
      profiles: [],
      assets: [],
      scenes: [],
      copiesMax: null,
      capabilities: { recipes: [], deployment_profiles: [] },
    },
    db: {},
    dispatch() {
      assert.fail('render must not write');
    },
  };
  const html = renderToStaticMarkup(
    React.createElement(compiled.exports.Preview, { adapter, shot }),
  );
  assert.match(
    html,
    /<button[^>]*class="primary" disabled="">(?:(?!<\/button>).)*Generate 1<\/button>/,
  );
  assert.match(html, /No takes yet/);
  assert.doesNotMatch(html, />Delete<|>Move to trash<|workbench\./);
});

test('people see their own choices; the model and engine settings stay admin-only', () => {
  const html = renderToStaticMarkup(
    React.createElement(compiled.exports.RecipePreview, {
      take: {
        seed: '123',
        recipe: {
          deployment_profile_id: 'retired-profile',
          model_id: 'OriginalModel',
          prompt: 'Original prompt',
          controls: { historical_strength: 0.73, duration: 5, steps: 5, sampler_name: 'euler' },
          inputs: {},
        },
      },
      state: { capabilities: { recipes: [], deployment_profiles: [] }, materials: [] },
    }),
  );
  // Trade secret (owner 2026-10-29): no model, profile or engine setting for people.
  assert.doesNotMatch(html, /OriginalModel|retired-profile|historical_strength|steps|euler/);
  assert.match(html, /duration/);
  assert.match(html, /Original prompt/);
});
