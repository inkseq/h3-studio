import test from 'node:test';
import assert from 'node:assert/strict';
import {createStore} from './store.js';
import {blankCloudProject} from './cloud-model.js';
import {createCloudController} from './cloud-controller.js';
import {createStoryRouteLoader,openedStoryHref,projectOpenFailure,readableProjectRecord,storyTitle} from './story-open.js';
import {translate} from './i18n/locale.js';

function memory(){const records=new Map();let blocked=false;return {records,getItem:key=>records.get(key)??null,setItem(key,value){if(blocked)throw Error('quota');records.set(key,value);},block(){blocked=true;}};}
function fixture(){
  const storage=memory(),store=createStore({storage}),records=new Map(),calls=[],opened=[];
  store.newProject('Original local story');
  const record=title=>{const project=blankCloudProject(title),value={id:project.id,version:1,project};records.set(value.id,value);return value;};
  const a=record('Cloud A'),b=record('Cloud B');
  const api={reset(){},authConfig:async()=>({authentication:'password'}),me:async()=>({username:'alice'}),login:async username=>({username}),logout:async()=>({}),projects:async()=>({projects:[...records.values()].map(({id,version,project})=>({id,version,title:project.title}))}),capabilities:async()=>({recipes:[],features:{}}),generationAvailability:async()=>null,jobs:async()=>({jobs:[]}),artifacts:async()=>({artifacts:[]}),project:async id=>{calls.push(['read',id]);return structuredClone(records.get(id));}};
  const cloud=createCloudController({store,storage,workspaceStorage:memory(),client:api,mediaHandlers:()=>{},onProjectOpened:detail=>opened.push(detail)});
  return {storage,store,cloud,api,records,a,b,calls,opened,async start(){await cloud.initialize();return this;}};
}

test('dirty draft opens inline and choosing cloud durably preserves the original across edits and reload',async()=>{
  const h=await fixture().start(),local=h.storage.getItem('yingxu-studio-v4');
  await h.cloud.open(h.a.id);h.store.updateProject({logline:'Unsynced original'});h.cloud.leave();
  assert.equal(await h.cloud.open(h.a.id),false);assert.equal(h.cloud.getState().projectOpen.status,'draft');
  assert.equal(h.store.getState().workspace.mode,'local');assert.equal(h.cloud.getState().error,'');
  assert.equal(await h.cloud.open(h.a.id,{keepDraft:true}),true);assert.equal(h.store.exportProject().logline,'');
  assert.equal(h.store.getPreservedCloudDrafts('alice',h.a.id)[0].project.logline,'Unsynced original');
  h.store.updateProject({logline:'A new cloud draft'});
  assert.equal(h.store.getPreservedCloudDrafts('alice',h.a.id)[0].project.logline,'Unsynced original');
  const reloaded=createStore({storage:h.storage});
  assert.equal(reloaded.getPreservedCloudDrafts('alice',h.a.id)[0].project.logline,'Unsynced original');
  assert.deepEqual(reloaded.getPreservedCloudDrafts('bob',h.a.id),[]);assert.equal(h.storage.getItem('yingxu-studio-v4'),local);
  assert.equal(h.calls.every(([method])=>method==='read'),true);h.cloud.destroy();
});

test('opening and reopening a job-bearing cloud story without edits stays clean, while a real edit still asks inline',async()=>{
  const h=await fixture().start(),job={id:'existing-job',client_ref:{project_id:h.a.id,shot_id:'historical-shot',shot_version:1},status:'succeeded',artifacts:[{id:'existing-video',kind:'video',mime:'video/mp4',filename:'original.mp4'}]};
  h.api.jobs=async id=>({jobs:id===h.a.id?[job]:[]});
  const original=structuredClone(h.records.get(h.a.id));
  assert.equal(await h.cloud.open(h.a.id),true);assert.equal(h.store.getState().workspace.dirty,false);
  assert.equal(h.store.getCloudDraft('alice',h.a.id).dirty,false);assert.equal(h.store.exportProject().jobs[0].id,job.id);
  assert.equal(h.store.exportProject().entities[0].data.cloudArtifactId,'existing-video');
  h.cloud.leave();assert.equal(await h.cloud.open(h.a.id),true);assert.equal(h.cloud.getState().draftOffer,null);assert.equal(h.store.getState().workspace.dirty,false);
  assert.deepEqual(h.records.get(h.a.id),original);assert.equal(h.store.exportProject().updatedAt,original.project.updatedAt);
  h.store.updateProject({logline:'An actual author edit'});job.status='failed';await h.cloud.refresh();assert.equal(h.store.getState().workspace.dirty,true);
  h.cloud.leave();assert.equal(await h.cloud.open(h.a.id),false);assert.equal(h.cloud.getState().projectOpen.status,'draft');
  assert.equal(h.store.getCloudDraft('alice',h.a.id).project.logline,'An actual author edit');h.cloud.destroy();
});

