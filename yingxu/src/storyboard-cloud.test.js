import test from 'node:test';
import assert from 'node:assert/strict';
import {createStore} from './store.js';
import {createCloudController} from './cloud-controller.js';
import {createCloudClient,CloudError} from './cloud-client.js';
import {shotSnapshot,digest,stableJSON} from './cloud-model.js';
import {applyStoryboardAction} from './storyboard-actions.js';

const clone=value=>structuredClone(value);
function memory(){const map=new Map();return {map,getItem:key=>map.get(key)??null,setItem:(key,value)=>map.set(key,value)};}
const controls={duration:{type:'integer',minimum:4,maximum:15,default:5},resolution:{type:'string',enum:['480P'],default:'480P'},steps:{type:'integer',minimum:1,maximum:50,default:8},seed:{type:['string','null'],default:null},generate_audio:{type:'boolean',default:true},guides:{type:'array',default:[]},video_audio:{type:'object',default:{}}};
const recipes=['fl','ref'].map(mode=>({id:`h3-base-${mode==='fl'?'fl2va':'ref2va'}-v1`,mode,controls}));
const profiles=['draft','final'].map(id=>({id,label:id,model_id:`MiniMax-H3-${id==='draft'?'Pruned-Rank8':'Base'}-INT8`,generation_support:Object.fromEntries(['fl','ref'].map(mode=>[mode,{enabled:true,configured:true,controls,limits:{max_images:5,max_videos:2,max_audios:3,max_total_files:8}}]))}));
const capabilities={capabilities_version:'story-v1',execution_enabled:true,recipes,deployment_profiles:profiles,default_deployment_profile_id:'draft'};

