import test from 'node:test';
import assert from 'node:assert/strict';
import {createQuickChatClient} from './quick-chat-client.js';
import {createQuickChatController} from './quick-chat-controller.js';
import {checkedHistoryPreview,checkedHistoryResult,historySelectable,purgeCountdown} from './creation-history-model.js';
import {digest} from './cloud-model.js';
import {saveDraft,readDraft} from './quick-chat-draft.js';
const memory=()=>{const data=new Map();return {getItem:key=>data.get(key)||null,setItem:(key,value)=>data.set(key,value),removeItem:key=>data.delete(key)};};
const row=(id='s',extra={})=>({id,title:'原创作',version:3,trash_state:'active',trashable:true,is_test:false,review_hold:false,...extra});
const removed={sessions:1,turns:2,cards:1,assets:1,artifacts:2,objects:3,bytes:500},kept={assets:1,artifacts:1,objects:2,bytes:300};
function preview(body,extra={}){return {preview_id:'preview-original',action:body.action,expires_at:Date.now()/1000+120,items:body.sessions.map(item=>({...item,eligible:true,blocked_reason:null})),removed:{...removed},kept:{...kept},warnings:[{kind:'story_reference',id:'a',reason:'chosen_reference',project_id:'owned-project',shot_id:'shot',label:'已选镜头'}],ledger_retained:true,review_hold_release_required:false,...extra};}
function receipt(action='trash',items=[{id:'s'}],extra={}){return {action_id:'original-action',action,state:'completed',items:items.map(item=>({id:item.id,state:'completed',trash_state:action==='trash'?'trashed':action==='restore'?'active':'purged',blocked_reason:null})),removed:{...removed},kept:{...kept},ledger_retained:true,...extra};}
function fake(overrides={}){let current=row();return {setAccount(){},reset(){},schema:async()=>({models:[]}),capabilities:async()=>({recipes:[]}),sessions:async({view}={})=>({sessions:view==='trash'?[row('t',{trash_state:'trashed'})]:[current],as_of:100,next_cursor:null}),session:async id=>({session:{...current,id}}),materials:async()=>({bindings:[]}),timeline:async()=>({events:[]}),previewHistory:async body=>preview(body),actionHistory:async()=>receipt(),patchSession:async(id,body)=>{current={...current,...body,id,version:current.version+1};return {session:current};},...overrides};}
async function setup(overrides={},storage=memory()){const client=fake(overrides),controller=createQuickChatController({client,storage});await controller.setAccount('owner');return {client,controller,storage};}
async function selected(controller,action='trash'){if(action!=='trash')await controller.historyView('trash');controller.selectHistory([action==='trash'?'s':'t']);return controller.previewHistory(action);}
test('history client keeps same-origin owner headers, filters and original confirmation key',async()=>{
  const calls=[],client=createQuickChatClient({fetcher:async(path,options)=>{calls.push({path,options});return new Response('{}',{headers:{'X-Authenticated-Account':'owner'}});}});client.setAccount('owner');
  await client.sessions({view:'trash',hideTests:false,cursor:'page'});await client.previewHistory({action:'purge',sessions:[{id:'s',expected_version:3}]});await client.actionHistory({preview_id:'p',confirmed:true},'original-key');await client.historyAction('action/one');
  const params=new URLSearchParams(calls[0].path.split('?')[1]);assert.equal(params.get('view'),'trash');assert.equal(params.get('hide_tests'),'false');assert.equal(params.get('cursor'),'page');assert.equal(calls[2].path,'/v1/quick-chat/sessions/history/actions');assert.equal(calls[2].options.headers['Idempotency-Key'],'original-key');assert.equal(calls[2].options.headers['X-Expected-Account'],'owner');assert.equal(calls[2].options.credentials,'same-origin');assert.equal(calls[3].path,'/v1/quick-chat/sessions/history/actions/action%2Fone');assert.equal(calls[3].options.method,'GET');
});
test('preview binds exact original versions and refuses malformed, duplicate, expired or ledger-changing receipts',()=>{
  const body={action:'trash',sessions:[{id:'s',expected_version:3}]},good=preview(body);
  for(const patch of [{action:'purge'},{ledger_retained:false},{expires_at:0},{items:[{id:'s',expected_version:4,eligible:true}]},{items:[...good.items,...good.items]},{removed:{...removed,bytes:-1}},{items:[{id:'other',expected_version:3,eligible:true}]}])assert.throws(()=>checkedHistoryPreview({...good,...patch},body));
  assert.equal(checkedHistoryPreview(good,body).kept.bytes,300);assert.equal(good.warnings[0].label,'已选镜头');
});
test('selection uses explicit server eligibility; title heuristics and purged/purging rows confer no authority',()=>{
  assert.equal(historySelectable(row('s',{title:'test acceptance',trashable:false}),'active'),false);assert.equal(historySelectable(row('s',{trashable:undefined}),'active'),false);assert.equal(historySelectable(row('s',{trash_state:'purged'}),'trash'),false);assert.equal(historySelectable(row('s',{trash_state:'purging'}),'trash'),false);assert.equal(historySelectable(row('s',{trash_state:'trashed',review_hold:true}),'trash'),true);
});
test('five-day display uses server as-of and never promises deletion without consent or through review hold',()=>{
  const r=row('s',{trash_state:'trashed',auto_purge_enabled:true,purge_at:100+5*86400});assert.equal(purgeCountdown(r,100),'5 天后自动清理');assert.match(purgeCountdown({...r,review_hold:true},100),/不自动删除/);assert.match(purgeCountdown({...r,auto_purge_enabled:false,purge_blocked_reason:'cleanup_scope_required'},100),/不自动删除/);assert.match(purgeCountdown(r,r.purge_at+1),/服务端结果/);assert.match(purgeCountdown(r,null),/待核对/);
});
test('only confirmed receipt closes a trashed current session; latest browser draft survives',async()=>{
  let finish,archived=false,calls=0;const {controller,storage}=await setup({sessions:async()=>({sessions:archived?[]:[row()]}),actionHistory:async()=>{calls++;return new Promise(resolve=>finish=()=>{archived=true;resolve(receipt());});}});await controller.open('s');saveDraft('owner','s','unsent original',{storage,edit:true});await selected(controller);assert.equal(controller.getState().session.id,'s');const sending=controller.confirmHistory();saveDraft('owner','s','newer unsent',{storage,edit:true});assert.equal(controller.getState().session.id,'s');finish();await sending;assert.equal(controller.getState().session,null);assert.equal(calls,1);assert.equal(readDraft('owner','s',storage),'newer unsent');assert.equal(controller.getState().pending,null);
});
test('lost confirmation reload recovers exact original key/body and does not establish a second preview',async()=>{
  const calls=[],storage=memory();let previews=0;const client=fake({previewHistory:async body=>{previews++;return preview(body);},actionHistory:async(body,key)=>{calls.push({body:structuredClone(body),key});if(calls.length===1)throw Error('unknown transport');return receipt();}}),first=createQuickChatController({client,storage});await first.setAccount('owner');await selected(first);await assert.rejects(first.confirmHistory());const original=structuredClone(first.getState().pending);await assert.rejects(first.confirmHistory());assert.equal(calls.length,1);
  const restored=createQuickChatController({client,storage});await restored.setAccount('owner');await restored.recover();assert.deepEqual(calls[0],calls[1]);assert.equal(calls[1].body.preview_id,original.body.preview_id);assert.equal(previews,1);assert.equal(restored.getState().pending,null);
});
test('pending purge stays journaled and GETs only original action until closed result',async()=>{
  let posts=0,gets=0;const {controller}=await setup({actionHistory:async()=>{posts++;return receipt('purge',[{id:'t'}],{state:'pending',items:[{id:'t',state:'pending',trash_state:'purging'}]});},historyAction:async id=>{assert.equal(id,'original-action');gets++;return gets===1?receipt('purge',[{id:'t'}],{state:'pending',items:[{id:'t',state:'pending',trash_state:'purging'}]}):receipt('purge',[{id:'t'}]);}});await selected(controller,'purge');await controller.confirmHistory();assert.equal(controller.getState().historyReceipt.state,'pending');assert.equal(controller.getState().pending.actionId,'original-action');await controller.recover();assert.ok(controller.getState().pending);await controller.recover();assert.equal(posts,1);assert.equal(gets,2);assert.equal(controller.getState().pending,null);
});
test('list authorization failure after accepted pending purge does not erase its action journal',async()=>{
  let accepted=false;const {controller}=await setup({sessions:async({view}={})=>{if(accepted)throw Object.assign(Error('read unavailable'),{status:403});return {sessions:[row(view==='trash'?'t':'s',{trash_state:view==='trash'?'trashed':'active'})]};},actionHistory:async()=>{accepted=true;return receipt('purge',[{id:'t'}],{state:'pending',items:[{id:'t',state:'pending',trash_state:'purging'}]});}});await selected(controller,'purge');await assert.rejects(controller.confirmHistory());assert.equal(controller.getState().pending.actionId,'original-action');assert.equal(controller.getState().historyReceipt.state,'pending');
});
test('old account preview response never enters next account and old pending confirmation stays isolated',async()=>{
  let finish;const {controller}=await setup({previewHistory:body=>new Promise(resolve=>finish=()=>resolve(preview(body)))});controller.selectHistory(['s']);const pending=controller.previewHistory('trash');await controller.setAccount('next');finish();await assert.rejects(pending);assert.equal(controller.getState().account,'next');assert.equal(controller.getState().historyPreview,null);assert.deepEqual(controller.getState().historySelection,[]);
});
test('filter, page and selection changes invalidate confirmation before any action POST',async()=>{
  let posts=0;const {controller}=await setup({actionHistory:async()=>{posts++;return receipt();}});await selected(controller);controller.selectHistory([]);await assert.rejects(controller.confirmHistory());await selected(controller);await controller.hideTests(true);await assert.rejects(controller.confirmHistory());await selected(controller);await controller.list({more:true});await assert.rejects(controller.confirmHistory());assert.equal(posts,0);
});
test('server-blocked batch preview remains all-or-none without dropping a blocked selected record',async()=>{
  let posts=0;const {controller}=await setup({sessions:async()=>({sessions:[row(),row('second')]}),previewHistory:async body=>preview(body,{items:body.sessions.map((item,index)=>({...item,eligible:index===0,blocked_reason:index?'active_work':null}))}),actionHistory:async()=>{posts++;return receipt();}});controller.selectHistory(['s','second']);await controller.previewHistory('trash');await assert.rejects(controller.confirmHistory());assert.equal(posts,0);assert.deepEqual(controller.getState().historySelection,['s','second']);
});
test('empty trash confirms all server-captured rows including records outside the loaded page',async()=>{
  let body;const {controller}=await setup({previewHistory:async request=>{assert.deepEqual(request,{action:'empty_trash',sessions:[]});return preview(request,{items:[{id:'t',expected_version:3,eligible:true},{id:'not-loaded',expected_version:7,eligible:true}]});},actionHistory:async value=>{body=value;return receipt('empty_trash',[{id:'t'},{id:'not-loaded'}]);}});await controller.historyView('trash');await controller.previewHistory('empty_trash');await controller.confirmHistory();assert.deepEqual(body,{preview_id:'preview-original',confirmed:true});assert.equal(controller.getState().historyReceipt.items.length,2);
});
test('malformed success does not clear unknown action or announce deleted content',async()=>{
  const {controller}=await setup({actionHistory:async()=>receipt('trash',[{id:'other'}])});await selected(controller);await assert.rejects(controller.confirmHistory());assert.ok(controller.getState().pending);assert.equal(controller.getState().historyReceipt,null);
});
test('known conflict clears only pending confirmation and preserves selected records and draft',async()=>{
  const {controller,storage}=await setup({actionHistory:async body=>{throw Object.assign(Error('version conflict'),{status:409,detail:{resource:'quick_chat_history',action:'confirm',code:'history_preview_changed',no_mutation_committed:true,request_hash:await digest(body)}});}});saveDraft('owner','s','local',{storage,edit:true});await selected(controller);await assert.rejects(controller.confirmHistory());assert.equal(controller.getState().pending,null);assert.equal(controller.getState().historyPreview,null);assert.deepEqual(controller.getState().historySelection,['s']);assert.equal(readDraft('owner','s',storage),'local');
});
test('generic 429/5xx remain unknown and no absent operation journal permits a mutation',async()=>{
  for(const status of [429,503]){const {controller}=await setup({actionHistory:async()=>{throw Object.assign(Error('unknown'),{status});}});await selected(controller);await assert.rejects(controller.confirmHistory());assert.ok(controller.getState().pending);}
  let posts=0;const {controller}=await setup({actionHistory:async()=>{posts++;return receipt();}},{getItem:()=>null,setItem(){throw Error('quota');},removeItem(){}});await selected(controller);await assert.rejects(controller.confirmHistory());assert.equal(posts,0);
});
test('expired confirmation cannot reuse the preview even if original selection is unchanged',async()=>{
  let posts=0;const {controller}=await setup({actionHistory:async()=>{posts++;return receipt();}});await selected(controller);const originalNow=Date.now;try{Date.now=()=>originalNow()+130000;await assert.rejects(controller.confirmHistory());}finally{Date.now=originalNow;}assert.equal(posts,0);
});
test('archive unknown recovers original metadata PATCH even after session is already trashed, without opening it',async()=>{
  const calls=[],storage=memory();let archived=false;const client=fake({sessions:async()=>({sessions:archived?[]:[row()]}),session:async()=>({session:row('s',{trash_state:archived?'trashed':'active'})}),patchSession:async(id,body,key)=>{calls.push({id,body,key});archived=true;if(calls.length===1)throw Error('response lost');return {session:row('s',{version:4,is_test:true,trash_state:'trashed'})};}}),controller=createQuickChatController({client,storage});await controller.setAccount('owner');await controller.open('s');saveDraft('owner','s','newest browser draft',{storage,edit:true});await assert.rejects(controller.patch({is_test:true,test_accepted:true}));await assert.rejects(controller.poll());assert.equal(controller.getState().session,null);await controller.recover();assert.deepEqual(calls[0],calls[1]);assert.equal(controller.getState().pending,null);assert.equal(readDraft('owner','s',storage),'newest browser draft');
});
test('test-accepted acknowledgement may defer safe archive, and test filtering is sent explicitly',async()=>{
  const reads=[],{controller}=await setup({sessions:async options=>{reads.push(options);return {sessions:[row()]};},patchSession:async(id,body)=>({session:row(id,{version:body.expected_version+1,is_test:true,trash_state:'active',archive_pending:true})})});await controller.open('s');await controller.patch({is_test:true,test_accepted:true});assert.equal(controller.getState().session.id,'s');assert.equal(controller.getState().session.trash_state,'active');assert.equal(controller.getState().session.archive_pending,true);assert.equal(controller.getState().pending,null);await controller.hideTests(true);assert.equal(reads.at(-1).hideTests,true);
});
test('account change during pending-action GET cannot erase the original owner journal',async()=>{
  let finish;const storage=memory(),{controller}=await setup({actionHistory:async()=>receipt('purge',[{id:'t'}],{state:'pending',items:[{id:'t',state:'pending',trash_state:'purging'}]}),historyAction:()=>new Promise(resolve=>finish=()=>resolve(receipt('purge',[{id:'t'}])))},storage);await selected(controller,'purge');await controller.confirmHistory();const checking=controller.recover();await controller.setAccount('next');finish();await assert.rejects(checking);assert.equal(controller.getState().pending,null);assert.equal(controller.getState().historyReceipt,null);assert.ok(JSON.parse(storage.getItem('yingxu:quick-chat:operation:owner')).actionId);
});
test('restore cannot claim a purged result; blocked purge is reported without claiming all deleted',()=>{
  const operation={action:'restore',items:[{id:'s'}]};assert.throws(()=>checkedHistoryResult(receipt('restore',[{id:'s'}],{items:[{id:'s',state:'completed',trash_state:'purged'}]}),operation));assert.throws(()=>checkedHistoryResult(receipt('restore',[{id:'s'}],{ledger_retained:false}),operation));const value=checkedHistoryResult(receipt('purge',[{id:'s'}],{items:[{id:'s',state:'blocked',trash_state:'trashed',blocked_reason:'shared_reference'}]}),{action:'purge',items:[{id:'s'}]});assert.equal(value.items[0].trash_state,'trashed');
});

