import test from 'node:test';
import assert from 'node:assert/strict';
import {createQuickChatClient} from './quick-chat-client.js';
import {createQuickChatController,saveMaterialSelection} from './quick-chat-controller.js';
import {saveDraft,readDraft,draftSnapshot,rememberDraftOrigin,clearAcceptedDraft,carryDraft} from './quick-chat-draft.js';
import {defaultsFor,nextSettingsFor,effectiveControlSchema,effectiveLimits,settingsProblems,bindingProblems,bindingPayload,bindingsToInputs,mergeTimelineTurns,isInternalDerivedInventory} from './quick-chat-model.js';
const memory=()=>{const data=new Map();return {getItem:key=>data.get(key)||null,setItem:(key,value)=>data.set(key,value),removeItem:key=>data.delete(key)};};
const capability={recipes:[{id:'fl',mode:'fl',controls:{duration:{type:'integer',minimum:4,maximum:15,default:5},resolution:{type:'string',enum:['480P','768P'],default:'768P'},seed:{type:['string','null'],default:null},steps:{type:'integer',minimum:1,maximum:100,default:50},video_decode:{type:'string',enum:['normal','tiled'],default:'normal'}},limits:{max_images:9,max_videos:3,max_audios:3,max_total_files:12,max_guides:8,min_clip_duration:2,max_clip_duration:15,max_total_video_duration:15,max_total_audio_duration:15},execution_support:{constraints:{max_steps:50,max_duration_seconds:124/24,max_reference_files:3,max_guides:1,controls:{video_decode:['tiled']},input_limits:{max_images:1,max_videos:1,max_audios:1,max_video_duration_seconds:10,max_audio_duration_seconds:10,guide_kinds:['image'],guide_recipe_ids:['ref']}}},deployment_preset:{controls:{video_decode:'tiled'}}}]};
test('pool constraints tighten defaults and native duration without widening schema',()=>{const recipe=capability.recipes[0],schema=effectiveControlSchema(recipe),defaults=defaultsFor(capability);assert.equal(schema.duration.maximum,5);assert.equal(schema.steps.maximum,50);assert.deepEqual(schema.video_decode.enum,['tiled']);assert.equal(defaults.controls.video_decode,'tiled');assert.equal(effectiveLimits(recipe).max_images,1);assert.deepEqual(capability.recipes[0].controls.video_decode.enum,['normal','tiled']);});
test('uint64 seeds stay decimal strings; overflow and lossy numbers fail',()=>{const recipe=capability.recipes[0];assert.deepEqual(settingsProblems({copies:1,controls:{seed:'18446744073709551615'}},recipe),[]);assert.ok(settingsProblems({copies:1,controls:{seed:'18446744073709551616'}},recipe).length);assert.ok(settingsProblems({copies:1,controls:{seed:18446744073709551615}},recipe).length);});
test('material catalog preserves disabled and incompatible selections, effective inputs are explicit',()=>{const bindings=[{binding_id:'first',asset_id:'a',kind:'image',slot:'first_frame',enabled:true,asset:{id:'a',kind:'image',status:'ready'}},{binding_id:'motion',asset_id:'b',kind:'video',slot:'videos',enabled:true,source_range:{start:1,end:6},include_audio:false,asset:{id:'b',kind:'video',metadata:{source_duration:30},status:'ready'}},{binding_id:'off',asset_id:'c',kind:'image',slot:'images',enabled:false,asset:{id:'c',kind:'image',status:'ready'}}];const fl=bindingsToInputs(bindings,{mode:'fl'});assert.equal(fl.first_frame.asset_id,'a');assert.deepEqual(fl.videos,[]);const ref=bindingsToInputs(bindings,{mode:'ref'});assert.equal(ref.first_frame,null);assert.equal(ref.videos[0].include_audio,false);assert.deepEqual(ref.videos[0].source_range,{start:1,end:6});assert.equal(bindings[1].enabled,true);assert.equal(bindings[2].enabled,false);});
test('guides use their independent current recipe allowance',()=>{const binding={binding_id:'g',asset_id:'a',kind:'image',slot:'guides',enabled:true,time_seconds:1,asset:{id:'a',kind:'image',status:'ready'}};assert.ok(bindingProblems([binding],capability.recipes[0],{duration:5}).includes('当前方式尚未开放这种时间锚点。'));assert.deepEqual(bindingProblems([binding],{...capability.recipes[0],id:'ref',mode:'ref'},{duration:5}),[]);});
test('assistant lifecycle updates one durable turn, not duplicate history messages',()=>{const initial={id:'t',seq:1,text:'我的想法',status:'running'};const turns=mergeTimelineTurns([{id:'e1',seq:1,type:'turn.created',record:initial},{id:'e2',seq:2,type:'assistant.completed',record:{...initial,status:'completed',reply:'持久结果'}}]);assert.equal(turns.length,1);assert.equal(turns[0].reply,'持久结果');});
test('client uses same-origin account fence, original idempotency and exact creative paths',async()=>{const calls=[],client=createQuickChatClient({fetcher:async(path,options)=>{calls.push({path,options});return new Response('{"session":{"id":"s"}}');}});client.setAccount('superdan');await client.createSession({model_id:'gemini-3.8-flash'},'logical-create');await client.preflight('s','r',{item_ids:['i'],retry_of_execution_id:'old'},'logical-pref');assert.equal(calls[0].path,'/v1/quick-chat/sessions');assert.equal(calls[0].options.credentials,'same-origin');assert.equal(calls[0].options.headers['X-Expected-Account'],'superdan');assert.equal(calls[1].options.headers['Idempotency-Key'],'logical-pref');assert.deepEqual(JSON.parse(calls[1].options.body).item_ids,['i']);});
function fakeClient(overrides={}){let account;return {setAccount:value=>account=value,reset(){},schema:async()=>({models:[]}),capabilities:async()=>capability,sessions:async()=>({sessions:[{id:'s',title:'测试',version:1}]}),createSession:async()=>({session:{id:'s',title:'测试',version:1}}),session:async id=>({session:{id,version:1}}),materials:async()=>({bindings:[]}),timeline:async()=>({events:[]}),...overrides};}
test('a durable generation receipt clears only the submitted draft, preserving settings and history on reload',async()=>{
  const storage=memory();saveDraft('owner','s','original prompt',{storage,edit:true});
  const draft=draftSnapshot(storage,'owner','s');let accepted=false;
  const receipt={status:'accepted',turn_id:'turn',revision_id:'revision',submission_id:'submission'},settings={recipe_id:'fl',controls:{resolution:'768P'},copies:2};
  const client=fakeClient({session:async()=>({session:{id:'s',version:accepted?2:1,next_settings:settings,composer_reset:accepted?receipt:null}}),turn:async()=>({id:'turn'}),timeline:async()=>({events:[{id:'old',seq:1,type:'turn.created',record:{id:'old-turn',text:'history',status:'recorded'}}]})});
  const controller=createQuickChatController({client,storage});await controller.setAccount('owner');await controller.open('s');
  await controller.turn('original prompt','gemini-3.8-flash','none',{draft});
  assert.equal(readDraft('owner','s',storage),'original prompt');assert.equal(controller.getState().draftReset,null);
  accepted=true;await controller.poll();assert.equal(readDraft('owner','s',storage),'');assert.equal(controller.getState().draftReset.submissionId,'submission');
  assert.deepEqual(controller.getState().session.next_settings,settings);assert.equal(controller.getState().timeline.length,1);
  const fresh=createQuickChatController({client,storage});await fresh.setAccount('owner');await fresh.open('s');assert.equal(readDraft('owner','s',storage),'');assert.equal(fresh.getState().timeline.length,1);
});
test('newer draft edits including ABA survive late accepted submission and account isolation is retained',()=>{
  const storage=memory();saveDraft('a','s','same',{storage,edit:true});const original=draftSnapshot(storage,'a','s');
  saveDraft('a','s','newer',{storage,edit:true});saveDraft('a','s','same',{storage,edit:true});rememberDraftOrigin(storage,'a','s',original,'turn');
  const receipt={status:'accepted',turn_id:'turn'};assert.equal(clearAcceptedDraft(storage,'a','s',receipt),false);assert.equal(readDraft('a','s',storage),'same');
  saveDraft('b','s','other account',{storage,edit:true});assert.equal(clearAcceptedDraft(storage,'b','s',receipt),false);assert.equal(readDraft('b','s',storage),'other account');
  const current=draftSnapshot(storage,'a','s');rememberDraftOrigin(storage,'a','s',current,'next');assert.equal(clearAcceptedDraft(storage,'a','s',{status:'admission_blocked',turn_id:'next'}),false);
  assert.equal(clearAcceptedDraft(storage,'a','s',{status:'accepted',turn_id:'next'}),true);assert.equal(clearAcceptedDraft(storage,'a','s',{status:'accepted',turn_id:'next'}),false);
});
test('first-session carry preserves draft identity and recovery links the original turn without resubmission',async()=>{
  const storage=memory();saveDraft('owner',null,'first prompt',{storage,edit:true});const draft=draftSnapshot(storage,'owner',null);carryDraft(storage,'owner',null,'owner','s');
  let committed=false;const calls=[];const client=fakeClient({session:async()=>({session:{id:'s',version:1,composer_reset:committed?{status:'accepted',turn_id:'turn',submission_id:'original-submission'}:null}}),turn:async(_id,body,key)=>{calls.push({body,key});committed=true;if(calls.length===1)throw Error('lost response');return {id:'turn'};}});
  const first=createQuickChatController({client,storage});await first.setAccount('owner');await first.open('s');await assert.rejects(first.turn('first prompt','gemini-3.8-flash','none',{draft,createCard:true}));
  assert.equal(readDraft('owner','s',storage),'first prompt');assert.deepEqual(first.getState().pending.draft,draft);
  const restored=createQuickChatController({client,storage});await restored.setAccount('owner');await restored.open('s');assert.equal(readDraft('owner','s',storage),'first prompt');
  await restored.recover();assert.deepEqual(calls[0],calls[1]);assert.equal(restored.getState().pending,null);assert.equal(readDraft('owner','s',storage),'');
});
test('late accepted receipt from a former account cannot clear either account draft',async()=>{
  const storage=memory();for(const account of ['old','new'])saveDraft(account,'s',account+' draft',{storage,edit:true});
  rememberDraftOrigin(storage,'old','s',draftSnapshot(storage,'old','s'),'old-turn');let defer=false,finish;
  const client=fakeClient({session:async()=>defer?new Promise(resolve=>{finish=resolve;}):{session:{id:'s',version:1}}});
  const controller=createQuickChatController({client,storage});await controller.setAccount('old');await controller.open('s');defer=true;const poll=controller.poll();await Promise.resolve();await controller.setAccount('new');
  finish({session:{id:'s',version:2,composer_reset:{status:'accepted',turn_id:'old-turn'}}});await assert.rejects(poll);
  assert.equal(readDraft('old','s',storage),'old draft');assert.equal(readDraft('new','s',storage),'new draft');assert.equal(controller.getState().draftReset,null);
});
test('a completed automatic title refreshes loaded history without replacing paged sessions or timeline',async()=>{
  let title='新的创作',listReads=0;
  const client=fakeClient({
    sessions:async({cursor})=>{listReads++;return cursor?{sessions:[{id:'older',title:'早期作品',version:1}],next_cursor:'third-page'}:{sessions:[{id:'s',title,version:4,project_id:'compatibility-project'},{id:'other',title:'另一份创作',version:2}],next_cursor:'second-page'};},
    session:async()=>({session:{id:'s',title,version:4,updated_at:100}}),
    timeline:async(_id,{before_cursor,after_cursor}={})=>before_cursor?{events:[{id:'old-event',seq:1,type:'turn.created',record:{id:'old-turn',status:'recorded'}}],before_cursor:'older-history'}:after_cursor?{events:[],after_cursor:'forward'}:{events:[{id:'event',seq:2,type:'turn.created',record:{id:'turn',status:'recorded'}}],before_cursor:'previous-history',after_cursor:'forward'},
  }),controller=createQuickChatController({client,storage:memory()});
  await controller.setAccount('supervan');await controller.open('s');await controller.list({more:true});await controller.history();
  title='月下剑舞';await controller.poll();
  const state=controller.getState();assert.equal(state.session.title,title);assert.equal(state.session.version,4);
  assert.deepEqual(state.sessions.map(item=>[item.id,item.title]),[['s',title],['other','另一份创作'],['older','早期作品']]);
  assert.equal(state.sessions[0].project_id,'compatibility-project');assert.equal(state.sessions[0].version,4);assert.equal(state.sessions[0].updated_at,100);
  assert.equal(listReads,2);assert.equal(state.sessionsCursor,'third-page');assert.equal(state.historyCursor,'older-history');assert.equal(state.forwardCursor,'forward');
  assert.deepEqual(state.timeline.map(event=>event.id),['old-event','event']);
});
test('late title reads cannot return another account name into history',async()=>{
  let account,hold=false,finish;
  const client=fakeClient({setAccount:value=>account=value,sessions:async()=>({sessions:[{id:account,title:account+'作品',version:1}]}),session:async id=>{const value={session:{id,title:id+'标题',version:1}};if(hold){hold=false;return new Promise(resolve=>finish=()=>resolve(value));}return value;}}),controller=createQuickChatController({client,storage:memory()});
  await controller.setAccount('owner-one');await controller.open('owner-one');hold=true;
  const oldPoll=controller.poll().catch(error=>error);await controller.setAccount('owner-two');finish();await oldPoll;
  assert.equal(controller.getState().account,'owner-two');assert.equal(controller.getState().session,null);
  assert.deepEqual(controller.getState().sessions.map(item=>[item.id,item.title]),[['owner-two','owner-two作品']]);assert.equal(controller.getState().error,'');
});
test('a late poll cannot undo a manual history rename',async()=>{
  let session={id:'s',title:'新的创作',version:1},hold=false,finish;
  const client=fakeClient({sessions:async()=>({sessions:[structuredClone(session)]}),session:async()=>{const value={session:structuredClone(session)};if(hold){hold=false;return new Promise(resolve=>finish=()=>resolve(value));}return value;},patchSession:async(_id,body)=>{assert.equal(body.expected_version,1);session={...session,title:body.title,version:2};return {session};}}),controller=createQuickChatController({client,storage:memory()});
  await controller.setAccount('supervan');await controller.open('s');hold=true;
  const oldPoll=controller.poll().catch(error=>error);await controller.patch({title:'我选的作品名'});finish();await oldPoll;
  assert.equal(controller.getState().session.title,'我选的作品名');assert.equal(controller.getState().sessions[0].title,'我选的作品名');assert.equal(controller.getState().sessions[0].version,2);
});
test('lost creation is retained and recovered with exactly the same operation key/body',async()=>{const calls=[],client=fakeClient({createSession:async(body,key)=>{calls.push({body,key});if(calls.length===1)throw Error('transport interrupted');return {session:{id:'s',version:1}};}}),controller=createQuickChatController({client,storage:memory()});await controller.setAccount('superdan');await assert.rejects(controller.create({model_id:'gemini-3.8-flash'}));assert.ok(controller.getState().pending);await assert.rejects(controller.create({model_id:'gemma-4-31b-it'}));assert.equal(calls.length,1);await controller.recover();assert.equal(calls.length,2);assert.deepEqual(calls[0],calls[1]);assert.equal(controller.getState().pending,null);});
test('switching accounts drops late resources and keeps old unknown operations in their own bucket',async()=>{let finish;const client=fakeClient({schema:()=>new Promise(resolve=>finish=resolve)}),storage=memory(),controller=createQuickChatController({client,storage});const old=controller.setAccount('superdan');await controller.setAccount(null);finish({models:[{id:'old-account-model'}]});await old;assert.equal(controller.getState().account,null);assert.equal(controller.getState().schema,null);assert.deepEqual(controller.getState().sessions,[]);});
test('no storage means no mutation sent and no false success',async()=>{let calls=0;const client=fakeClient({createSession:async()=>{calls++;return {session:{id:'s'}};}}),controller=createQuickChatController({client,storage:{getItem:()=>null,setItem:()=>{throw Error('quota');},removeItem(){}}});await controller.setAccount('superdan');await assert.rejects(controller.create({model_id:'gemini-3.8-flash'}));assert.equal(calls,0);});
test('sequential multi-file uploads bind against current session and binding versions',async()=>{
  let version=1;const assets=[],bindings=[],calls=[];
  const client=fakeClient({session:async()=>({session:{id:'s',version}}),materials:async()=>({session_version:version,bindings:assets.map(asset=>({...bindings.find(b=>b.asset_id===asset.asset_id)||{binding_id:'b-'+asset.asset_id,version:0,asset_id:asset.asset_id,kind:asset.kind,slot:asset.kind==='image'?'images':'audios',enabled:false},asset}))}),uploadMaterial:async(_id,file,id)=>{const asset={asset_id:id,file_name:file.name,kind:id==='one'?'image':'audio',status:'ready'};assets.push(asset);return asset;},saveMaterials:async(_id,body)=>{assert.equal(body.expected_version,version);for(const b of body.bindings)assert.equal(b.version,bindings.find(old=>old.binding_id===b.binding_id)?.version??0);calls.push(structuredClone(body));bindings.splice(0,bindings.length,...body.bindings.map(b=>({...b,version:b.version+1})));version++;return {bindings};}});
  const controller=createQuickChatController({client,storage:memory()});await controller.setAccount('qc-review');await controller.open('s');
  for(const id of ['one','two']){await controller.upload({name:id+'.fixture'},id);const rows=controller.getState().materials.map(({asset,...binding})=>({...binding,enabled:true}));await controller.saveMaterials(rows);}
  assert.equal(calls[0].expected_version,1);assert.equal(calls[1].expected_version,2);assert.equal(calls[1].bindings.find(b=>b.asset_id==='one').version,1);assert.equal(controller.getState().session.version,3);assert.equal(controller.getState().materials.length,2);assert.equal(controller.getState().pending,null);
});
test('a poll started before an upload cannot roll back current materials or version',async()=>{
  let version=1,hold=false,finish,rows=[];const client=fakeClient({session:async()=>{const value={session:{id:'s',version}};if(hold){hold=false;return new Promise(resolve=>finish=()=>resolve(value));}return value;},materials:async()=>({bindings:structuredClone(rows)}),uploadMaterial:async()=>{version++;rows=[{binding_id:'b',asset_id:'a',kind:'image',slot:'images',version:0,enabled:false,asset:{status:'ready'}}];return {asset_id:'a',status:'ready'};}}),controller=createQuickChatController({client,storage:memory()});await controller.setAccount('qc-review');await controller.open('s');hold=true;const oldPoll=controller.poll().catch(error=>error);await controller.upload({name:'image.png'},'a');finish();await oldPoll;assert.equal(controller.getState().session.version,2);assert.equal(controller.getState().materials[0].asset_id,'a');
});
test('unknown upload recovery reads original receipt and never reuploads bytes',async()=>{
  let uploads=0,lookup;const client=fakeClient({uploadMaterial:async()=>{uploads++;throw Error('lost receipt');},sessionAssets:async(_id,assetId)=>{lookup=assetId;return {assets:[{asset_id:assetId,status:'ready'}]};}}),controller=createQuickChatController({client,storage:memory()});await controller.setAccount('qc-review');await controller.open('s');await assert.rejects(controller.upload({name:'image.png'},'original-asset'));await controller.recover();assert.equal(uploads,1);assert.equal(lookup,'original-asset');assert.equal(controller.getState().pending,null);
});
test('active submission status refreshes when timeline sequence has not changed',async()=>{
  let status='running',timelineCalls=0;const client=fakeClient({timeline:async()=>({events:timelineCalls++===0?[{id:'e',seq:1,type:'submission.confirmed',record:{id:'submission',revision_id:'r'}}]:[],after_cursor:'cursor'}),revision:async()=>({id:'r',card_id:'c'}),card:async()=>({id:'c',current_revision_id:'r'}),submission:async()=>({id:'submission',revision_id:'r',items:[{id:'i',status}],status})}),controller=createQuickChatController({client,storage:memory()});await controller.setAccount('qc-review');await controller.open('s');assert.equal(controller.getState().submissions.submission.status,'running');status='succeeded';await controller.poll();assert.equal(controller.getState().submissions.submission.status,'succeeded');
});
test('pending assistant reads its original run when no new timeline event arrives',async()=>{
  let reads=0;const client=fakeClient({timeline:async()=>({events:[{id:'e',seq:1,type:'turn.created',record:{id:'t',seq:1,text:'离线验收',status:'running'}}],after_cursor:'cursor'}),getTurn:async()=>{reads++;return {id:'t',seq:1,text:'离线验收',status:'completed',reply:'隔离 HTTP 测试收据'};}}),controller=createQuickChatController({client,storage:memory()});await controller.setAccount('qc-review');await controller.open('s');assert.equal(reads,1);assert.equal(mergeTimelineTurns(controller.getState().timeline)[0].status,'completed');
});
test('unbound unselected inventory is not implicitly bound by an unrelated material edit',async()=>{
  const rows=[{binding_id:'a',asset_id:'a',version:1,kind:'image',slot:'images',enabled:true,asset:{status:'ready'}},{binding_id:'long',asset_id:'long',version:0,kind:'video',slot:'videos',enabled:false,asset:{status:'ready',metadata:{duration:16}}}],client=fakeClient({materials:async()=>({bindings:rows}),saveMaterials:async(_id,body)=>{assert.deepEqual(body.bindings.map(item=>item.asset_id),['a']);return {};}}),controller=createQuickChatController({client,storage:memory()});await controller.setAccount('supervan');await controller.open('s');await controller.saveMaterials(rows.map(bindingPayload));
});
test('a video without an audio track cannot request nonexistent original audio in a new card',()=>{
  const inputs=bindingsToInputs([{asset_id:'silent',kind:'video',slot:'videos',enabled:true,include_audio:true,asset:{metadata:{has_audio:false}}}],{mode:'ref'});assert.equal(inputs.videos[0].include_audio,false);
});
test('technical derivative inventory hides only unbound selections, never uploads, imported results or explicit bindings',()=>{
  const internal={version:0,enabled:false,asset:{parent_id:'original-audio',selection:{start:1,end:5},client_asset_id:null}};
  const uploaded={version:0,enabled:false,asset:{client_asset_id:'user-upload',parent_id:null,selection:null,file_name:'trimmed.wav'}};
  const imported={version:0,enabled:false,asset:{client_asset_id:'result-import-stable-id',parent_id:null,selection:null}};
  const bound={...internal,version:1,enabled:true},catalog=[internal,uploaded,imported,bound],original=structuredClone(catalog);
  assert.equal(isInternalDerivedInventory(internal),true);
  assert.equal(isInternalDerivedInventory({...internal,version:1}),false);
  assert.equal(isInternalDerivedInventory({...internal,enabled:true}),false);
  assert.equal(isInternalDerivedInventory(uploaded),false);
  assert.equal(isInternalDerivedInventory(imported),false);
  assert.deepEqual(catalog.filter(binding=>!isInternalDerivedInventory(binding)),[uploaded,imported,bound]);
  assert.deepEqual(catalog,original);
});
test('canonical revision deep links read that immutable resource even outside the current timeline page',async()=>{
  const reads=[],client=fakeClient({revision:async(_id,id)=>{reads.push(id);return {id,card_id:'c',version:id==='old'?1:2};},card:async()=>({id:'c',current_revision_id:'head',revisions:[{id:'old'},{id:'head'}]})}),controller=createQuickChatController({client,storage:memory()});await controller.setAccount('supervan');await controller.open('s',{revisionId:'old'});assert.ok(reads.includes('old'));assert.equal(controller.getState().revisions.old.version,1);assert.equal(controller.getState().cards.c.current_revision_id,'head');assert.equal(controller.getState().pending,null);
});
test('a failed card or current revision read recovers a full direct-card event on an empty forward poll without writes',async()=>{
  for(const failedResource of ['card','revision']){
  let cardReads=0,revisionReads=0,writes=0;const timelineReads=[];
  const card={id:'card',version:1,current_revision_id:'revision',revisions:[{id:'revision'}]},revision={id:'revision',card_id:'card',version:1,input_hash:'original-hash',deployment_profile_id:'selected-ref-profile',recipe_id:'h3-base-ref2va-v1',prompt:'Saved original prompt',controls:{duration:4,steps:20,seed:'42',sampler_name:'res_multistep'},inputs:{images:[{asset_id:'image',purpose:'reference'}],first_frame:{asset_id:'image'}},copies:1};
  const forbidden=async()=>{writes++;throw Error('recovery must remain read-only');},client=fakeClient({
    timeline:async(_id,options)=>{timelineReads.push(options);return {events:options.after_cursor?[]:[{id:'event',seq:1,type:'card.created',record:structuredClone(revision)}],after_cursor:'forward'};},
    card:async()=>{if(++cardReads===1&&failedResource==='card')throw Error('transient card read failed');return structuredClone(card);},revision:async()=>{if(++revisionReads===1&&failedResource==='revision')throw Error('transient revision read failed');return structuredClone(revision);},
    createCard:forbidden,reviseCard:forbidden,preflight:forbidden,submit:forbidden,
  }),controller=createQuickChatController({client,storage:memory()});await controller.setAccount('supervan');
  await assert.rejects(controller.open('s',{cardId:card.id}),new RegExp('transient '+failedResource+' read failed'));
  assert.deepEqual(controller.getState().cards.card,failedResource==='card'?undefined:card);assert.equal(controller.getState().revisions.revision,undefined);assert.equal(controller.getState().forwardCursor,'forward');
  await controller.poll();assert.deepEqual(controller.getState().cards.card,card);assert.deepEqual(controller.getState().revisions.revision,revision);assert.equal(cardReads,2);assert.equal(revisionReads,failedResource==='card'?1:2);assert.equal(timelineReads[1].after_cursor,'forward');
  const reads=revisionReads;await controller.poll();assert.equal(revisionReads,reads,'loaded immutable data does not get fetched on every poll');assert.equal(cardReads,2);assert.equal(writes,0);assert.equal(controller.getState().pending,null);controller.destroy();
  }
});
test('missing-card recovery is bounded to loaded durable references and stops refetching completed cards',async()=>{
  let cardReads=0;const timelineReads=[],events=Array.from({length:30},(_,index)=>({id:'event-'+index,seq:index+1,type:'card.created',record:{id:'revision-'+index,card_id:'card-'+index}}));
  events.push({id:'scope-limited',seq:31,type:'preflight.completed',record:{id:'scoped-preflight',requires_scope:'jobs:write'}});
  const client=fakeClient({timeline:async(_id,options)=>{timelineReads.push(options);return {events:options.after_cursor?[]:events,after_cursor:'forward'};},card:async(_id,id)=>{if(++cardReads===1)throw Error('transient card read failed');return {id,current_revision_id:'revision-'+id.slice(5)};},revision:async(_id,id)=>({id,card_id:'card-'+id.slice(9)})}),controller=createQuickChatController({client,storage:memory()});await controller.setAccount('supervan');
  await assert.rejects(controller.open('s'),/transient card read failed/);await controller.poll();assert.equal(Object.keys(controller.getState().cards).length,20);assert.equal(cardReads,21);
  await controller.poll();assert.equal(Object.keys(controller.getState().cards).length,30);assert.equal(cardReads,31);
  await controller.poll();assert.equal(cardReads,31);assert.ok(timelineReads.slice(1).every(options=>options.after_cursor==='forward'&&!options.before_cursor),'recovery does not read additional history pages');assert.equal(controller.getState().pending,null);controller.destroy();
});
test('a late missing-revision recovery respects account, session and read-generation fences',async()=>{
  for(const boundary of ['account','session','authoring']){
    let reads=0,finish,started;const waiting=new Promise(resolve=>{started=resolve;}),revision={id:'revision',card_id:'card',version:1};
    const client=fakeClient({
      timeline:async(id,options)=>({events:id==='s'&&!options.after_cursor?[{id:'event',seq:1,type:'card.created',record:{card_id:'card'}}]:[],after_cursor:'forward'}),
      card:async()=>({id:'card',current_revision_id:'revision'}),revision:async()=>{if(++reads===1)throw Error('transient');return new Promise(resolve=>{finish=resolve;started();});},
      patchSession:async()=>({session:{id:'s',version:2,title:'Newer authored title'}}),
    }),controller=createQuickChatController({client,storage:memory()});await controller.setAccount('supervan');await assert.rejects(controller.open('s'),/transient/);
    const oldPoll=assert.rejects(controller.poll(),/工作账户、会话或内容已经改变/);await waiting;
    if(boundary==='account')await controller.setAccount('other-owner');else if(boundary==='session')await controller.open('other-session');else await controller.patch({title:'Newer authored title'});
    finish(revision);await oldPoll;assert.equal(controller.getState().revisions.revision,undefined,'an obsolete recovery cannot publish into the current view');
    if(boundary==='authoring')assert.equal(controller.getState().session.version,2);else assert.deepEqual(controller.getState().cards,{});
    assert.equal(controller.getState().pending,null);controller.destroy();
  }
});
test('direct-card send recovers the original atomic turn command and reads its one durable card',async()=>{
  const calls=[];let committed=false;
  const turn={id:'turn',seq:1,text:'一架纸飞机起飞。',assistant_mode:'none',status:'recorded',card_id:'card'},client=fakeClient({
    session:async()=>({session:{id:'s',version:committed?2:1}}),
    turn:async(_id,body,key)=>{calls.push({body:structuredClone(body),key});committed=true;if(calls.length===1)throw Error('lost response');return turn;},
    timeline:async()=>({events:committed?[{id:'event',seq:1,type:'turn.created',record:turn}]:[],after_cursor:'cursor'}),
    card:async()=>({id:'card',turn_id:'turn',current_revision_id:'revision'}),
    revision:async()=>({id:'revision',card_id:'card',prompt:turn.text,copies:2}),
  }),controller=createQuickChatController({client,storage:memory()});
  await controller.setAccount('supervan');await controller.open('s');
  await assert.rejects(controller.turn(turn.text,'gemini-3.8-flash','none',{createCard:true}));
  assert.equal(controller.getState().pending.body.create_card,true);
  await controller.recover();
  assert.equal(calls.length,2);assert.deepEqual(calls[0],calls[1]);
  assert.equal(calls[1].body.expected_version,1);assert.equal(controller.getState().session.version,2);
  assert.deepEqual(Object.keys(controller.getState().cards),['card']);
  assert.equal(controller.getState().revisions.revision.prompt,turn.text);
  assert.equal(mergeTimelineTurns(controller.getState().timeline).length,1);
  assert.equal(controller.getState().pending,null);
});
test('direct-card opt-in never silently changes assistant or discussion requests',async()=>{
  const calls=[],controller=createQuickChatController({client:fakeClient({turn:async(_id,body)=>{calls.push(body);return {id:'turn'};}}),storage:memory()});
  await controller.setAccount('supervan');await controller.open('s');
  await assert.rejects(controller.turn('讨论这个镜头。','gemini-3.8-flash','discuss',{createCard:true}));
  assert.equal(calls.length,0);assert.equal(controller.getState().pending,null);
  await controller.turn('讨论这个镜头。','gemini-3.8-flash','discuss');
  assert.equal(calls[0].assistant_mode,'discuss');assert.equal('create_card' in calls[0],false);
});