async function harness(){
  const storage=memory(),store=createStore({storage}),remote=new Map(),calls=[],jobs=[],files=new Map();
  store.newProject('Owner story');const chapter=store.addEntity('chapter'),scene=store.addEntity('scene',chapter),shot=store.addEntity('shot',scene,{description:'Run across the roof',data:{seconds:5}});
  store.updateEntity(shot,{data:{storyboard:{version:1,mode:'ref',quality:'draft',place:null,propIds:[],firstFrameId:'',lastFrameId:'',continuePrevious:false,extraRefs:[]},h3:{inputMode:'ref',recipeId:recipes[1].id,deploymentProfileId:'draft',controls:{duration:5}}}});
  const initial=store.exportProject();remote.set(initial.id,{id:initial.id,version:1,project:initial});
  const api={
    reset(){},setAccount(){},authConfig:async()=>({authentication:'password',auth_ready:true}),me:async()=>({username:'superdan',features:{storyboard_workspace:true},operator_capacity:{view:true}}),login:async username=>({username}),logout:async()=>({ok:true}),capabilities:async()=>clone(capabilities),
    generationAvailability:async()=>({version:1,observed_at:Date.now()/1000,expires_at:Date.now()/1000+10,advisory_only:true,profiles:profiles.map(p=>({deployment_profile_id:p.id,modes:Object.fromEntries(recipes.map(r=>[r.mode,{recipe_id:r.id,state:'ready',available:true,reason_code:null}]))}))}),
    projects:async()=>({projects:[...remote.values()].map(r=>({id:r.id,title:r.project.title,version:r.version}))}),project:async id=>clone(remote.get(id)),
    saveProject:async(id,project,version)=>{calls.push(['save',id,version]);assert.equal(remote.get(id).version,version);const receipt={id,version:version+1,project:clone(project)};remote.set(id,receipt);return clone(receipt);},
    upload:async(file,projectId,id,key)=>{calls.push(['upload',file.name,projectId,id,key]);return {asset_id:'synced-reference',status:'ready',kind:'image',metadata:{mime:'image/png'},content_url:'/v1/assets/synced-reference/content'};},
    jobs:async()=>({jobs:clone(jobs)}),artifacts:async()=>({artifacts:[]}),
    generationDraft:async(projectId,shotId)=>{calls.push(['draft',projectId,shotId]);const saved=remote.get(projectId);return {project_id:projectId,shot_id:shotId,project_version:saved.version,shot_version:saved.project.entities.find(e=>e.id===shotId).version,issues:[],draft:{recipe_id:recipes[1].id,deployment_profile_id:'draft',prompt:'Server-composed prompt',storyboard:{prompt_parts:[],references:[],notes:[]}}};},
    savedPlan:async(projectId,shotId,body,key)=>{calls.push(['savedPlan',body,key]);assert.equal(body.expected_version,remote.get(projectId).version);return {plan_id:'saved-plan',status:'ready',expires_at:Date.now()/1000+300,client_ref:{project_id:projectId,shot_id:shotId,shot_version:remote.get(projectId).project.entities.find(e=>e.id===shotId).version,source_hash:'authoritative-server-hash'},effective_request:{model:profiles[0].model_id,mode:'ref',prompt:'Server-composed prompt',duration:5,resolution:'480P',steps:8},estimate:{}};},
    plan:async(payload,key)=>{calls.push(['plan',clone(payload),key]);return {plan_id:'final-plan',status:'ready',expires_at:Date.now()/1000+300,client_ref:clone(payload.client_ref),effective_request:{...clone(payload.controls),mode:'ref',prompt:payload.prompt,model:profiles[1].model_id},estimate:{}};},
    submit:async(planId,key)=>{calls.push(['submit',planId,key]);const plan=controller.getState().plans[shot],job={id:'accepted-job',plan_id:planId,recipe_id:plan.availabilitySettings.recipe_id,client_ref:clone(plan.client_ref),status:'queued',artifacts:[]};if(!jobs.length)jobs.push(job);return clone(job);},
    saveFrame:async(projectId,artifactId,selection,key)=>{calls.push(['frame',projectId,artifactId,clone(selection),key]);return {asset_id:'frame-receipt',id:'frame-receipt',project_id:projectId,status:'ready',kind:'image',file_name:'frame.png',content_url:'/v1/assets/frame-receipt/content',metadata:{bytes:100},selection:{kind:'artifact_frame',artifact_id:artifactId,job_id:'original-job',...selection,frame_index:119,time_seconds:119/24,source_sha256:'a'.repeat(64)}};},
    job:async id=>{calls.push(['job',id]);throw new CloudError('source missing',{status:404});},
  };
  const controller=createCloudController({store,storage,client:api,mediaHandlers(){},readFile:async id=>files.get(id)||null});
  await controller.login('superdan','test-only');await controller.open(initial.id);
  return {store,storage,api,controller,remote,calls,jobs,files,chapter,scene,shot};
}

