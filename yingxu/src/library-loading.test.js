import test from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { Module } from 'node:module';
import { build } from 'esbuild';
import React from 'react';

// Execute the component's real effects and read controller without a DOM or a
// network. The page cache retains hooks while effect lifetimes can reconnect.
const directory = fileURLToPath(new URL('.', import.meta.url));
const stubs = {
  './i18n/I18nProvider.jsx': `export const useI18n=()=>({t:key=>key});`,
  './UniversalTrash.jsx': `export const useTrash=()=>null;export const TrashMenu=()=>null;`,
  './ui/Dialog.jsx': `export const Dialog=()=>null;`,
  './media.js': `export const uploadFile=()=>null;`,
  './media-operations.js': `export const uploadLibraryFiles=()=>[];`,
  './storyboard-actions.js': `export const applyStoryboardAction=()=>false;`,
  './v2/LibraryParts.jsx': `const C=()=>null;export const FloatingPanel=C,InlineName=C,ReferencePicker=C,TagEditor=C;export const displayName=(r)=>r.title;export const managedRow=()=>false;`,
  './cloud-controller.js': `export const cloudController={};`,
  './StudioHeader.jsx': `export default ()=>null;`,
  './v2/LibraryView.jsx': `export default ()=>null;`,
  './store.js': `const state={workspace:{mode:'local',dirty:false},project:{id:'local'}};export const store={subscribe:()=>()=>{},getState:()=>state};export const ancestors=()=>[];export const validateProject=()=>[];`,
};
const compiled = await build({
  stdin: { contents: `export {default as Library} from './Library.jsx';`, resolveDir: directory },
  bundle: true,
  write: false,
  format: 'cjs',
  platform: 'node',
  external: ['react'],
  plugins: [
    {
      name: 'library-render-boundaries',
      setup(builder) {
        builder.onResolve({ filter: /.*/ }, (args) =>
          Object.hasOwn(stubs, args.path)
            ? { path: args.path, namespace: 'library-stub' }
            : undefined,
        );
        builder.onLoad({ filter: /.*/, namespace: 'library-stub' }, (args) => ({
          contents: stubs[args.path],
          loader: 'js',
        }));
      },
    },
  ],
});
const same = (left, right) =>
  Array.isArray(left) &&
  left.length === right?.length &&
  left.every((value, index) => Object.is(value, right[index]));
function mountedLibrary() {
  const hooks = [],
    events = new Map(),
    calls = [],
    waiting = [];
  let cursor = 0,
    dirty = false,
    props = { account: null, active: true },
    output;
  const useState = (initial) => {
    const index = cursor++;
    if (!hooks[index])
      hooks[index] = { kind: 'state', value: typeof initial === 'function' ? initial() : initial };
    return [
      hooks[index].value,
      (value) => {
        const next = typeof value === 'function' ? value(hooks[index].value) : value;
        if (!Object.is(next, hooks[index].value)) {
          hooks[index].value = next;
          dirty = true;
        }
      },
    ];
  };
  const fakeReact = {
    ...React,
    useState,
    useRef: (initial) => {
      const index = cursor++;
      return (hooks[index] ??= { kind: 'ref', current: initial });
    },
    useMemo: (create, deps) => {
      const index = cursor++,
        old = hooks[index];
      if (!old || !same(old.deps, deps)) hooks[index] = { kind: 'memo', value: create(), deps };
      return hooks[index].value;
    },
    useEffect: (create, deps) => {
      const index = cursor++,
        old = hooks[index];
      if (!old || !same(old.deps, deps)) {
        hooks[index] = { kind: 'effect', create, deps, cleanup: old?.cleanup };
        waiting.push(index);
      }
    },
    useSyncExternalStore: (subscribe, snapshot) => snapshot(),
    // Effect events always call the latest render's function.
    useEffectEvent: (fn) => {
      const index = cursor++;
      const slot = (hooks[index] ??= { kind: 'event', fn });
      slot.fn = fn;
      return (...args) => slot.fn(...args);
    },
  };
  const filename = directory + 'library-loading-test.cjs',
    mod = new Module(filename),
    nativeRequire = mod.require.bind(mod);
  mod.filename = filename;
  mod.paths = Module._nodeModulePaths(directory);
  mod.require = (name) => (name === 'react' ? fakeReact : nativeRequire(name));
  mod._compile(compiled.outputFiles[0].text, filename);
  const previous = {
    fetch: globalThis.fetch,
    window: globalThis.window,
    document: globalThis.document,
    localStorage: globalThis.localStorage,
  };
  const saved = new Map([['inkseq-library-command-v1:supervan', 'original pending command bytes']]);
  globalThis.localStorage = {
    getItem: (key) => saved.get(key) ?? null,
    setItem: () => assert.fail('read lifecycle must not rewrite pending commands'),
    removeItem: () => assert.fail('read lifecycle must not clear pending commands'),
  };
  globalThis.document = { title: '' };
  globalThis.window = {
    addEventListener: (type, fn) => events.set(type, fn),
    removeEventListener: (type, fn) => {
      if (events.get(type) === fn) events.delete(type);
    },
  };
  globalThis.fetch = (path, options) =>
    new Promise((resolve, reject) =>
      calls.push({
        path,
        options,
        resolve: (value) =>
          resolve(
            new Response(JSON.stringify(value), {
              headers: {
                'Content-Type': 'application/json',
                'X-Authenticated-Account': options.headers['X-Expected-Account'],
              },
            }),
          ),
        reject,
      }),
    );
  function render(next = props) {
    props = next;
    cursor = 0;
    dirty = false;
    output = mod.exports.Library(props);
    while (waiting.length) {
      const effect = hooks[waiting.shift()];
      effect.cleanup?.();
      effect.cleanup = effect.create();
    }
    return output.props;
  }
  async function flush() {
    for (let round = 0; round < 16; round++) {
      await new Promise((resolve) => setImmediate(resolve));
      if (dirty) render();
    }
    return output.props;
  }
  return {
    calls,
    saved,
    render,
    flush,
    get view() {
      return output.props;
    },
    reconnect() {
      const effects = hooks.filter((hook) => hook.kind === 'effect');
      for (const effect of effects) effect.cleanup?.();
      for (const effect of effects) effect.cleanup = effect.create();
    },
    evictMemos() {
      for (const hook of hooks) if (hook.kind === 'memo') hook.deps = undefined;
    },
    close() {
      for (const hook of hooks) if (hook.kind === 'effect') hook.cleanup?.();
      for (const call of calls)
        call.resolve(call.path.startsWith('/v1/projects') ? { projects: [] } : page());
      for (const [key, value] of Object.entries(previous))
        if (value === undefined) delete globalThis[key];
        else globalThis[key] = value;
    },
  };
}
const page = (id) => ({
  items: id
    ? [
        {
          target: { project_id: 'story', kind: 'asset', id },
          title: id,
          tags: [],
          usage: [],
          project_version: 1,
          type: 'image',
        },
      ]
    : [],
  next_offset: null,
  storage_scope: 'project',
  usage_scope: 'authorized_projects',
  aggregate_read_only: true,
});
const listCalls = (app) => app.calls.filter((call) => call.path.startsWith('/v1/library?'));
const ownerHeader = (call) => call.options.headers['X-Expected-Account'];

