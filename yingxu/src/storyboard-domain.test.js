import test from 'node:test';
import assert from 'node:assert/strict';
import {chapterShots,storyboardView,expandStoryboard,storyboardDrop,storyboardProfile,patchStoryboardShot} from './storyboard-domain.js';
import {shotSnapshot,digest,mergeJobCandidate} from './cloud-model.js';
import {applyStoryboardAction} from './storyboard-actions.js';

const entity=(id,type,data={},parentId=null,order=0)=>({id,type,title:id,description:id+' description',parentId,order,version:1,status:'draft',data});
const image=id=>entity(id,'image',{fileId:'cloud_asset_'+id,cloudAssetId:id});
const audio=id=>entity(id,'audio',{fileId:'cloud_asset_'+id,cloudAssetId:id,metadata:{duration:8}});
const video=id=>entity(id,'video',{fileId:'cloud_artifact_'+id,cloudArtifactId:id,sourceJobId:'job-'+id,metadata:{duration:5}});
function fixture(){return {schemaVersion:4,id:'project',title:'Story',logline:'',journey:{storyboard:{style:'Ink and rain'}},entities:[entity('chapter','chapter'),entity('scene','scene',{},'chapter'),entity('shot','shot',{seconds:5,prompt:'Run across the roof',storyboard:{version:1,mode:'ref',quality:'draft',place:null,propIds:[],firstFrameId:'',lastFrameId:'',continuePrevious:false,extraRefs:[]}},'scene')],links:[],jobs:[]};}
function capabilities({images=5,videos=2,audios=3,total=8,constraints={}}={}){
  const support={enabled:true,configured:true,controls:{duration:{type:'integer',minimum:4,maximum:15,multipleOf:1},resolution:{default:'480P'},steps:{maximum:50}},limits:{max_images:images,max_videos:videos,max_audios:audios,max_total_files:total,max_total_audio_duration:12,max_total_video_duration:12},constraints};
  return {default_deployment_profile_id:'draft',deployment_profiles:[{id:'draft',label:'Pruned live profile',model_id:'MiniMax-H3-Pruned-Rank8-INT8',generation_support:{fl:structuredClone(support),ref:structuredClone(support)}},{id:'final',label:'Base live profile',model_id:'MiniMax-H3-Base-INT8',generation_support:{fl:structuredClone(support),ref:structuredClone(support)}}]};
}
const view=(project,cap=capabilities())=>storyboardView(project,{chapterId:'chapter',capabilities:cap});
const expanded=(project,cap)=>{const state=view(project,cap);return expandStoryboard(state.shots[0],state);};
const shotOf=project=>project.entities.find(e=>e.id==='shot');
const character=(id,photo,voice)=>entity(id,'character',{defaultLookId:'look',looks:[{id:'look',name:'Rain coat',description:'blue coat',gallery:{},imageIds:[photo]}],...(voice?{mainVoiceId:'voice',voices:[{id:'voice',assetId:voice,clipStart:1}]}:{})});