test('same-mode material edit persists the complete profile/settings before binding with the new version',async()=>{
  for(const changed of [{deployment_profile_id:'new-profile'},{deployment_profile_id:null},{controls:{steps:50}}]){
    let session={id:'s',version:4,next_settings:{recipe_id:'fl',deployment_profile_id:'old-profile',controls:{steps:20},copies:1}},rows=[{binding_id:'binding',asset_id:'image',version:1,kind:'image',slot:'first_frame',enabled:true,asset:{status:'ready'}}];
    const calls=[],client=fakeClient({session:async()=>({session:structuredClone(session)}),materials:async()=>({bindings:structuredClone(rows)}),patchSession:async(_id,body)=>{assert.equal(body.expected_version,4);calls.push('settings');session={...session,version:5,next_settings:structuredClone(body.next_settings)};return {session:structuredClone(session)};},saveMaterials:async(_id,body)=>{calls.push('materials');assert.equal(body.expected_version,5);assert.equal(body.bindings[0].version,1);rows=body.bindings.map(item=>({...item,version:2}));session={...session,version:6};return {bindings:rows};}}),controller=createQuickChatController({client,storage:memory()});
    await controller.setAccount('qc-review');await controller.open('s');const original=controller.getState(),next={...original.session.next_settings,...changed};
    await saveMaterialSelection(controller,{original,next,proposed:original.materials.map(binding=>({...binding,enabled:false}))});
    assert.deepEqual(calls,['settings','materials']);assert.deepEqual(controller.getState().session.next_settings,next);assert.equal(controller.getState().materials[0].enabled,false);
    await controller.open('s');assert.deepEqual(controller.getState().session.next_settings,next);assert.equal(controller.getState().pending,null);
  }
});

