import test from 'node:test';
import assert from 'node:assert/strict';
import {createCloudClient} from './cloud-client.js';
import {createCloudController} from './cloud-controller.js';
import {createStore} from './store.js';
import {blankCloudProject,freestyleProject} from './cloud-model.js';
import {freestyleLinkState} from './agent-navigation.js';

async function fixture(){
  const data=new Map(),storage={getItem:k=>data.get(k)||null,setItem:(k,v)=>data.set(k,v)};
  const store=createStore({storage});store.newProject('Local');
  const project=blankCloudProject('Agent story'),record={id:project.id,version:1,project};
  const api={reset(){},setAccount(){},authConfig:async()=>({}),me:async()=>({username:'superdan'}),projects:async()=>({projects:[]}),capabilities:async()=>({}),project:async()=>structuredClone(record),projectMeta:async()=>({id:record.id,version:record.version,updated_at:100}),jobs:async()=>({jobs:[]})};
  const controller=createCloudController({store,client:api,storage,workspaceStorage:storage,mediaHandlers:()=>{}});
  await controller.initialize();await controller.open(project.id);
  return {controller,store,api,record};
}
test('agent cloud update is reported without overwriting a dirty local draft',async()=>{
  const h=await fixture();h.store.updateProject({logline:'My unsaved changes'});h.record.version=2;h.record.project.logline='Agent changes';
  await h.controller.checkRemoteVersion();assert.equal(h.controller.getState().remoteUpdate.version,2);assert.equal(h.store.getState().project.logline,'My unsaved changes');assert.equal(h.store.getState().workspace.serverVersion,1);
  assert.equal(await h.controller.open(h.record.id),false);assert.equal(h.controller.getState().draftOffer.record.version,2);assert.equal(h.store.getState().project.logline,'My unsaved changes');h.controller.destroy();
});
test('late version read after leaving the story cannot expose an old project notification',async()=>{
  const h=await fixture();let done;h.api.projectMeta=()=>new Promise(resolve=>{done=resolve;});const pending=h.controller.checkRemoteVersion();h.controller.leave();done({id:h.record.id,version:9});await pending;
  assert.equal(h.controller.getState().remoteUpdate,null);assert.equal(h.controller.getState().remoteUpdateError,'');h.controller.destroy();
});
test('opening the checked remote version clears the stale update notice',async()=>{
  const h=await fixture();h.record.version=2;h.record.project.title='Agent revised story';await h.controller.checkRemoteVersion();await h.controller.open(h.record.id);
  assert.equal(h.controller.getState().remoteUpdate,null);assert.equal(h.store.getState().project.title,'Agent revised story');assert.equal(h.store.getState().workspace.serverVersion,2);h.controller.destroy();
});
test('opening an Agent quick link selects its second shot and preserves the previous project dirty draft',async()=>{
  const h=await fixture(),previousId=h.record.id;
  h.store.updateProject({logline:'Keep my unsaved work'});
  const quick=freestyleProject('Agent quick'),first=quick.entities.find(e=>e.type==='shot');
  quick.entities.push({...structuredClone(first),id:'second-shot',title:'Agent target',order:1});
  h.record.id=quick.id;h.record.project=quick;
  assert.equal(freestyleLinkState(h.store.getState(),`?project=${quick.id}&entity=second-shot`,'superdan'),null);
  assert.equal(await h.controller.open(quick.id),true);
  assert.equal(freestyleLinkState(h.store.getState(),`?project=${quick.id}&entity=second-shot`,'superdan').target.shotId,'second-shot');
  assert.equal(h.store.getCloudDraft('superdan',previousId).project.logline,'Keep my unsaved work');
  h.store.updateEntity('second-shot',{data:{prompt:'Browser draft'}});
  h.record.version=2;h.record.project.entities.find(e=>e.id==='second-shot').data.prompt='Agent newer edit';
  await h.controller.checkRemoteVersion();assert.equal(h.controller.getState().remoteUpdate.version,2);
  assert.equal(await h.controller.open(quick.id),false);
  assert.equal(h.store.getState().project.entities.find(e=>e.id==='second-shot').data.prompt,'Browser draft');
  assert.equal(h.controller.getState().draftOffer.record.version,2);
  h.controller.destroy();
});
test('key creation is a single account-bound write and a late secret cannot return after account reset',async()=>{
  let resolve,calls=0,observed;const client=createCloudClient({fetcher:(path,options)=>{calls++;observed={path,options};return new Promise(r=>{resolve=r;});}});client.setAccount('superdan');
  const body={name:'fixture',all_projects:true,project_ids:[],scopes:['projects:read'],expires_in_days:30};const result=client.createApiKey(body),rejected=assert.rejects(result,/账户已改变/);
  client.setAccount('supervan');resolve(new Response(JSON.stringify({api_key:'synthetic-test-key-never-valid'}),{status:200,headers:{'Content-Type':'application/json'}}));await rejected;
  assert.equal(calls,1);assert.equal(observed.path,'/v1/api-keys');assert.equal(observed.options.headers['X-Expected-Account'],'superdan');assert.equal(observed.options.method,'POST');assert.deepEqual(JSON.parse(observed.options.body),body);client.reset();
});