test('storyboard preflight uses saved server compilation, then confirms the same frozen plan',async()=>{
  const h=await harness();try{
    await h.controller.prepareStoryboard(h.shot);assert.deepEqual(h.calls.map(c=>c[0]),['save','draft','savedPlan']);
    const plan=h.controller.getState().plans[h.shot];assert.equal(plan.client_ref.source_hash,'authoritative-server-hash');assert.match(plan.sourceHash,/^[a-f0-9]{64}$/);assert.notEqual(plan.sourceHash,plan.client_ref.source_hash);
    await h.controller.submit(h.shot);assert.equal(h.calls.at(-1)[0],'submit');assert.equal(h.calls.at(-1)[1],'saved-plan');assert.equal(h.jobs.length,1);
    assert.equal(h.store.exportProject().jobs[0].id,'accepted-job');assert.equal(!!h.store.exportProject().entities.find(e=>e.id===h.shot).data.selectedAssetId,false);
  }finally{h.controller.destroy();}
});
test('capable REF continuation extracts and binds the chosen previous tail frame before saved-draft preflight',async()=>{
  const h=await harness();try{
    const currentCapabilities=clone(capabilities);currentCapabilities.deployment_profiles[0].generation_support.ref.input_support={first_frame:true,last_frame:true,reference_images:true,reference_video:true,reference_audio:true};currentCapabilities.deployment_profiles[0].generation_support.ref.limits.input_count_semantics='native_roles_v2';h.api.capabilities=async()=>currentCapabilities;
    const previous=h.store.addEntity('shot',h.scene,{data:{seconds:5,prompt:'Previous action'}}),chosen=h.store.addEntity('video',null,{data:{fileId:'cloud_artifact_previous',cloudArtifactId:'previous',sourceJobId:'previous-job'}});
    h.store.editProject(project=>{project.entities.find(entity=>entity.id===previous).order=0;project.entities.find(entity=>entity.id===previous).data.selectedAssetId=chosen;const current=project.entities.find(entity=>entity.id===h.shot);current.order=1;current.data.storyboard.continuePrevious=true;});
    await h.controller.prepareStoryboard(h.shot);const frameCall=h.calls.find(call=>call[0]==='frame');assert.equal(frameCall[2],'previous');assert.deepEqual(frameCall[3],{position:'last'});
    const savedShot=h.store.exportProject().entities.find(entity=>entity.id===h.shot),binding=savedShot.data.storyboard.previousFrame;assert.equal(binding.shotId,previous);assert.equal(binding.selectedAssetId,chosen);assert.ok(binding.frameAssetId);assert.equal(savedShot.data.storyboard.mode,'ref');
    assert.equal(h.calls.some(call=>call[0]==='savedPlan'),true);assert.equal(h.calls.some(call=>call[0]==='submit'),false);
  }finally{h.controller.destroy();}
});

test('failed re-preflight cannot leave an older ready plan available for confirmation',async()=>{
  const h=await harness();try{
    await h.controller.prepareStoryboard(h.shot);h.api.generationDraft=async()=>{throw new CloudError('draft read unavailable',{status:503});};
    await assert.rejects(h.controller.prepareStoryboard(h.shot),/draft read unavailable/);assert.equal(h.controller.getState().plans[h.shot],undefined);
    assert.match(h.controller.getState().preflightErrors[h.shot],/draft read unavailable/);await assert.rejects(h.controller.submit(h.shot),/预检/);assert.equal(h.calls.some(c=>c[0]==='submit'),false);
  }finally{h.controller.destroy();}
});

test('Final redo uses original request even when its completed job is outside the loaded page',async()=>{
  const h=await harness();try{
    h.store.updateEntity(h.shot,{data:{storyboard:{...h.store.exportProject().entities.find(e=>e.id===h.shot).data.storyboard,quality:'final'},h3:{inputMode:'ref',recipeId:recipes[1].id,deploymentProfileId:'final',controls:{duration:5}}}});
    const original={id:'old-chosen-job',status:'succeeded',recipe_id:recipes[1].id,client_ref:{project_id:h.store.exportProject().id,shot_id:h.shot,shot_version:1,source_hash:'old-snapshot'},effective_request:{model:profiles[0].model_id,mode:'ref',prompt:'Original selected prompt',duration:5,resolution:'480P',steps:8,seed:'42',generate_audio:true,inputs:{images:['original-image'],videos:['original-video'],audios:['original-audio']},video_audio:{'original-video':true}}};
    const chosen=h.store.addEntity('video',null,{data:{fileId:'cloud_artifact_chosen',cloudArtifactId:'chosen',sourceJobId:original.id}});h.store.updateEntity(h.shot,{data:{selectedAssetId:chosen,candidateIds:[chosen]}});
    h.api.job=async id=>{assert.equal(id,original.id);return clone(original);};
    h.store.updateEntity(h.shot,{data:{prompt:'New unsent editor prompt'}});await h.controller.prepareStoryboard(h.shot,{sourceJobId:original.id});
    const payload=h.calls.find(c=>c[0]==='plan')[1];assert.equal(payload.prompt,'Original selected prompt');assert.deepEqual(payload.inputs.images,[{asset_id:'original-image'}]);assert.deepEqual(payload.inputs.audios,[{asset_id:'original-audio'}]);assert.deepEqual(payload.inputs.videos,[{asset_id:'original-video',include_audio:true}]);assert.equal(payload.controls.seed,null);
    assert.equal(h.controller.getState().plans[h.shot].availabilitySettings.recipe_id,recipes[1].id);assert.equal(h.controller.getState().jobs.some(j=>j.id===original.id),false);
    await h.controller.submit(h.shot);assert.equal(h.calls.at(-1)[1],'final-plan');assert.equal(h.jobs.length,1);
  }finally{h.controller.destroy();}
});