test('bare authentication/conflict errors preserve original history confirmation; expired original closes only on exact proof',async()=>{
 for(const status of [401,403,404,409]){const {controller}=await setup({actionHistory:async()=>{throw Object.assign(Error('unknown'),{status});}});await selected(controller);await assert.rejects(controller.confirmHistory());assert.ok(controller.getState().pending);}
 const calls=[];let first=true,wrong=true;const {controller}=await setup({actionHistory:async(body,key)=>{calls.push({body:structuredClone(body),key});if(first){first=false;throw Error('lost');}throw Object.assign(Error('expired'),{status:409,detail:{resource:'quick_chat_history',action:'confirm',code:'history_preview_expired',no_mutation_committed:true,request_hash:wrong?'foreign':await digest(body)}});}});
 await selected(controller);await assert.rejects(controller.confirmHistory());await assert.rejects(controller.recover());assert.ok(controller.getState().pending);wrong=false;await assert.rejects(controller.recover());assert.equal(controller.getState().pending,null);assert.deepEqual(calls[0],calls[1]);assert.deepEqual(calls[0],calls[2]);
});

test('row metadata renames the captured row without opening it or changing the current draft',async()=>{
 const rows=[row('s'),row('other',{title:'Other creation',version:7})],calls=[];
 const {controller,storage}=await setup({sessions:async()=>({sessions:structuredClone(rows)}),session:async id=>({session:structuredClone(rows.find(r=>r.id===id))}),patchSession:async(id,body,key)=>{calls.push({id,body:structuredClone(body),key});const i=rows.findIndex(r=>r.id===id);rows[i]={...rows[i],...body,version:rows[i].version+1};return {session:structuredClone(rows[i])};}});
 await controller.open('s');saveDraft('owner','s','A newer unsent draft',{storage,edit:true});const current=structuredClone(controller.getState().session);
 await controller.patchRow({account:'owner',id:'other',version:7},{title:'Renamed other'});
 assert.equal(calls.length,1);assert.equal(calls[0].id,'other');assert.deepEqual(calls[0].body,{expected_version:7,title:'Renamed other'});
 assert.deepEqual(controller.getState().session,current);assert.equal(readDraft('owner','s',storage),'A newer unsent draft');assert.equal(controller.getState().sessions.find(r=>r.id==='other').title,'Renamed other');assert.equal(controller.getState().pending,null);
});

