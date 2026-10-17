import test from 'node:test';
import assert from 'node:assert/strict';
import {fileURLToPath} from 'node:url';
import {Module} from 'node:module';
import {build} from 'esbuild';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
const directory=fileURLToPath(new URL('.',import.meta.url)),filename=directory+'library-ui-test.cjs';
const compiled=await build({stdin:{contents:`import React from 'react';import Library,{LibraryMedia} from './Library.jsx';import StoryCoach from './StoryCoach.jsx';import CreationJourney from './CreationJourney.jsx';import {CloudProjects} from './CloudStudio.jsx';import StoryNavigation from './StoryNavigation.jsx';import {store} from './store.js';import {I18nProvider} from './i18n/I18nProvider.jsx';export {cloudController as testCloud} from './cloud-controller.js';export const Media=()=> <I18nProvider><LibraryMedia row={{type:'video',artifact_id:'original',title:'Video'}} api={{nativeContentURL:p=>p+'?expected_account=owner'}} detail/></I18nProvider>;export const LibraryPreview=()=> <I18nProvider><Library account={null}/></I18nProvider>;export const Coach=()=> <I18nProvider><StoryCoach project={{logline:'',journey:{brief:{}}}}/></I18nProvider>;export const Journey=()=> <I18nProvider><CreationJourney state={{project:{id:'story',title:'Story',logline:'',entities:[],links:[],journey:{stage:1,brief:{seconds:7200}}}}}/></I18nProvider>;export const CloudStoryList=()=> <I18nProvider><CloudProjects onClose={()=>{}}/></I18nProvider>;export const SidebarStories=()=> <I18nProvider><StoryNavigation state={store.getState()} onOpenCloud={()=>{}} onOpenLocal={()=>{}} onSelect={()=>{}}/></I18nProvider>;`,resolveDir:directory,loader:'jsx'},bundle:true,write:false,loader:{'.css':'empty'},format:'cjs',platform:'node',external:['react','react-dom']});
const mod=new Module(filename);mod.filename=filename;mod.paths=Module._nodeModulePaths(directory);mod._compile(compiled.outputFiles[0].text,filename);
test('uninitialized account library renders one sign-in state without inventing an authorized inventory',()=>{globalThis.localStorage={getItem:()=>null};try{const html=renderToStaticMarkup(React.createElement(mod.exports.LibraryPreview));assert.match(html,/Sign in to browse your library/);assert.match(html,/class="empty-state"/);assert.match(html,/aria-label="Sign in"/);assert.equal((html.match(/nav-ico/g)||[]).length,5);assert.doesNotMatch(html,/Import copy|Replace tags|libp-side|libp-head|library-source-tools|class="lib-search"/);}finally{delete globalThis.localStorage;}});
test('guided four-question copy is English by default and chapter target permits above legacy UI cap',()=>{const coach=renderToStaticMarkup(React.createElement(mod.exports.Coach));assert.match(coach,/Who does the story follow/);assert.match(coach,/What stands in their way/);assert.doesNotMatch(coach,/故事跟着谁|这次最想/);const html=renderToStaticMarkup(React.createElement(mod.exports.Journey));const target=html.match(/<input[^>]*value="7200"[^>]*>/)?.[0];assert.ok(target);assert.doesNotMatch(target,/max=|min="5"/);});

test('media detail uses original owner-fenced content URL with native controls and no autoplay',()=>{const html=renderToStaticMarkup(React.createElement(mod.exports.Media));assert.match(html,/artifacts\/original\/content\?expected_account=owner/);assert.match(html,/controls=/);assert.doesNotMatch(html,/autoPlay|autoplay/);});

test('legacy nullable story titles render in both cloud lists without changing stored summaries',()=>{
  const projects=Object.freeze([Object.freeze({id:'legacy-null',title:null,version:1}),Object.freeze({id:'legacy-missing',version:1}),Object.freeze({id:'named',title:'Named story',version:2})]);
  const before=structuredClone(projects),cloud=mod.exports.testCloud,originalGetState=cloud.getState,originalStorage=globalThis.localStorage;
  const snapshot={...originalGetState(),account:'owner',projects,projectsLoaded:true,auth:null};
  cloud.getState=()=>snapshot;globalThis.localStorage={getItem:()=>null};
  try{
    const cloudHTML=renderToStaticMarkup(React.createElement(mod.exports.CloudStoryList));
    assert.match(cloudHTML,/class="cloud-project-list"/);assert.match(cloudHTML,/Named story/);
    assert.equal((cloudHTML.match(/<b><\/b>/g)||[]).length,2);
    const sidebarHTML=renderToStaticMarkup(React.createElement(mod.exports.SidebarStories));
    assert.match(sidebarHTML,/class="story-nav-list"/);assert.match(sidebarHTML,/Named story/);
    assert.equal((sidebarHTML.match(/<b><\/b>/g)||[]).length,2);
    assert.deepEqual(projects,before);
  }finally{cloud.getState=originalGetState;if(originalStorage===undefined)delete globalThis.localStorage;else globalThis.localStorage=originalStorage;}
});