test('successful saved storyboard job uses shot version without mistaking the server hash for a UI hash',async()=>{
  const h=await harness();try{
    await h.controller.prepareStoryboard(h.shot);const shot=h.store.exportProject().entities.find(e=>e.id===h.shot);
    h.jobs.push({id:'finished',status:'succeeded',recipe_id:recipes[1].id,client_ref:{project_id:h.store.exportProject().id,shot_id:h.shot,shot_version:shot.version,source_hash:'authoritative-server-hash'},artifacts:[{id:'video',kind:'video',content_url:'/v1/artifacts/video/content',metadata:{duration:5}},{id:'audio',kind:'audio',content_url:'/v1/artifacts/audio/content'}]});
    await h.controller.refresh();const result=h.store.exportProject().entities.find(e=>e.data.cloudArtifactId==='video');assert.equal(result.data.oldVersion,false);assert.equal(!!h.store.exportProject().entities.find(e=>e.id===h.shot).data.selectedAssetId,false);
    h.store.editProject(p=>applyStoryboardAction(p,{type:'update',id:h.shot,patch:{prompt:'Change after completion'}},{chapterId:h.chapter})!==false);
    h.jobs.push({id:'late-finished',status:'succeeded',client_ref:{project_id:h.store.exportProject().id,shot_id:h.shot,shot_version:shot.version,source_hash:'other-server-hash'},artifacts:[{id:'late-video',kind:'video',content_url:'/v1/artifacts/late-video/content'}]});
    await h.controller.refresh();assert.equal(h.store.exportProject().entities.find(e=>e.data.cloudArtifactId==='late-video').data.oldVersion,true);
  }finally{h.controller.destroy();}
});

test('frame response loss recovers one stable receipt and never creates another generation',async()=>{
  const h=await harness();try{
    const normal=h.api.saveFrame;let fail=true;h.api.saveFrame=async(...args)=>{const response=await normal(...args);if(fail){fail=false;throw Error('response lost');}return response;};
    await assert.rejects(h.controller.saveFrame('original-video',{position:'last'}),/response lost/);
    const first=await h.controller.saveFrame('original-video',{position:'last'}),second=await h.controller.saveFrame('original-video',{position:'last'});assert.equal(first,second);
    const frames=h.calls.filter(c=>c[0]==='frame');assert.equal(new Set(frames.map(c=>c[4])).size,1);assert.equal(h.store.exportProject().entities.filter(e=>e.data.cloudAssetId==='frame-receipt').length,1);assert.equal(h.jobs.length,0);assert.equal(h.calls.some(c=>['plan','savedPlan','submit'].includes(c[0])),false);
  }finally{h.controller.destroy();}
});

test('late frame response cannot cross the same-ID workspace epoch or owner switch',async()=>{
  const h=await harness();try{
    const normal=h.api.saveFrame;let finish,entered;const requested=new Promise(resolve=>entered=resolve);h.api.saveFrame=async(...args)=>new Promise(resolve=>{finish=async()=>resolve(await normal(...args));entered();});
    const pending=h.controller.saveFrame('original-video',{position:'last'}),rejected=assert.rejects(pending,/账户或项目已经切换/);await requested;
    const before=h.store.exportProject();h.store.enterCloudProject(before,{account:'superdan',version:1});await finish();await rejected;assert.equal(h.store.exportProject().entities.some(e=>e.data.cloudAssetId==='frame-receipt'),false);
  }finally{h.controller.destroy();}
});

