import test from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { Module } from 'node:module';
import { build } from 'esbuild';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
const directory = fileURLToPath(new URL('.', import.meta.url)),
  filename = directory + 'library-ui-test.cjs';
const compiled = await build({
  stdin: {
    contents: `import React from 'react';import Library,{LibraryMedia} from './Library.jsx';import {CloudProjects} from './CloudStudio.jsx';import StoryNavigation from './StoryNavigation.jsx';import StoryOpenNotice from './StoryOpenNotice.jsx';import {store} from './store.js';import {I18nProvider} from './i18n/I18nProvider.jsx';export {cloudController as testCloud} from './cloud-controller.js';export const Media=()=> <I18nProvider><LibraryMedia row={{type:'video',artifact_id:'original',title:'Video'}} api={{nativeContentURL:p=>p+'?expected_account=owner'}} detail/></I18nProvider>;export const LibraryPreview=()=> <I18nProvider><Library account={null}/></I18nProvider>;export const OpenNotice=props=><I18nProvider><StoryOpenNotice {...props}/></I18nProvider>;export const CloudStoryList=()=> <I18nProvider><CloudProjects onClose={()=>{}}/></I18nProvider>;export const SidebarStories=()=> <I18nProvider><StoryNavigation state={store.getState()} onOpenCloud={()=>{}} onOpenLocal={()=>{}} onSelect={()=>{}}/></I18nProvider>;`,
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
mod._compile(compiled.outputFiles[0].text, filename);
test('uninitialized account library renders one sign-in state without inventing an authorized inventory', () => {
  globalThis.localStorage = { getItem: () => null };
  try {
    const html = renderToStaticMarkup(React.createElement(mod.exports.LibraryPreview));
    assert.match(html, /Sign in to browse your library/);
    assert.match(html, /class="empty-state"/);
    assert.match(html, /aria-label="Sign in"/);
    // Signed out: no GPUs entry (h3-studio#304); works, quick create, library, settings.
    assert.equal((html.match(/nav-ico/g) || []).length, 4);
    assert.doesNotMatch(
      html,
      /Import copy|Replace tags|libp-side|libp-head|library-source-tools|class="lib-search"/,
    );
  } finally {
    delete globalThis.localStorage;
  }
});
test('media detail uses original owner-fenced content URL with native controls and no autoplay', () => {
  const html = renderToStaticMarkup(React.createElement(mod.exports.Media));
  assert.match(html, /artifacts\/original\/content\?expected_account=owner/);
  assert.match(html, /controls=/);
  assert.doesNotMatch(html, /autoPlay|autoplay/);
});

test('legacy nullable story titles render in both cloud lists without changing stored summaries', () => {
  const projects = Object.freeze([
    Object.freeze({ id: 'legacy-null', title: null, version: 1 }),
    Object.freeze({ id: 'legacy-missing', version: 1 }),
    Object.freeze({ id: 'named', title: 'Named story', version: 2 }),
  ]);
  const before = structuredClone(projects),
    cloud = mod.exports.testCloud,
    originalGetState = cloud.getState,
    originalStorage = globalThis.localStorage;
  const snapshot = {
    ...originalGetState(),
    account: 'owner',
    projects,
    projectsLoaded: true,
    auth: null,
  };
  cloud.getState = () => snapshot;
  globalThis.localStorage = { getItem: () => null };
  try {
    const cloudHTML = renderToStaticMarkup(React.createElement(mod.exports.CloudStoryList));
    assert.match(cloudHTML, /class="cloud-project-list"/);
    assert.match(cloudHTML, /Named story/);
    assert.equal((cloudHTML.match(/Untitled story/g) || []).length, 2);
    assert.doesNotMatch(cloudHTML, /<b><\/b>/);
    const sidebarHTML = renderToStaticMarkup(React.createElement(mod.exports.SidebarStories));
    assert.match(sidebarHTML, /class="story-nav-list"/);
    assert.match(sidebarHTML, /Named story/);
    assert.match(sidebarHTML, /Untitled story/);
    assert.doesNotMatch(sidebarHTML, /<b><\/b>/);
    assert.deepEqual(projects, before);
  } finally {
    cloud.getState = originalGetState;
    if (originalStorage === undefined) delete globalThis.localStorage;
    else globalThis.localStorage = originalStorage;
  }
});

test('a project open failure renders one localized scoped alert and no account project dialog', () => {
  const cloud = mod.exports.testCloud,
    originalGetState = cloud.getState,
    originalStorage = globalThis.localStorage;
  cloud.getState = () => ({
    account: 'owner',
    busy: false,
    projectOpen: {
      projectId: 'removed',
      account: 'owner',
      status: 'error',
      key: 'storyOpen.removed',
    },
  });
  globalThis.localStorage = { getItem: () => null };
  try {
    const html = renderToStaticMarkup(React.createElement(mod.exports.OpenNotice));
    assert.equal((html.match(/role="alert"/g) || []).length, 1);
    assert.match(html, /content was removed/);
    assert.doesNotMatch(
      html,
      /项目版本|cloud-projects|Retry|Download original story|unsynced draft/,
    );
  } finally {
    cloud.getState = originalGetState;
    if (originalStorage === undefined) delete globalThis.localStorage;
    else globalThis.localStorage = originalStorage;
  }
});

test('inline dirty draft choice keeps remote open and download available, while a stale draft cannot restore', () => {
  const cloud = mod.exports.testCloud,
    originalGetState = cloud.getState,
    originalStorage = globalThis.localStorage;
  cloud.getState = () => ({
    account: 'owner',
    busy: false,
    projectOpen: { projectId: 'story', account: 'owner', status: 'draft' },
    draftOffer: {
      record: { id: 'story', version: 2, project: { title: 'The story' } },
      draft: { serverVersion: 1, project: { id: 'story', title: 'My draft' } },
    },
  });
  globalThis.localStorage = { getItem: () => null };
  try {
    const html = renderToStaticMarkup(React.createElement(mod.exports.OpenNotice));
    assert.match(html, /disabled=""[^>]*>Continue my unsynced draft/);
    assert.match(html, />Open the cloud version<\/button>/);
    assert.match(html, />Download unsynced draft<\/button>/);
    assert.doesNotMatch(html, /cloud-projects|Discard/);
  } finally {
    cloud.getState = originalGetState;
    if (originalStorage === undefined) delete globalThis.localStorage;
    else globalThis.localStorage = originalStorage;
  }
});