test('row metadata rejects foreign/stale/ended targets and unsupported fields before creating a pending command',async()=>{
 let writes=0;const {controller}=await setup({sessions:async()=>({sessions:[row(),row('ended',{trash_state:'trashed'})]}),patchSession:async()=>{writes++;return {};}});
 for(const target of [{account:'foreign',id:'s',version:3},{account:'owner',id:'s',version:2},{account:'owner',id:'missing',version:3},{account:'owner',id:'ended',version:3}])await assert.rejects(controller.patchRow(target,{title:'New title'}));
 for(const patch of [{next_settings:{}},{test_accepted:true},{review_hold:true},{title:''},{is_test:'true'},{}])await assert.rejects(controller.patchRow({account:'owner',id:'s',version:3},patch));
 assert.equal(writes,0);assert.equal(controller.getState().pending,null);
});

test('unknown row rename recovers exactly its original body/key while another session stays open',async()=>{
 const rows=[row(),row('other',{version:7,title:'Original other'})],calls=[],storage=memory();let first=true;
 const client=fake({sessions:async()=>({sessions:structuredClone(rows)}),session:async id=>({session:structuredClone(rows.find(r=>r.id===id))}),patchSession:async(id,body,key)=>{calls.push({id,body:structuredClone(body),key});const i=rows.findIndex(r=>r.id===id);rows[i]={...rows[i],title:body.title,version:8};if(first){first=false;throw Error('lost acknowledgement');}return {session:structuredClone(rows[i])};}});
 let controller=createQuickChatController({client,storage});await controller.setAccount('owner');await controller.open('s');saveDraft('owner','s','Unsent',{storage,edit:true});
 await assert.rejects(controller.patchRow({account:'owner',id:'other',version:7},{title:'Saved other'}));const original=structuredClone(controller.getState().pending);
 assert.equal(original.sessionId,null);assert.deepEqual(original.metadataTarget,{account:'owner',id:'other',version:7});assert.equal(controller.getState().session.id,'s');
 controller=createQuickChatController({client,storage});await controller.setAccount('owner');await controller.open('s');await controller.recover();
 assert.deepEqual(calls[0],calls[1]);assert.equal(controller.getState().pending,null);assert.equal(controller.getState().session.id,'s');assert.equal(readDraft('owner','s',storage),'Unsent');
});