test('unused media/results do not stale a storyboard plan but bound profile and previous selected source do',async()=>{
  const h=await harness();try{
    const p=h.store.exportProject(),shot=p.entities.find(e=>e.id===h.shot),portrait={id:'portrait',type:'image',title:'portrait',description:'',parentId:null,order:0,version:1,status:'draft',data:{fileId:'cloud_asset_portrait',cloudAssetId:'portrait'}};
    p.entities.push(portrait,{id:'hero',type:'character',title:'Hero',description:'blue coat',parentId:null,order:1,version:1,status:'draft',data:{looks:[{id:'coat',name:'Coat',description:'blue',imageIds:['portrait']}],defaultLookId:'coat'}});shot.data.cast=[{characterId:'hero',lookId:'coat'}];
    const initial=stableJSON(shotSnapshot(p,h.shot));p.entities.push({...portrait,id:'unused',data:{fileId:'cloud_asset_unused',cloudAssetId:'unused'}});assert.equal(stableJSON(shotSnapshot(p,h.shot)),initial);
    p.entities.push({...portrait,id:'result-unrelated',type:'video',data:{fileId:'cloud_artifact_other',cloudArtifactId:'other',sourceJobId:'unrelated'}});assert.equal(stableJSON(shotSnapshot(p,h.shot)),initial);
    p.entities.find(e=>e.id==='hero').description='red coat';assert.notEqual(stableJSON(shotSnapshot(p,h.shot)),initial);
    const first={...clone(shot),id:'previous',order:0,data:{...clone(shot.data),selectedAssetId:'chosen-a'}},last=p.entities.find(e=>e.id===h.shot);last.order=1;last.data.storyboard.continuePrevious=true;p.entities.push(first,{...portrait,id:'chosen-a',type:'video',data:{fileId:'cloud_artifact_a',cloudArtifactId:'a'}},{...portrait,id:'chosen-b',type:'video',data:{fileId:'cloud_artifact_b',cloudArtifactId:'b'}});
    const prior=await digest(shotSnapshot(p,h.shot));first.data.selectedAssetId='chosen-b';assert.notEqual(await digest(shotSnapshot(p,h.shot)),prior);
  }finally{h.controller.destroy();}
});

test('preflight synchronizes its active readable reference without touching unrelated missing local files',async()=>{
  const h=await harness();try{
    const active=h.store.addEntity('image',null,{title:'Active reference',data:{fileId:'local-active',fileName:'active.png',mime:'image/png'}});h.files.set('local-active',new Blob(['readable image'],{type:'image/png'}));
    const unused=h.store.addEntity('image',null,{title:'Unrelated missing file',data:{fileId:'missing-unused',fileName:'do-not-read.png',mime:'image/png'}});
    h.store.updateEntity(h.shot,{data:{storyboard:{...h.store.exportProject().entities.find(e=>e.id===h.shot).data.storyboard,extraRefs:[{entityId:active}]}}});
    await h.controller.prepareStoryboard(h.shot);const uploaded=h.calls.filter(c=>c[0]==='upload');assert.equal(uploaded.length,1);assert.equal(uploaded[0][1],'active.png');
    assert.equal(h.store.exportProject().entities.find(e=>e.id===active).data.cloudAssetId,'synced-reference');assert.equal(h.store.exportProject().entities.find(e=>e.id===unused).data.cloudAssetId,undefined);
    assert.equal(h.calls.findIndex(c=>c[0]==='upload')<h.calls.findIndex(c=>c[0]==='savedPlan'),true);
  }finally{h.controller.destroy();}
});

