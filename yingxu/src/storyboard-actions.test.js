import test from 'node:test';
import assert from 'node:assert/strict';
import {applyStoryboardAction as edit} from './storyboard-actions.js';
import {validateProject, children} from './store.js';

const entity=(id,type,parentId=null,data={},order=0)=>({id,type,parentId,data,order,title:id,description:'',version:1,status:'ready'});
function fixture(){return {schemaVersion:4,id:'project',title:'Storyboard',logline:'',
  entities:[entity('chapter','chapter'),entity('scene-a','scene','chapter',{cast:[{characterId:'hero',lookId:'coat'}],locationId:'place'}),
    entity('scene-b','scene','chapter',{cast:[{characterId:'other',lookId:'main'}]},1),
    entity('shot-a','shot','scene-a',{seconds:5,prompt:'Run',selectedAssetId:'result',candidateIds:['result'],h3:{controls:{steps:50,resolution:'480P'}}}),
    entity('shot-b','shot','scene-a',{seconds:5,prompt:'Leap'},1),entity('shot-c','shot','scene-b',{seconds:5,prompt:'Wait'}),
    entity('hero','character',null,{look:'legacy field',looks:[{id:'coat',name:'Coat',description:'Red',gallery:{front:'image-one'},version:1}]}),
    entity('other','character',null,{looks:[{id:'main',name:'Main',description:'',gallery:{},version:1}]}),
    entity('place','location',null,{referenceAssetIds:['image-two'],time:'legacy night'}),entity('prop','prop',null,{imageIds:['image-two']}),
    entity('image-one','image',null,{fileId:'r2-one'}),entity('image-two','image',null,{fileId:'r2-two'}),
    entity('voice-one','audio',null,{fileId:'r2-voice',metadata:{source_duration:8}}),entity('voice-two','audio',null,{fileId:'r2-other',metadata:{source_duration:6}}),
    entity('result','video','shot-a',{fileId:'r2-result',cloudArtifactId:'artifact',sourceJobId:'paid-job'}),
    entity('audio-result','audio','shot-a',{fileId:'r2-output'}),entity('frame','image',null,{fileId:'r2-frame'})],
  links:[{id:'first',source:'image-one',target:'shot-a',role:'firstFrame'}],
  jobs:[{id:'paid-job',client_ref:{shot_id:'shot-a'},status:'succeeded',artifacts:[{id:'artifact',kind:'video'}]}],
  journey:{stage:4,brief:{aspect:'16:9'},soundTracks:{chapter:[{id:'track',assetId:'audio-result',shotId:'shot-a'}]}},
  layout:{positions:{'shot-a':{x:1,y:2}},viewport:{x:0,y:0,zoom:1}}};}
const get=(project,id)=>project.entities.find(value=>value.id===id);
const act=(project,action)=>edit(project,action,{chapterId:'chapter'});