test('resolved owner starts its library read independently of a slow project list', async () => {
  const app = mountedLibrary();
  try {
    app.render();
    assert.equal(app.calls.length, 0);
    app.render({ account: 'supervan', active: true });
    await app.flush();
    assert.equal(listCalls(app).length, 1);
    assert.equal(ownerHeader(listCalls(app)[0]), 'supervan');
    listCalls(app)[0].resolve(page('owned'));
    await app.flush();
    assert.equal(app.view.loading, false);
    assert.equal(app.view.rows[0].title, 'owned');
    assert.equal(
      app.saved.get('inkseq-library-command-v1:supervan'),
      'original pending command bytes',
    );
  } finally {
    app.close();
  }
});

test('effect reconnect restores the owner fence and completes a fresh list read', async () => {
  const app = mountedLibrary();
  try {
    app.render({ account: 'supervan', active: true });
    await app.flush();
    const first = listCalls(app)[0];
    app.reconnect();
    await app.flush();
    assert.equal(listCalls(app).length, 2);
    first.resolve(page('obsolete'));
    listCalls(app)[1].resolve(page('reconnected'));
    await app.flush();
    assert.equal(app.view.loading, false);
    assert.equal(app.view.rows[0].title, 'reconnected');
  } finally {
    app.close();
  }
});

test('a replaced memoized client cannot invalidate the list without starting its replacement', async () => {
  const app = mountedLibrary();
  try {
    app.render({ account: 'supervan', active: true });
    await app.flush();
    const first = listCalls(app)[0];
    app.evictMemos();
    app.render();
    await app.flush();
    assert.equal(listCalls(app).length, 2);
    first.resolve(page('retired-client'));
    listCalls(app)[1].resolve(page('current-client'));
    await app.flush();
    assert.equal(app.view.loading, false);
    assert.equal(app.view.rows[0].title, 'current-client');
  } finally {
    app.close();
  }
});

test('a cached hidden page resumes with fresh reads and ignores late hidden results', async () => {
  const app = mountedLibrary();
  try {
    app.render({ account: 'supervan', active: true });
    await app.flush();
    const first = listCalls(app)[0];
    app.render({ account: 'supervan', active: false });
    await app.flush();
    assert.equal(app.view.loading, false);
    first.resolve(page('late-hidden'));
    await app.flush();
    assert.equal(app.view.rows.length, 0);
    app.render({ account: 'supervan', active: true });
    await app.flush();
    assert.equal(listCalls(app).length, 2);
    listCalls(app)[1].resolve(page('returned'));
    await app.flush();
    assert.equal(app.view.loading, false);
    assert.equal(app.view.rows[0].title, 'returned');
  } finally {
    app.close();
  }
});

test('late owner reads are fenced and a failed list exits loading before explicit refresh', async () => {
  const app = mountedLibrary();
  try {
    app.render({ account: 'superdan', active: true });
    await app.flush();
    const old = listCalls(app)[0];
    app.render({ account: 'supervan', active: true });
    await app.flush();
    assert.equal(ownerHeader(listCalls(app)[1]), 'supervan');
    old.resolve(page('foreign'));
    listCalls(app)[1].reject(Error('offline'));
    await app.flush();
    assert.equal(app.view.rows.length, 0);
    assert.equal(app.view.loading, false);
    app.view.onRefresh();
    await app.flush();
    listCalls(app)[2].resolve(page());
    await app.flush();
    assert.equal(app.view.loading, false);
    assert.equal(app.view.rows.length, 0);
    assert.equal(
      app.saved.get('inkseq-library-command-v1:supervan'),
      'original pending command bytes',
    );
  } finally {
    app.close();
  }
});