test('changing the chosen Final source during receipt read or before confirmation rejects the old plan',async()=>{
  const h=await harness();try{
    const previous=h.store.addEntity('video',null,{data:{fileId:'cloud_artifact_previous',cloudArtifactId:'previous',sourceJobId:'source-job'}}),next=h.store.addEntity('video',null,{data:{fileId:'cloud_artifact_next',cloudArtifactId:'next',sourceJobId:'another-job'}});
    h.store.updateEntity(h.shot,{data:{selectedAssetId:previous,storyboard:{...h.store.exportProject().entities.find(e=>e.id===h.shot).data.storyboard,quality:'final'},h3:{inputMode:'ref',recipeId:recipes[1].id,deploymentProfileId:'final',controls:{duration:5}}}});
    let entered,finish;const requested=new Promise(resolve=>entered=resolve);h.api.job=async()=>new Promise(resolve=>{finish=()=>resolve({id:'source-job',status:'succeeded',recipe_id:recipes[1].id,client_ref:{project_id:h.store.exportProject().id,shot_id:h.shot,shot_version:1},effective_request:{mode:'ref',prompt:'Original source',duration:5,resolution:'480P',steps:8,inputs:{images:[],videos:[],audios:[]}}});entered();});
    const pending=h.controller.prepareStoryboard(h.shot,{sourceJobId:'source-job'}),rejected=assert.rejects(pending,/选择已改变|镜头或档案已改变/);await requested;h.store.updateEntity(h.shot,{data:{selectedAssetId:next}});finish();await rejected;
    assert.equal(h.controller.getState().plans[h.shot],undefined);assert.equal(h.calls.some(c=>c[0]==='submit'),false);assert.equal(h.store.exportProject().entities.find(e=>e.id===h.shot).data.selectedAssetId,next);
    h.store.updateEntity(h.shot,{data:{selectedAssetId:previous}});h.api.job=async()=>({id:'source-job',status:'succeeded',recipe_id:recipes[1].id,client_ref:{project_id:h.store.exportProject().id,shot_id:h.shot,shot_version:1},effective_request:{mode:'ref',prompt:'Original source',duration:5,resolution:'480P',steps:8,inputs:{images:[],videos:[],audios:[]}}});
    await h.controller.prepareStoryboard(h.shot,{sourceJobId:'source-job'});h.store.updateEntity(h.shot,{data:{selectedAssetId:next}});await assert.rejects(h.controller.submit(h.shot),/选择|预检|改变/);assert.equal(h.calls.some(c=>c[0]==='submit'),false);
  }finally{h.controller.destroy();}
});

test('new frame and saved draft clients carry normal expected-account protection and exact owned URLs',async()=>{
  const calls=[],client=createCloudClient({fetcher:async(path,options)=>{calls.push({path,options});return new Response('{}',{headers:{'X-Authenticated-Account':'superdan'}});}});client.setAccount('superdan');
  await client.saveFrame('owned-project','owned-artifact',{position:'last'},'stable-frame-key');await client.generationDraft('owned-project','owned-shot');await client.savedPlan('owned-project','owned-shot',{expected_version:4,capabilities_version:'v'},'stable-plan-key');
  assert.equal(calls[0].path,'/v1/projects/owned-project/artifacts/owned-artifact/frames');assert.equal(calls[0].options.headers['Idempotency-Key'],'stable-frame-key');assert.deepEqual(JSON.parse(calls[0].options.body),{position:'last'});
  assert.equal(calls[1].path,'/v1/projects/owned-project/shots/owned-shot/generation-draft');assert.equal(calls[2].path,'/v1/projects/owned-project/shots/owned-shot/generation-plans');for(const call of calls)assert.equal(call.options.headers['X-Expected-Account'],'superdan');
  const foreign=createCloudClient({fetcher:async()=>new Response('{"private":"other owner"}',{headers:{'X-Authenticated-Account':'supervan'}})});foreign.setAccount('superdan');await assert.rejects(foreign.saveFrame('owned-project','owned-artifact',{position:'last'},'same-key'),/旧账户响应未展示/);
});