test('first opt-in materializes inherited context; FL/REF switching keeps all inputs and preferences',()=>{
  const project=fixture(),job=structuredClone(project.jobs);
  assert.ok(act(project,{type:'update',id:'shot-a',patch:{mode:'REF'}}));
  const shot=get(project,'shot-a');assert.deepEqual(shot.data.cast,[{characterId:'hero',lookId:'coat'}]);
  assert.equal(shot.data.storyboard.firstFrameId,'image-one');
  assert.deepEqual(shot.data.storyboard.place,{locationId:'place',versionId:''});
  assert.equal(shot.data.h3.controls.steps,50);
  act(project,{type:'drop',shot:'shot-a',asset:'voice-one',slot:'refs'});
  act(project,{type:'update',id:'shot-a',patch:{start:'image-two',end:'image-one',mode:'FL'}});
  assert.deepEqual(shot.data.storyboard.extraRefs,[{entityId:'voice-one'}]);
  assert.equal(shot.data.storyboard.firstFrameId,'image-two');assert.equal(shot.data.storyboard.lastFrameId,'image-one');
  act(project,{type:'update',id:'shot-a',patch:{mode:'REF'}});
  assert.equal(shot.data.storyboard.firstFrameId,'image-two');assert.equal(shot.data.selectedAssetId,'result');
  assert.deepEqual(project.jobs,job);assert.equal(validateProject(project).ok,true);
});
test('uncast and unplace persist explicit empty overrides while preserving scene defaults',()=>{
  const project=fixture();act(project,{type:'uncast',shot:'shot-a',id:'hero'});act(project,{type:'unplace',shot:'shot-a'});
  assert.deepEqual(get(project,'shot-a').data.cast,[]);assert.equal(get(project,'shot-a').data.storyboard.place,null);
  assert.deepEqual(get(project,'scene-a').data.cast,[{characterId:'hero',lookId:'coat'}]);assert.equal(get(project,'scene-a').data.locationId,'place');
});
test('profile variants, main image order and voice ids save canonical fields without dropping legacy gallery',()=>{
  const project=fixture();act(project,{type:'fileImage',pid:'hero',vid:'coat',asset:'image-two'});
  act(project,{type:'coverImage',pid:'hero',vid:'coat',asset:'image-two'});
  const hero=get(project,'hero');assert.deepEqual(hero.data.looks[0].imageIds,['image-two','image-one']);
  assert.deepEqual(hero.data.looks[0].gallery,{front:'image-one'});assert.equal(hero.data.look,'legacy field');
  act(project,{type:'addVoice',pid:'hero',asset:'voice-one'});act(project,{type:'addVoice',pid:'hero',asset:'voice-two'});
  const first=hero.data.voices[0],second=hero.data.voices[1];assert.notEqual(first.id,first.assetId);
  act(project,{type:'voice',pid:'hero',asset:'voice-one',patch:{start:4}});
  act(project,{type:'profile',id:'hero',patch:{voice:'voice-two'}});
  act(project,{type:'variant',pid:'hero',vid:'coat',patch:{voice:'voice-one'}});
  assert.equal(hero.data.mainVoiceId,second.id);assert.equal(hero.data.looks[0].voiceId,first.id);assert.equal(first.clipStart,4);
  act(project,{type:'removeVoice',pid:'hero',asset:'voice-one'});
  assert.equal(hero.data.looks[0].voiceId,'');assert.equal(hero.data.mainVoiceId,second.id);
  assert.ok(get(project,'voice-one'));assert.equal(get(project,'shot-a').status,'review');
});
test('legacy location filing materializes a durable version, retaining legacy references and ambient clip',()=>{
  const project=fixture();act(project,{type:'fileImage',pid:'place',vid:'legacy',asset:'image-one'});
  const location=get(project,'place');assert.equal(location.data.defaultVersionId,'legacy');
  assert.deepEqual(location.data.versions[0].imageIds,['image-two','image-one']);assert.deepEqual(location.data.referenceAssetIds,['image-two']);
  act(project,{type:'profile',id:'place',patch:{ambient:'voice-one'}});location.data.ambient.clipStart=2;
  act(project,{type:'profile',id:'place',patch:{ambient:'voice-one'}});assert.equal(location.data.ambient.clipStart,2);
  act(project,{type:'drop',shot:'shot-b',profile:'place'});assert.equal(get(project,'shot-b').data.storyboard.place.versionId,'legacy');
});
test('cross-scene reordering keeps story hierarchy and materializes source bindings before the move',()=>{
  const project=fixture(),jobs=structuredClone(project.jobs);act(project,{type:'reorder',from:'shot-a',to:'shot-c'});
  const moved=get(project,'shot-a');assert.equal(moved.parentId,'scene-b');
  assert.deepEqual(moved.data.cast,[{characterId:'hero',lookId:'coat'}]);assert.equal(moved.data.storyboard.place.locationId,'place');
  assert.deepEqual(children(project,'scene-a').filter(value=>value.type==='shot').map(value=>value.id),['shot-b']);
  assert.deepEqual(children(project,'scene-b').filter(value=>value.type==='shot').map(value=>value.id),['shot-c','shot-a']);
  assert.deepEqual(project.jobs,jobs);assert.equal(get(project,'result').parentId,'shot-a');assert.equal(validateProject(project).ok,true);
});
test('removing a chosen shot preserves media, execution and sound history without cascading deletion',()=>{
  const project=fixture(),jobs=structuredClone(project.jobs),tracks=structuredClone(project.journey.soundTracks);
  assert.ok(act(project,{type:'remove',id:'shot-a'}));assert.equal(get(project,'shot-a'),undefined);
  assert.equal(get(project,'result').parentId,'scene-a');assert.equal(get(project,'audio-result').parentId,'scene-a');
  assert.ok(get(project,'hero'));assert.ok(get(project,'image-one'));assert.deepEqual(project.jobs,jobs);
  assert.deepEqual(project.journey.soundTracks,tracks);assert.equal(project.links.length,0);assert.equal(project.layout.positions['shot-a'],undefined);
  assert.equal(validateProject(project).ok,true);
});
test('profile and style changes invalidate dependent shot identities but preserve current chosen outputs',()=>{
  const project=fixture();act(project,{type:'profile',id:'hero',patch:{desc:'A new coat'}});
  assert.equal(get(project,'shot-a').version,2);assert.equal(get(project,'shot-b').version,2);assert.equal(get(project,'shot-c').version,1);
  act(project,{type:'project',patch:{style:'Film grain'}});
  assert.equal(get(project,'shot-c').version,2);assert.equal(get(project,'shot-a').data.selectedAssetId,'result');
  assert.equal(project.journey.stage,4);assert.deepEqual(project.journey.brief,{aspect:'16:9'});
});
test('new profiles and a new shot use existing entity kinds and never create jobs',()=>{
  const project=fixture();const profile=act(project,{type:'newProfile',ptype:'character',name:'New actor'});
  const added=act(project,{type:'add'});assert.equal(get(project,added.id).parentId,'scene-b');
  const variant=act(project,{type:'addVariant',pid:profile.id,name:'Second look'});
  act(project,{type:'profile',id:profile.id,patch:{def:variant.id}});
  act(project,{type:'drop',shot:added.id,profile:profile.id});
  assert.deepEqual(get(project,added.id).data.cast,[{characterId:profile.id,lookId:variant.id}]);
  assert.equal(project.jobs.length,1);assert.equal(validateProject(project).ok,true);
});
test('unfiling and dismissing inbox never remove reusable assets',()=>{
  const project=fixture();act(project,{type:'fileImage',pid:'hero',vid:'coat',asset:'image-two'});
  act(project,{type:'unfileImage',pid:'hero',vid:'coat',asset:'image-two'});
  assert.equal(get(project,'image-two').data.storyboardInbox,true);
  act(project,{type:'dismissInbox',asset:'image-two'});assert.equal(get(project,'image-two').data.storyboardInbox,false);
  assert.equal(get(project,'image-two').data.fileId,'r2-two');
});
test('invalid types, foreign references and out-of-range clips leave the input document unchanged',()=>{
  for(const action of [{type:'update',id:'shot-a',patch:{start:'voice-one'}},{type:'update',id:'shot-a',patch:{mode:'Both'}},
    {type:'update',id:'shot-a',patch:{duration:NaN}},{type:'drop',shot:'shot-a',asset:'foreign'},
    {type:'profile',id:'hero',patch:{voice:'voice-one'}},{type:'profile',id:'prop',patch:{ambient:'voice-one'}},
    {type:'variant',pid:'hero',vid:'coat',patch:{voice:'foreign'}},{type:'fileImage',pid:'hero',vid:'coat',asset:'voice-one'},
    {type:'coverImage',pid:'hero',vid:'coat',asset:'image-two'},{type:'voice',pid:'hero',asset:'voice-one',patch:{start:6}},
    {type:'project',patch:{style:'x',budget:9999}},{type:'generate',ids:['shot-a']}]){
      const project=fixture(),before=structuredClone(project);assert.equal(act(project,action),false,JSON.stringify(action));assert.deepEqual(project,before);
    }
  const project=fixture();act(project,{type:'addVoice',pid:'hero',asset:'voice-one'});const before=structuredClone(project);
  assert.equal(act(project,{type:'voice',pid:'hero',asset:'voice-one',patch:{start:6}}),false);assert.deepEqual(project,before);
});
