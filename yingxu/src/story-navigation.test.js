import test from 'node:test';
import assert from 'node:assert/strict';
import {createStore} from './store.js';
import {blankCloudProject} from './cloud-model.js';
import {LOCAL_STORIES_KEY,archiveLocalStory,createLocalStory,openLocalStory,readLocalStoryLibrary,storyCoverFileId,storyEditedTimestamp,storyLibrarySummary} from './story-navigation.js';

function fixture(){const data=new Map();let fail=false;const storage={getItem:key=>data.get(key)||null,setItem(key,value){if(fail)throw Error('storage full');data.set(key,value);}},store=createStore({storage});store.newProject('故事 A');const cloud={getState:()=>({busy:false}),leave:()=>store.leaveCloudProject()};return {store,cloud,storage,data,setFail:value=>fail=value};}

test('sidebar local creation archives the old story and switches between independent stories',()=>{
  const h=fixture(),a=h.store.getState().project.id;h.store.addEntity('chapter',null,{title:'A 第一章'});
  createLocalStory('故事 B',h);const b=h.store.getState().project.id;assert.notEqual(a,b);assert.equal(h.store.getState().project.entities.length,0);
  h.store.addEntity('chapter',null,{title:'B 第一章'});openLocalStory(a,h);assert.equal(h.store.getState().project.entities[0].title,'A 第一章');
  openLocalStory(b,h);assert.equal(h.store.getState().project.entities[0].title,'B 第一章');assert.equal(readLocalStoryLibrary(h.storage).projects.length,2);
});

test('return from a dirty cloud story restores the original local story and keeps cloud draft private',()=>{
  const h=fixture(),local=h.store.exportProject();archiveLocalStory(h.store,h.storage);
  const remote=blankCloudProject('云故事');h.store.enterCloudProject(remote,{account:'supervan',version:1});h.store.updateProject({logline:'未同步的修改'});
  assert.deepEqual(h.store.getLocalProjectSummary(),{id:local.id,title:local.title});openLocalStory(local.id,h);
  assert.deepEqual(h.store.exportProject(),local);assert.equal(h.store.getCloudDraft('supervan',remote.id).project.logline,'未同步的修改');assert.equal(readLocalStoryLibrary(h.storage).projects.some(p=>p.id===remote.id),false);
});

test('failed local archive prevents replacing the current story',()=>{
  const h=fixture(),before=h.store.exportProject();h.setFail(true);
  assert.throws(()=>createLocalStory('不会覆盖',h),/storage full/);assert.deepEqual(h.store.exportProject(),before);
});

test('corrupt local story is rejected before leaving a cloud story',()=>{
  const h=fixture(),remote=blankCloudProject('云故事');h.store.enterCloudProject(remote,{account:'supervan',version:1});
  h.data.set(LOCAL_STORIES_KEY,JSON.stringify([{id:'broken',title:'损坏故事',entities:[]}]))
  assert.throws(()=>openLocalStory('broken',h));assert.equal(h.store.getState().workspace.mode,'cloud');assert.equal(h.store.getState().project.id,remote.id);
});


test('library metadata stays unknown for summaries and uses only the first chosen shot cover',()=>{
  assert.deepEqual(storyLibrarySummary({id:'p',title:'仅摘要'}),{id:'p',title:'仅摘要',updatedAt:null,shotCount:null,chosenCount:null,cover:null});
  const project={id:'p',title:'故事',entities:[
    {id:'chapter-late',type:'chapter',order:1},{id:'chapter-first',type:'chapter',order:0},
    {id:'scene-late',type:'scene',parentId:'chapter-late',order:0},{id:'scene-first',type:'scene',parentId:'chapter-first',order:0},
    {id:'shot-late',type:'shot',parentId:'scene-late',order:0,data:{selectedAssetId:'video-late'}},
    {id:'shot-first',type:'shot',parentId:'scene-first',order:0,data:{selectedAssetId:'video-first',storyboard:{firstFrameId:'unrelated-input'}}},
    {id:'video-late',type:'video',data:{}},{id:'video-first',type:'video',data:{cloudArtifactId:'first-artifact'}},
    {id:'unrelated-input',type:'image',data:{}},
    {id:'chosen-frame',type:'image',data:{fileId:'frame-file',sourceSelection:{kind:'artifact_frame',artifact_id:'first-artifact'}}},
  ]};
  const before=structuredClone(project),summary=storyLibrarySummary(project);
  assert.equal(summary.shotCount,2);assert.equal(summary.chosenCount,2);assert.equal(summary.cover.id,'chosen-frame');assert.deepEqual(project,before);
});

test('library cover file identity stays owned and malformed edit times do not break cards',()=>{
  const cover={type:'video',data:{fileId:'cloud_artifact_original',cloudArtifactId:'original'}};
  assert.deepEqual(storyCoverFileId(cover,{mode:'cloud'}),{fileId:'cloud_artifact_original',kind:'video',cloudPath:'/v1/artifacts/original/content'});
  assert.equal(storyCoverFileId(cover,{mode:'local'}),null);
  assert.equal(storyCoverFileId({...cover,data:{...cover.data,cloudArtifactId:'different'}},{mode:'cloud'}),null);
  assert.equal(storyCoverFileId({type:'image',data:{fileId:'../outside'}},{mode:'local'}),null);
  assert.equal(storyEditedTimestamp(1791960000),1791960000000);assert.equal(storyEditedTimestamp(9e99),null);assert.equal(storyEditedTimestamp('not a date'),null);
});
