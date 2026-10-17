import test from 'node:test';
import assert from 'node:assert/strict';
import {fileURLToPath} from 'node:url';
import {Module} from 'node:module';
import {build} from 'esbuild';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
const directory=fileURLToPath(new URL('.',import.meta.url)),filename=directory+'workbench-test.cjs';
const built=await build({stdin:{contents:`import React from 'react';import TakeRecipe from './QuickChatTakeRecipe.jsx';export function RecipePreview({take,state}){return <I18nProvider><TakeRecipe take={take} state={state}/></I18nProvider>}import {I18nProvider} from './i18n/I18nProvider.jsx';import {StoryboardUIProvider} from './storyboard-ui-context.jsx';import {WorkbenchBulk,WorkbenchTakes,WorkbenchDefaults,WorkbenchFork} from './StoryboardWorkbench.jsx';export function Preview({adapter,shot}){return <I18nProvider><StoryboardUIProvider adapter={adapter}><WorkbenchBulk ids={['shot']} onClear={()=>{}} onSelectAll={()=>{}}/><WorkbenchTakes shot={shot}/><WorkbenchDefaults shotId="shot"/><WorkbenchFork ids={['shot']}/></StoryboardUIProvider></I18nProvider>}`,resolveDir:directory,loader:'jsx'},bundle:true,write:false,format:'cjs',platform:'node',external:['react','react-dom']});
const compiled=new Module(filename);compiled.filename=filename;compiled.paths=Module._nodeModulePaths(directory);compiled._compile(built.outputFiles[0].text,filename);
test('actual workbench renders its initial unread state without generation or destructive controls becoming available',()=>{
 const shot={id:'shot',versions:[],entity:{data:{}}};
 const adapter={state:{contextId:'a:p:1',shots:[shot],profiles:[],assets:[],scenes:[],copiesMax:null,capabilities:{recipes:[],deployment_profiles:[]}},db:{},dispatch(){assert.fail('render must not write');}};
 const html=renderToStaticMarkup(React.createElement(compiled.exports.Preview,{adapter,shot}));
 assert.match(html,/disabled="">Check estimate/);
 assert.match(html,/No takes yet/);assert.match(html,/Generation defaults/);
 assert.doesNotMatch(html,/>Delete<|>Move to trash<|workbench\./);
});

test('saved profile identity and controls absent from current schema remain visible',()=>{
 const html=renderToStaticMarkup(React.createElement(compiled.exports.RecipePreview,{take:{seed:'123',recipe:{deployment_profile_id:'retired-profile',model_id:'OriginalModel',prompt:'Original prompt',controls:{historical_strength:0.73},inputs:{}}},state:{capabilities:{recipes:[],deployment_profiles:[]},materials:[]}}));
 assert.match(html,/OriginalModel/);assert.match(html,/historical_strength/);assert.match(html,/0.73/);assert.match(html,/Original prompt/);
});