test('material selection refuses stale session versions before applying the pending profile',async()=>{
  let version=1,calls=0;const client=fakeClient({session:async()=>({session:{id:'s',version,next_settings:{recipe_id:'fl'}}}),patchSession:async()=>{calls++;},saveMaterials:async()=>{calls++;}}),controller=createQuickChatController({client,storage:memory()});
  await controller.setAccount('qc-review');await controller.open('s');const original=controller.getState();version=2;await controller.poll();
  await assert.rejects(saveMaterialSelection(controller,{original,next:{recipe_id:'fl',deployment_profile_id:'new'},proposed:[]}));assert.equal(calls,0);
});

test('profile save failure or account switch during PATCH cannot continue material writes',async()=>{
  for(const scenario of ['conflict','account-change']){
    let finish,materialWrites=0;const client=fakeClient({session:async()=>({session:{id:'s',version:1,next_settings:{recipe_id:'fl'}}}),patchSession:async()=>{if(scenario==='conflict')throw Object.assign(Error('version conflict'),{status:409});return new Promise(resolve=>{finish=resolve;});},saveMaterials:async()=>{materialWrites++;}}),controller=createQuickChatController({client,storage:memory()});
    await controller.setAccount('qc-review');await controller.open('s');const original=controller.getState(),saving=saveMaterialSelection(controller,{original,next:{recipe_id:'fl',deployment_profile_id:'new'},proposed:[]});
    const rejected=assert.rejects(saving);if(scenario==='account-change'){await controller.setAccount('other-account');finish({session:{id:'s',version:2,next_settings:{recipe_id:'fl',deployment_profile_id:'new'}}});}await rejected;
    assert.equal(materialWrites,0);if(scenario==='account-change')assert.equal(controller.getState().session,null);
  }
});

