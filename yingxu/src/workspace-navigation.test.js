import test from 'node:test';
import assert from 'node:assert/strict';
import {workspaceHref,navigateWorkspace} from './workspace-navigation.js';

test('view switching preserves only the bounded public project, entity and activity destination',()=>{
  const search='?project=quick%2F1&entity=second%26shot&panel=activity&api_key=do-not-copy&redirect=https://other.example';
  assert.equal(workspaceHref('/freestyle',search),'/freestyle?project=quick%2F1&entity=second%26shot&panel=activity');
  assert.equal(workspaceHref('/',search),'/?project=quick%2F1&entity=second%26shot&panel=activity');
  for(const invalid of ['?entity=shot','?project=a&project=b','?project=a&entity=%0Ab','?project=a&panel=activity&panel=delete'])assert.equal(workspaceHref('/freestyle',invalid),'/freestyle');
  assert.equal(workspaceHref('/','?project=a&panel=delete&token=secret'),'/?project=a');
  assert.throws(()=>workspaceHref('https://other.example',search),/Unknown workspace/);
});

test('normal switching changes history without reloading; modified clicks retain native behavior',()=>{
  const oldWindow=globalThis.window,oldPop=globalThis.PopStateEvent,calls=[];
  globalThis.window={location:{search:'?project=p&entity=second&token=hidden'},history:{pushState:(_state,_title,path)=>calls.push(path)},dispatchEvent:e=>calls.push(e.type),scrollTo:()=>{}};
  globalThis.PopStateEvent=class {constructor(type){this.type=type;}};
  try{
    const click={button:0,preventDefault:()=>calls.push('prevented')};
    navigateWorkspace({...click,ctrlKey:true},'/freestyle');assert.deepEqual(calls,[]);
    navigateWorkspace(click,'/freestyle');assert.deepEqual(calls,['prevented','/freestyle?project=p&entity=second','popstate']);
  }finally{if(oldWindow===undefined)delete globalThis.window;else globalThis.window=oldWindow;if(oldPop===undefined)delete globalThis.PopStateEvent;else globalThis.PopStateEvent=oldPop;}
});


test('legacy Freestyle routes into the same v2 project/entity/activity without copying a draft',async()=>{
 const {freestyleWorkspaceHref}=await import('./workspace-navigation.js');
 const state={workspace:{mode:'cloud',dirty:true},project:{id:'kept-project',entities:[{id:'local-edit'}]},selectedId:'local-edit'};
 const before=JSON.stringify(state);
 assert.equal(freestyleWorkspaceHref('?project=original&entity=shot&panel=activity&secret=discard',state),'/?project=original&entity=shot&panel=activity&view=workspace');
 assert.equal(freestyleWorkspaceHref('',state),'/?project=kept-project&entity=local-edit&view=workspace');
 assert.equal(freestyleWorkspaceHref('',{workspace:{mode:'local'}}),'/?view=workspace');
 assert.equal(freestyleWorkspaceHref('?project=one&project=two',state),null);
 assert.equal(JSON.stringify(state),before);
});
