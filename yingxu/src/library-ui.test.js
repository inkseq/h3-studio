import test from 'node:test';
import assert from 'node:assert/strict';
import {fileURLToPath} from 'node:url';
import {Module} from 'node:module';
import {build} from 'esbuild';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
const directory=fileURLToPath(new URL('.',import.meta.url)),filename=directory+'library-ui-test.cjs';
const compiled=await build({stdin:{contents:`import React from 'react';import Library,{LibraryMedia} from './Library.jsx';import StoryCoach from './StoryCoach.jsx';import CreationJourney from './CreationJourney.jsx';import {I18nProvider} from './i18n/I18nProvider.jsx';export const Media=()=> <I18nProvider><LibraryMedia row={{type:'video',artifact_id:'original',title:'Video'}} api={{nativeContentURL:p=>p+'?expected_account=owner'}} detail/></I18nProvider>;export const LibraryPreview=()=> <I18nProvider><Library account={null}/></I18nProvider>;export const Coach=()=> <I18nProvider><StoryCoach project={{logline:'',journey:{brief:{}}}}/></I18nProvider>;export const Journey=()=> <I18nProvider><CreationJourney state={{project:{id:'story',title:'Story',logline:'',entities:[],links:[],journey:{stage:1,brief:{seconds:7200}}}}}/></I18nProvider>;`,resolveDir:directory,loader:'jsx'},bundle:true,write:false,loader:{'.css':'empty'},format:'cjs',platform:'node',external:['react','react-dom']});
const mod=new Module(filename);mod.filename=filename;mod.paths=Module._nodeModulePaths(directory);mod._compile(compiled.outputFiles[0].text,filename);
test('uninitialized account library renders sign-in without inventing an authorized inventory',()=>{globalThis.localStorage={getItem:()=>null};try{const html=renderToStaticMarkup(React.createElement(mod.exports.LibraryPreview));assert.match(html,/Sign in to browse your library/);assert.doesNotMatch(html,/Import copy|Replace tags/);}finally{delete globalThis.localStorage;}});
test('guided four-question copy is English by default and chapter target permits above legacy UI cap',()=>{const coach=renderToStaticMarkup(React.createElement(mod.exports.Coach));assert.match(coach,/Who does the story follow/);assert.match(coach,/What stands in their way/);assert.doesNotMatch(coach,/故事跟着谁|这次最想/);const html=renderToStaticMarkup(React.createElement(mod.exports.Journey));const target=html.match(/<input[^>]*value="7200"[^>]*>/)?.[0];assert.ok(target);assert.doesNotMatch(target,/max=|min="5"/);});

test('media detail uses original owner-fenced content URL with native controls and no autoplay',()=>{const html=renderToStaticMarkup(React.createElement(mod.exports.Media));assert.match(html,/artifacts\/original\/content\?expected_account=owner/);assert.match(html,/controls=/);assert.doesNotMatch(html,/autoPlay|autoplay/);});