test('an automatic local-media observation keeps a clean story clean and never clears a genuine draft',async()=>{
  const h=await fixture().start(),asset={id:'local-asset',type:'image',title:'Original reference',description:'',parentId:null,order:0,version:1,status:'draft',data:{fileId:'local-original'}};
  h.records.get(h.a.id).project.entities.push(asset);
  await h.cloud.open(h.a.id);const initial=h.store.exportProject().updatedAt;
  h.store.updateEntity(asset.id,{data:{missingFile:true}},{history:false,observation:true});
  assert.equal(h.store.getState().workspace.dirty,false);assert.equal(h.store.exportProject().updatedAt,initial);
  h.store.updateProject({logline:'Keep my edit'});h.store.updateEntity(asset.id,{data:{missingFile:false}},{history:false,observation:true});
  assert.equal(h.store.getState().workspace.dirty,true);assert.equal(h.store.exportProject().logline,'Keep my edit');h.cloud.destroy();
});

test('continuing the matching unsynced draft never writes to the server; changed remote version refuses restore',async()=>{
  const h=await fixture().start();await h.cloud.open(h.a.id);h.store.updateProject({logline:'My draft'});h.cloud.leave();
  assert.equal(await h.cloud.open(h.a.id,{restoreDraft:true}),true);assert.equal(h.store.exportProject().logline,'My draft');
  h.cloud.leave();h.records.get(h.a.id).version=2;
  await assert.rejects(h.cloud.open(h.a.id,{restoreDraft:true}));
  assert.equal(h.cloud.getState().projectOpen.key,'storyOpen.versionConflict');assert.equal(h.cloud.getState().error,'');
  assert.equal(h.store.getState().workspace.mode,'local');assert.equal(h.store.getCloudDraft('alice',h.a.id).project.logline,'My draft');
  h.cloud.destroy();
});

test('archive quota failure refuses cloud replacement without changing the original draft or current story',async()=>{
  const h=await fixture().start();await h.cloud.open(h.a.id);h.store.updateProject({logline:'Only draft'});h.cloud.leave();
  const before=h.store.exportProject(),raw=[...h.storage.records.entries()];h.storage.block();
  await assert.rejects(h.cloud.open(h.a.id,{keepDraft:true}));assert.equal(h.cloud.getState().projectOpen.key,'storyOpen.preserveFailed');
  assert.deepEqual(h.store.exportProject(),before);assert.deepEqual([...h.storage.records.entries()],raw);h.cloud.destroy();
});

test('a corrupt preserved-draft archive is never overwritten while opening the cloud version',async()=>{
  const h=await fixture().start();await h.cloud.open(h.a.id);h.store.updateProject({logline:'Original draft'});h.cloud.leave();
  const key=`yingxu-cloud-kept-drafts-v1:alice:${encodeURIComponent(h.a.id)}`,raw='{"original":"truncated';h.storage.setItem(key,raw);
  await assert.rejects(h.cloud.open(h.a.id,{keepDraft:true}));assert.equal(h.storage.getItem(key),raw);
  assert.equal(h.store.getCloudDraft('alice',h.a.id).project.logline,'Original draft');assert.equal(h.store.getState().workspace.mode,'local');h.cloud.destroy();
});

test('legacy documents open read-only with original bytes and owner identity checked; no store conversion or global error',async()=>{
  const h=await fixture().start(),legacy={id:'old-project',version:7,project:{title:null,episodes:[{title:'Original episode'}],characters:[],locations:[]}};
  h.records.set(legacy.id,legacy);const before=h.store.exportProject(),raw=h.storage.getItem('yingxu-studio-v4');
  assert.equal(await h.cloud.open(legacy.id),false);assert.equal(h.cloud.getState().projectOpen.status,'legacy');
  assert.deepEqual(h.cloud.getState().legacyOffer.record,legacy);assert.equal(h.cloud.getState().error,'');
  assert.deepEqual(h.store.exportProject(),before);assert.equal(h.storage.getItem('yingxu-studio-v4'),raw);
  assert.throws(()=>readableProjectRecord({...legacy,id:'foreign'},legacy.id));
  assert.throws(()=>readableProjectRecord({...legacy,project:{id:'foreign'}},legacy.id));
  assert.equal(storyTitle(legacy.project,'Untitled story'),'Untitled story');
  assert.equal(storyTitle({title:'  '},'Untitled story'),'Untitled story');
  await h.cloud.open(h.b.id);assert.equal(h.cloud.getState().legacyOffer,null);assert.equal(h.cloud.getState().projectOpen,null);h.cloud.destroy();
});

test('content-deleted tombstones are removed stories, never a legacy view, migration or draft restore',async()=>{
  const h=await fixture().start(),removed={id:'chat-workspace-removed',version:3,project:{content_deleted:true}};
  h.records.set(removed.id,removed);const before=h.store.exportProject();
  await assert.rejects(h.cloud.open(removed.id));assert.equal(h.cloud.getState().projectOpen.key,'storyOpen.removed');
  assert.equal(h.cloud.getState().legacyOffer,null);assert.equal(h.cloud.getState().draftOffer,null);assert.equal(h.cloud.getState().error,'');
  assert.deepEqual(h.store.exportProject(),before);assert.match(translate('en','storyOpen.removed'),/content was removed/);
  assert.equal(h.calls.every(([method])=>method==='read'),true);h.cloud.destroy();
});