test('late row flag acknowledgement cannot clear another account or its original owner journal',async()=>{
 let finish;const {controller,storage}=await setup({patchSession:()=>new Promise(resolve=>finish=()=>resolve({session:row('s',{version:4,is_test:true})}))});
 const updating=controller.patchRow({account:'owner',id:'s',version:3},{is_test:true});const original=structuredClone(controller.getState().pending);await controller.setAccount('next');finish();await assert.rejects(updating);
 assert.equal(controller.getState().account,'next');assert.equal(controller.getState().pending,null);assert.deepEqual(JSON.parse(storage.getItem('yingxu:quick-chat:operation:owner')),original);
});

test('malformed row acknowledgements preserve the exact pending metadata request',async()=>{
 for(const returned of [row('wrong',{title:'New',version:4}),row('s',{title:'Different',version:4}),row('s',{title:'New',version:3})]){
  const {controller}=await setup({patchSession:async()=>({session:returned})});await assert.rejects(controller.patchRow({account:'owner',id:'s',version:3},{title:'New'}));assert.deepEqual(controller.getState().pending.body,{expected_version:3,title:'New'});
 }
});

test('fresh session reads update sidebar generating eligibility instead of retaining an old trashable flag',async()=>{
 const {controller}=await setup({session:async()=>({session:row('s',{trashable:false,blocked_reason:'active_work'})})});
 assert.equal(controller.getState().sessions[0].trashable,true);await controller.open('s');
 assert.equal(controller.getState().sessions[0].trashable,false);assert.equal(controller.getState().sessions[0].blocked_reason,'active_work');
});

