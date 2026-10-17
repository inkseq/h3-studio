import test from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import {renderToPipeableStream} from 'react-dom/server';
import {PassThrough} from 'node:stream';
import {once} from 'node:events';
import {deferredPanel,reloadAfterCheckpoint} from './deferred-panel.js';
import {createStore} from './store.js';

test('refresh requires durable draft storage, retains original data on quota/corruption and does not change undo history',()=>{
  const records=new Map();let full=false,reloads=0;
  const storage={getItem:key=>records.get(key)??null,setItem:(key,value)=>{if(full)throw Error('quota');records.set(key,value);}};
  const state=createStore({storage});state.updateProject({title:'需要保留的正文'});
  const before=state.exportProject(),history=state.getState().undoCount;
  assert.equal(reloadAfterCheckpoint(state,()=>reloads++),true);assert.equal(reloads,1);
  assert.deepEqual(state.exportProject(),before);assert.equal(state.getState().undoCount,history);
  full=true;state.updateProject({title:'尚未落盘的修改'});
  assert.equal(reloadAfterCheckpoint(state,()=>reloads++),false);assert.equal(reloads,1);
  assert.equal(state.getState().project.title,'尚未落盘的修改');assert.match(state.getState().saveState,/保存失败/);
  full=false;records.set('yingxu-studio-v4','{corrupt-original');
  const damaged=createStore({storage});assert.equal(reloadAfterCheckpoint(damaged,()=>reloads++),false);
  assert.equal(records.get('yingxu-studio-v4'),'{corrupt-original');assert.equal(reloads,1);
});

test('refresh checkpoints a cloud draft under its owner and leaves the anonymous project untouched',()=>{
  const records=new Map(),storage={getItem:key=>records.get(key)??null,setItem:(key,value)=>records.set(key,value)},state=createStore({storage});
  state.updateProject({title:'原本机作品'});const anonymous=records.get('yingxu-studio-v4'),cloud=structuredClone(state.exportProject());cloud.id='cloud-one';cloud.title='云原稿';
  state.enterCloudProject(cloud,{account:'supervan',version:22});state.updateProject({title:'私人未同步正文'});
  let reloaded=false;assert.equal(reloadAfterCheckpoint(state,()=>reloaded=true),true);assert.equal(reloaded,true);
  assert.equal(records.get('yingxu-studio-v4'),anonymous);assert.equal(state.getCloudDraft('supervan','cloud-one').project.title,'私人未同步正文');
  assert.equal(state.getCloudDraft('superdan','cloud-one'),null);assert.equal(state.getCloudDraft('supervan','cloud-one').serverVersion,22);
});

test('a quota failure across account switching keeps private drafts recoverable only by their owner and blocks refresh until saved',()=>{
  const records=new Map();let full=false,reloads=0;
  const storage={getItem:key=>records.get(key)??null,setItem:(key,value)=>{if(full)throw Error('quota');records.set(key,value);}},state=createStore({storage});
  state.updateProject({title:'原匿名稿'});const original=state.exportProject(),cloud=structuredClone(original);cloud.id='same-project';cloud.title='远端原稿';
  state.enterCloudProject(cloud,{account:'supervan',version:1});full=true;state.updateProject({title:'supervan仅在内存的私人内容'});state.leaveCloudProject();
  assert.equal(state.getState().project.title,'原匿名稿');assert.equal(state.getCloudDraft('supervan',cloud.id).project.title,'supervan仅在内存的私人内容');
  assert.equal(state.getCloudDraft('supervan',cloud.id).volatile,true);assert.equal(state.getCloudDraft('superdan',cloud.id),null);
  full=false;assert.equal(reloadAfterCheckpoint(state,()=>reloads++),false);assert.equal(reloads,0);assert.deepEqual(state.exportProject(),original);
  state.enterCloudProject(cloud,{account:'supervan',version:1,restoreDraft:true});assert.equal(state.getState().project.title,'supervan仅在内存的私人内容');
  assert.equal(state.getCloudDraft('supervan',cloud.id).volatile,undefined);assert.equal(reloadAfterCheckpoint(state,()=>reloads++),true);assert.equal(reloads,1);
  state.leaveCloudProject();assert.deepEqual(state.exportProject(),original);
});

test('a delayed optional panel keeps navigation visible, then renders the same draft without modifying it',async()=>{
  let resolve,calls=0;const pending=new Promise(r=>resolve=r),Panel=deferredPanel(()=>{calls++;return pending;},'字幕编辑器'),draft={title:'未同步的字幕稿'},original=structuredClone(draft);
  const output=new PassThrough();let html='';output.on('data',value=>html+=value.toString());const ended=once(output,'end');let shellReady;
  const shell=new Promise(r=>shellReady=r),stream=renderToPipeableStream(React.createElement('main',null,React.createElement('nav',null,'返回章节'),React.createElement(Panel,{draft})),{onShellReady(){stream.pipe(output);shellReady();}});
  try{await shell;assert.match(html,/返回章节/);assert.match(html,/正在打开字幕编辑器/);assert.match(html,/role="status"/);assert.match(html,/aria-busy="true"/);assert.equal(calls,1);
    resolve({default:({draft:value})=>React.createElement('textarea',{defaultValue:value.title})});await ended;assert.match(html,/未同步的字幕稿/);assert.deepEqual(draft,original);assert.equal(calls,1);
  }finally{stream.abort();}
});