test('URL hydration waits for authentication, loads the named cloud story and handles back/forward',async()=>{
  const h=fixture(),loader=createStoryRouteLoader({cloud:h.cloud,store:h.store});
  assert.equal(loader.update({account:null,projectId:h.a.id}),null);assert.equal(h.calls.length,0);
  await h.start();await loader.update({account:'alice',projectId:h.a.id});assert.equal(h.store.exportProject().id,h.a.id);
  await loader.update({account:'alice',projectId:h.b.id});assert.equal(h.store.exportProject().id,h.b.id);
  await loader.update({account:'alice',projectId:h.a.id});assert.equal(h.store.exportProject().id,h.a.id);
  assert.deepEqual(h.calls.map(call=>call[1]),[h.a.id,h.b.id,h.a.id]);loader.destroy();h.cloud.destroy();
});

test('every accepted project open carries its checked identity to URL navigation, including restored drafts',async()=>{
  const h=await fixture().start();await h.cloud.open(h.a.id);h.store.updateProject({logline:'My draft'});h.cloud.leave();
  assert.equal(await h.cloud.open(h.a.id),false);assert.equal(h.opened.length,1);
  await h.cloud.open(h.a.id,{restoreDraft:true});assert.deepEqual(h.opened,[{account:'alice',projectId:h.a.id},{account:'alice',projectId:h.a.id}]);
  const live=h.store.getState(),detail=h.opened.at(-1);
  const expected='/?view=workspace&project='+h.a.id;
  assert.equal(openedStoryHref('https://staging.inkseq.com/',detail,live,'alice'),expected);
  assert.equal(openedStoryHref('https://staging.inkseq.com/?view=workspace',detail,live,'alice'),expected);
  assert.equal(openedStoryHref('https://staging.inkseq.com/?project=old&entity=old-shot&panel=activity',detail,live,'alice'),'/?project='+h.a.id+'&view=workspace');
  assert.equal(openedStoryHref('https://staging.inkseq.com/?project='+h.a.id+'&entity=shot',detail,live,'alice'),'/?project='+h.a.id+'&entity=shot&view=workspace');
  assert.equal(openedStoryHref('https://staging.inkseq.com/',detail,live,'bob'),null);
  assert.equal(openedStoryHref('https://staging.inkseq.com/operator',detail,live,'alice'),null);h.cloud.destroy();
});

test('navigation or account changes fence a late project response before it can replace local work',async()=>{
  const h=await fixture().start(),loader=createStoryRouteLoader({cloud:h.cloud,store:h.store}),before=h.store.exportProject();
  let finish;h.api.project=()=>new Promise(resolve=>finish=resolve);
  const pending=loader.update({account:'alice',projectId:h.a.id});
  loader.update({account:'alice',projectId:h.b.id,busy:true});finish(h.a);
  assert.equal(await pending,false);assert.deepEqual(h.store.exportProject(),before);assert.equal(h.cloud.getState().projectOpen,null);
  h.api.project=async()=>h.b;await loader.update({account:'alice',projectId:h.b.id});assert.equal(h.store.exportProject().id,h.b.id);
  h.cloud.leave();h.api.project=()=>new Promise(resolve=>finish=resolve);
  const old=h.cloud.open(h.a.id);h.api.me=async()=>({username:'bob'});await h.cloud.recheckSession();finish(h.a);
  await assert.rejects(old);assert.equal(h.cloud.getState().account,'bob');assert.equal(h.cloud.getState().projectOpen,null);
  assert.equal(h.store.getState().workspace.mode,'local');loader.destroy();h.cloud.destroy();
});

test('a failed story read has one scoped translated error, clears on navigation and does not retry itself',async()=>{
  const h=await fixture().start(),loader=createStoryRouteLoader({cloud:h.cloud,store:h.store});let reads=0;
  h.api.project=async()=>{reads++;throw Object.assign(Error('private upstream text must not leak'),{status:404});};
  await loader.update({account:'alice',projectId:h.a.id});assert.equal(h.cloud.getState().projectOpen.key,'storyOpen.notFound');
  assert.equal(h.cloud.getState().error,'');assert.equal(loader.update({account:'alice',projectId:h.a.id}),null);assert.equal(reads,1);
  const failure=projectOpenFailure(Error('项目版本不支持；请导入本工作室导出的 v4 项目。'));
  assert.equal(/[\u3400-\u9fff]/.test(translate('en',failure.key,{status:failure.statusCode})),false);
  loader.update({account:'alice',projectId:null,active:false});assert.equal(h.cloud.getState().projectOpen,null);loader.destroy();h.cloud.destroy();
});

test('failure to load job history does not turn an already opened cloud story into an open failure',async()=>{
  const h=await fixture().start();h.api.jobs=async()=>{throw Error('History unavailable');};
  assert.equal(await h.cloud.open(h.a.id),true);assert.equal(h.store.exportProject().id,h.a.id);assert.equal(h.cloud.getState().projectOpen,null);
  assert.equal(h.cloud.getState().activityError,'History unavailable');h.cloud.destroy();
});