test('chapter card order follows scene and shot order without mutating the document',()=>{
  const p=fixture();p.entities.push(entity('later-scene','scene',{},'chapter',1),entity('later-shot','shot',{seconds:5},'later-scene'),entity('second-shot','shot',{seconds:5},'scene',1));const before=structuredClone(p);
  assert.deepEqual(chapterShots(p,'chapter').map(s=>s.id),['shot','second-shot','later-shot']);assert.deepEqual(p,before);
});
test('quality chooses its advertised model and explicit retired profiles never fall back',()=>{
  const cap=capabilities();assert.equal(storyboardProfile(cap,'draft','ref').id,'draft');assert.equal(storyboardProfile(cap,'final','fl').id,'final');assert.equal(storyboardProfile(cap,'draft','ref','retired'),null);
  assert.equal(storyboardProfile(cap,'final','ref','draft').id,'final');assert.equal(storyboardProfile(cap,'draft','ref','final').id,'draft');cap.deployment_profiles[0].generation_support.ref.enabled=false;
  assert.equal(storyboardProfile(cap,'draft','ref','draft'),null);assert.equal(storyboardProfile(cap,'draft','ref'),null);
});
test('mode changes preserve both inactive frame choices and reference clip metadata',()=>{
  const p=fixture();p.entities.push(image('first'),image('last'),audio('voice'));const s=shotOf(p);s.data.storyboard={...s.data.storyboard,firstFrameId:'first',lastFrameId:'last',extraRefs:[{entityId:'voice',clipStart:2}]};
  patchStoryboardShot(p,'shot',{mode:'FL'});const fl=expanded(p);assert.deepEqual(fl.items.filter(i=>i.sent).map(i=>i.a.id),['first','last']);assert.match(fl.notes.join(' '),/参考保留/);
  patchStoryboardShot(p,'shot',{mode:'REF'});assert.deepEqual(s.data.storyboard.extraRefs,[{entityId:'voice',clipStart:2}]);assert.equal(s.data.storyboard.firstFrameId,'first');assert.equal(s.data.storyboard.lastFrameId,'last');const ref=expanded(p);assert.equal(ref.items.some(i=>['first','last'].includes(i.a.id)&&i.sent),false);assert.equal(ref.items.find(i=>i.a.id==='voice').clipStart,2);
});
test('explicitly cleared storyboard frames do not revive old guide/canvas links',()=>{
  const p=fixture();p.entities.push(image('old-first'),image('old-last'));p.links.push({id:'first-link',source:'old-first',target:'shot',role:'firstFrame'},{id:'last-link',source:'old-last',target:'shot',role:'lastFrame'});
  const s=view(p).shots[0];assert.equal(s.start,'');assert.equal(s.end,'');
});
test('explicitly removed scene and inherited cast stay removed in the view',()=>{
  const p=fixture();p.entities.push(character('hero','portrait'),image('portrait'),entity('place','location',{referenceAssetIds:[]}));p.entities.find(e=>e.id==='scene').data={locationId:'place',cast:[{characterId:'hero',lookId:'look'}]};shotOf(p).data.cast=[];
  const s=view(p).shots[0];assert.equal(s.place,null);assert.deepEqual(s.cast,[]);
});
test('legacy frame inputs survive first storyboard edit of unrelated prompt',()=>{
  const p=fixture();delete shotOf(p).data.storyboard;p.entities.push(image('first'));p.links.push({id:'frame-link',source:'first',target:'shot',role:'firstFrame'});
  assert.ok(applyStoryboardAction(p,{type:'update',id:'shot',patch:{prompt:'A new action'}}));assert.equal(view(p).shots[0].start,'first');assert.equal(shotOf(p).data.storyboard.firstFrameId,'first');
});
test('REF duplicate references do not consume unique file or total capacity',()=>{
  const p=fixture();p.entities.push(image('shared'),image('extra'),character('a','shared'),character('b','shared'));shotOf(p).data.cast=[{characterId:'a',lookId:'look'},{characterId:'b',lookId:'look'}];shotOf(p).data.storyboard.extraRefs=[{entityId:'extra'}];
  const info=expanded(p,capabilities({images:2,total:2}));assert.deepEqual(info.items.filter(i=>i.sent).map(i=>i.a.id),['shared','extra']);assert.equal(info.items.find((i,n)=>n>0&&i.a.id==='shared').sent,false);
});
test('REF clipping never counts future items and preserves the first supported unique references',()=>{
  const p=fixture();p.entities.push(image('shared'),image('second'),image('third'),character('a','shared'),character('b','shared'));shotOf(p).data.cast=[{characterId:'a',lookId:'look'},{characterId:'b',lookId:'look'}];shotOf(p).data.storyboard.extraRefs=[{entityId:'second'},{entityId:'third'}];
  const info=expanded(p,capabilities({images:8,total:2}));assert.deepEqual(info.items.filter(i=>i.sent).map(i=>i.a.id),['shared','second']);assert.ok(info.items.find(i=>i.a.id==='third').reason);
});
test('REF priority matches the authoritative per-character photo and voice order',()=>{
  const p=fixture();p.entities.push(image('photo-a'),image('photo-b'),audio('voice-a'),character('a','photo-a','voice-a'),character('b','photo-b'));shotOf(p).data.cast=[{characterId:'a',lookId:'look'},{characterId:'b',lookId:'look'}];
  assert.deepEqual(expanded(p,capabilities({total:2})).items.filter(i=>i.sent).map(i=>i.a.id),['photo-a','voice-a']);
});
test('REF capability policy clamps advertised reference limits instead of legacy or history values',()=>{
  const p=fixture();p.entities.push(image('a'),image('b'));shotOf(p).data.storyboard.extraRefs=[{entityId:'a'},{entityId:'b'}];const cap=capabilities({images:9,total:12,constraints:{input_limits:{max_images:1},max_reference_files:1}});
  const info=expanded(p,cap);assert.deepEqual(info.items.filter(i=>i.sent).map(i=>i.a.id),['a']);assert.match(info.limitsLabel,/图片 1/);
});
test('unknown capabilities retain REF authoring references but never pretend they will be sent',()=>{
  const p=fixture();p.entities.push(image('a'));shotOf(p).data.storyboard.extraRefs=[{entityId:'a'}];const info=expanded(p,null);assert.equal(info.items.length,1);assert.equal(info.items[0].sent,false);assert.ok(info.items[0].reason);
});
test('a stale explicitly selected look never silently supplies another looks photo',()=>{
  const p=fixture();p.entities.push(character('hero','photo'),image('photo'));shotOf(p).data.cast=[{characterId:'hero',lookId:'deleted-look'}];const info=expanded(p);
  assert.equal(info.items.some(i=>i.a.id==='photo'&&i.sent),false);assert.ok(info.warnings.length);
});
test('two distinct voice selections from the same file keep their own clip starts',()=>{
  const p=fixture();p.entities.push(character('hero','photo','voice'),image('photo'),audio('voice'));shotOf(p).data.cast=[{characterId:'hero',lookId:'look'}];shotOf(p).data.storyboard.extraRefs=[{entityId:'voice',clipStart:4}];
  const voices=expanded(p).items.filter(i=>i.kind==='声音'&&i.sent);assert.deepEqual(voices.map(i=>i.clipStart),[1,4]);
});
test('REF duration policy omits later voice clips without deleting saved references',()=>{
  const p=fixture();p.entities.push(audio('first'),audio('later'));shotOf(p).data.storyboard.extraRefs=[{entityId:'first',clipStart:1},{entityId:'later',clipStart:2}];const cap=capabilities({constraints:{input_limits:{max_audio_duration_seconds:3}}});
  const info=expanded(p,cap);assert.deepEqual(info.items.filter(i=>i.sent).map(i=>i.a.id),['first']);assert.match(info.items[1].reason,/参考时长/);assert.equal(shotOf(p).data.storyboard.extraRefs.length,2);
});
test('an unavailable or wrong-kind profile main image is visible but never marked sent',()=>{
  const p=fixture();p.entities.push(character('hero','wrong'),audio('wrong'));shotOf(p).data.cast=[{characterId:'hero',lookId:'look'}];let info=expanded(p);assert.equal(info.items[0].sent,false);assert.match(info.items[0].reason,/类型/);
  p.entities.find(e=>e.id==='wrong').type='image';p.entities.find(e=>e.id==='wrong').data.missingFile=true;info=expanded(p);assert.equal(info.items[0].sent,false);assert.match(info.items[0].reason,/文件缺失/);
});
test('continue previous only previews a frame bound to the currently chosen real artifact',()=>{
  const p=fixture();const current=shotOf(p);current.order=1;current.data.storyboard.mode='fl';current.data.storyboard.continuePrevious=true;current.data.storyboard.previousFrame={shotId:'previous',selectedAssetId:'chosen',frameAssetId:'frame'};
  p.entities.push(entity('previous','shot',{seconds:5,selectedAssetId:'chosen'},'scene'),video('chosen'),video('changed'),image('frame'));p.entities.find(e=>e.id==='frame').data.sourceSelection={kind:'artifact_frame',position:'last',artifact_id:'chosen'};
  for(const id of ['chosen','changed'])p.jobs.push({id:'job-'+id,status:'succeeded',client_ref:{project_id:'project',shot_id:'previous'},effective_request:{mode:'fl',model:'MiniMax-H3-Pruned-Rank8-INT8'},artifacts:[{id,kind:'video'}]});
  let state=view(p),info=expandStoryboard(state.shots.find(s=>s.id==='shot'),state);assert.equal(info.items[0].a.id,'frame');assert.equal(info.items[0].sent,true);
  p.entities.find(e=>e.id==='previous').data.selectedAssetId='changed';state=view(p);info=expandStoryboard(state.shots.find(s=>s.id==='shot'),state);assert.equal(info.items[0].sent,false);assert.match(info.items[0].reason,/当前选定/);
});
test('drop classification rejects non-image first frames and preserves inactive FL audio/video refs',()=>{
  const p=fixture();p.entities.push(audio('voice'),video('motion'));const state=view(p),s=state.shots[0];s.mode='FL';assert.equal(storyboardDrop(s,{asset:state.assets.find(a=>a.id==='voice')},'start',state).patch,null);
  assert.deepEqual(storyboardDrop(s,{asset:state.assets.find(a=>a.id==='motion')},'smart',state).patch,{refs:['motion']});assert.equal(s.refs.length,0);
});
test('profile look imageIds empty explicitly clears gallery while absent imageIds keeps legacy images',()=>{
  const p=fixture();const hero=character('hero','photo');hero.data.looks=[{id:'clear',name:'clear',gallery:{main:'photo'},imageIds:[]},{id:'legacy',name:'legacy',gallery:{main:'photo'}}];p.entities.push(hero,image('photo'));const looks=view(p).profiles[0].variants;
  assert.deepEqual(looks[0].images,[]);assert.deepEqual(looks[1].images,['photo']);
});
test('look voice binding and chosen asset remain explicit; succeeded new jobs do not auto-adopt',()=>{
  const p=fixture();const hero=character('hero','photo','main-voice');hero.data.voices.push({id:'other',assetId:'look-voice',clipStart:3});hero.data.looks[0].voiceId='other';p.entities.push(hero,image('photo'),audio('main-voice'),audio('look-voice'),video('keep'));shotOf(p).data.cast=[{characterId:'hero',lookId:'look'}];shotOf(p).data.selectedAssetId='keep';
  const info=expanded(p);assert.equal(info.items.find(i=>i.kind==='声音').a.id,'look-voice');assert.equal(info.items.find(i=>i.kind==='声音').clipStart,3);
  mergeJobCandidate(p,{id:'new-job',status:'succeeded',client_ref:{project_id:'project',shot_id:'shot',shot_version:1},artifacts:[{id:'new-result',kind:'video',mime:'video/mp4'}]});assert.equal(view(p).shots[0].chosen,'keep');assert.ok(view(p).shots[0].versions.some(v=>v.artifactId==='new-result'));
});
test('storyboard preflight snapshot invalidates when shared look, clip start or global style changes',async()=>{
  const p=fixture();p.entities.push(character('hero','photo','voice'),image('photo'),audio('voice'));shotOf(p).data.cast=[{characterId:'hero',lookId:'look'}];let before=await digest(shotSnapshot(p,'shot'));
  p.entities.find(e=>e.id==='hero').data.voices[0].clipStart=2;assert.notEqual(await digest(shotSnapshot(p,'shot')),before);before=await digest(shotSnapshot(p,'shot'));
  p.entities.find(e=>e.id==='hero').data.looks[0].description='black coat';assert.notEqual(await digest(shotSnapshot(p,'shot')),before);before=await digest(shotSnapshot(p,'shot'));
  p.journey.storyboard.style='Watercolor';assert.notEqual(await digest(shotSnapshot(p,'shot')),before);
});
test('an unrelated completed shot or profile never invalidates this shots frozen preflight',async()=>{
  const p=fixture();p.entities.push(image('photo'),character('hero','photo'));shotOf(p).data.cast=[{characterId:'hero',lookId:'look'}];const before=await digest(shotSnapshot(p,'shot'));
  p.entities.push(entity('other-shot','shot',{seconds:5,prompt:'A different scene'},'scene',1),character('unused','photo'));
  mergeJobCandidate(p,{id:'other-job',status:'succeeded',client_ref:{project_id:'project',shot_id:'other-shot',shot_version:1},artifacts:[{id:'other-result',kind:'video',mime:'video/mp4'}]});
  assert.equal(await digest(shotSnapshot(p,'shot')),before);
});