test('recovering a lost row rename acknowledges its exact key without rolling back newer loaded settings',async()=>{
 let live=row('s',{next_settings:{controls:{duration:5}}}),originalReceipt;const calls=[],opens=[];
 const {controller,storage}=await setup({sessions:async()=>({sessions:[structuredClone(live)]}),session:async id=>{opens.push(id);return {session:structuredClone(live)};},patchSession:async(id,body,key)=>{
  calls.push({id,body:structuredClone(body),key});if(calls.length===1){live={...live,title:body.title,version:4};originalReceipt=structuredClone(live);throw Error('lost acknowledgement');}return {session:structuredClone(originalReceipt)};
 }});
 await controller.open('s');await assert.rejects(controller.patchRow({account:'owner',id:'s',version:3},{title:'Renamed'}));
 const pending=structuredClone(controller.getState().pending);
 // Another client changes generation settings, then this tab reads that v5.
 live={...live,version:5,next_settings:{controls:{duration:10}}};await controller.open('s');
 saveDraft('owner','s','Newer unsent prompt',{storage,edit:true});const loaded=structuredClone(controller.getState().session),openCount=opens.length;
 await controller.recover();
 assert.deepEqual(calls[0],calls[1]);assert.equal(calls.length,2);assert.equal(calls[1].key,pending.key);assert.deepEqual(calls[1].body,pending.body);
 assert.deepEqual(controller.getState().session,loaded);assert.equal(controller.getState().session.version,5);assert.equal(controller.getState().session.next_settings.controls.duration,10);
 assert.equal(opens.length,openCount);assert.equal(controller.getState().pending,null);assert.equal(readDraft('owner','s',storage),'Newer unsent prompt');
});