test('fresh draft uses only the enabled explicit server profile and its exact defaults',()=>{
  const controls={duration:{type:'integer',minimum:5,maximum:5,default:5},resolution:{type:'string',enum:['480P','768P'],default:'480P'},steps:{type:'integer',enum:[20,50],default:20},sampler_name:{type:'string',enum:['native'],default:'native'}};
  const profile={id:'cheap-tested',generation_support:{fl:{configured:true,enabled:true,controls}}},capabilities={recipes:[{id:'fl',mode:'fl',controls:{...controls,sampler_name:{type:'string',default:'generic'},negative_prompt:{type:'string',default:'generic-negative'}}}],deployment_profiles:[profile],default_deployment_profile_id:'cheap-tested'};
  const expected={recipe_id:'fl',deployment_profile_id:'cheap-tested',controls:{duration:5,resolution:'480P',steps:20,sampler_name:'native'},copies:1},schema={default_next_settings:structuredClone(expected)};
  const actual=nextSettingsFor(capabilities,schema);assert.deepEqual(actual,expected);assert.equal('negative_prompt' in actual.controls,false);assert.equal(actual.controls.resolution,'480P');assert.notEqual(actual,schema.default_next_settings);assert.deepEqual(schema.default_next_settings,expected);
  for(const change of [{default_deployment_profile_id:null},{default_deployment_profile_id:'other'},{deployment_profiles:[]},{deployment_profiles:[{...profile,generation_support:{fl:{...profile.generation_support.fl,configured:false}}}]},{deployment_profiles:[{...profile,generation_support:{fl:{...profile.generation_support.fl,enabled:false}}}]}])assert.equal(nextSettingsFor({...capabilities,...change},schema).deployment_profile_id,undefined);
  const invalid=structuredClone(schema);invalid.default_next_settings.controls.steps=51;assert.equal(nextSettingsFor(capabilities,invalid).deployment_profile_id,undefined);
  const undeclared=structuredClone(schema);undeclared.default_next_settings.controls.negative_prompt='generic-negative';assert.equal(nextSettingsFor(capabilities,undeclared).deployment_profile_id,undefined);
});

test('service default changes do not replace existing session or explicitly authored draft settings',()=>{
  const cap={...capability,default_deployment_profile_id:'new-profile'},schema={default_next_settings:{recipe_id:'fl',deployment_profile_id:'new-profile',controls:{steps:20,resolution:'480P'},copies:1}};
  for(const id of [null,'bf16','retired-profile']){const session={recipe_id:'fl',deployment_profile_id:id,controls:{steps:50,resolution:'768P'},copies:2};assert.equal(nextSettingsFor(cap,schema,{session}),session);const draft={...session,controls:{steps:30}};assert.equal(nextSettingsFor(cap,schema,{session,draft}),draft);}
  const legacy={recipe_id:'fl',controls:{resolution:'480P',duration:5},copies:1};assert.deepEqual(nextSettingsFor(cap,{default_next_settings:legacy}),legacy);
});
