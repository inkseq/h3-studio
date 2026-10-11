import test from 'node:test';
import assert from 'node:assert/strict';
import {createOperatorClient} from './operator-client.js';
import {createOperatorController} from './operator-controller.js';
import {allowed,profileSelection,nodeDeadline,nodeRemovalConfirmation,runtimeDuration,initialOperatorSelection,operatorStartPayload,operatorFilterEdit,hasBoundOperatorSelection,previewOperatorSelection,operatorMarketProvider,operatorRecommendationsCurrent,operatorOfferReadiness,operatorInventorySummary,recommendationSelection,operatorHardwareOptions,reasonText} from './operator-model.js';
import {recipeFor,effectiveControlSchema,effectiveLimits,clipLimits,inputProblems} from './quick-chat-model.js';

const memory=()=>{const data=new Map();return {getItem:key=>data.get(key)||null,setItem:(key,value)=>data.set(key,value),removeItem:key=>data.delete(key)};};
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
function fakeClient(overrides={}){let account;return {setAccount(value){account=value;},reset(){},state:async()=>({observed_at:Date.now()/1000,operator:{account,permissions:{view:true,start:true}},nodes:[]}),catalog:async()=>({profiles:[]}),...overrides};}
test('extension preview binds version and duration; uncertain confirmation recovers its exact original intent',async()=>{
  const calls=[],storage=memory();let fail=true;
  const controller=createOperatorController({storage,client:fakeClient({extensionPreview:async(id,body)=>{calls.push({preview:true,id,body});return {preview_id:'e1',node_id:id};},extend:async(id,body,key)=>{calls.push({id,body,key});if(fail)throw Error('lost response');return {operation:{id:'same-extension'}};}})});
  await controller.setAccount('owner');const preview=await controller.extensionPreview({id:'node',version:'original'},1800);
  assert.deepEqual(calls[0],{preview:true,id:'node',body:{expected_version:'original',additional_seconds:1800}});assert.equal(controller.getState().pending,null);
  await assert.rejects(controller.extend(preview));const saved=structuredClone(controller.getState().pending);assert.equal(saved.method,'extend');
  assert.deepEqual(calls[1].body,{preview_id:'e1'});assert.equal(calls[1].id,'node');
  await controller.poll();assert.equal(calls.length,2);fail=false;await controller.recover();assert.deepEqual(calls[2],calls[1]);assert.equal(controller.getState().pending,null);
});
test('extension client preserves cookie identity and same-origin exact endpoint with idempotency only on confirmation',async()=>{
  const calls=[],client=createOperatorClient({fetcher:async(path,options)=>{calls.push({path,options});return new Response('{}',{headers:{'Content-Type':'application/json','X-Authenticated-Account':'owner'}});}});
  client.setAccount('owner');await client.extensionPreview('node/one',{expected_version:'v1',additional_seconds:600});await client.extend('node/one',{preview_id:'e1'},'original-extension-key');
  assert.equal(calls[0].path,'/v1/operator/capacity/nodes/node%2Fone/extension-previews');assert.equal(calls[1].path,'/v1/operator/capacity/nodes/node%2Fone/extensions');
  for(const call of calls){assert.equal(call.options.method,'POST');assert.equal(call.options.credentials,'same-origin');assert.equal(call.options.headers['X-Expected-Account'],'owner');}
  assert.equal(calls[0].options.headers['Idempotency-Key'],undefined);assert.equal(calls[1].options.headers['Idempotency-Key'],'original-extension-key');
  assert.deepEqual(JSON.parse(calls[0].options.body),{expected_version:'v1',additional_seconds:600});assert.deepEqual(JSON.parse(calls[1].options.body),{preview_id:'e1'});
});
test('manual review binds exact UID and original version; uncertain replay retains its single operation',async()=>{
  const storage=memory(),calls=[];let fail=true;
  const client=fakeClient({manualReview:async(nodeId,body,key)=>{calls.push({nodeId,body,key});if(fail)throw Error('connection lost');return {operation:{id:'same-review'}};}});
  const node={id:'original-node',provider_instance_id:'wrk-original',version:'original-version'};
  const controller=createOperatorController({client,storage});await controller.setAccount('owner');
  await assert.rejects(controller.manualReview(node,{account_absent:true,no_continuing_charge:true}));
  const saved=controller.getState().pending;assert.equal(saved.method,'manualReview');
  assert.deepEqual(calls[0].body,{expected_version:'original-version',provider_instance_id:'wrk-original',account_absent:true,no_continuing_charge:true});
  await controller.poll();assert.equal(calls.length,1);fail=false;await controller.recover();
  assert.deepEqual(calls[1],calls[0]);assert.equal(controller.getState().pending,null);
});
test('unbound creation manual review preserves exact null identity across uncertain recovery',async()=>{
  const storage=memory(),calls=[];let fail=true;
  const client=fakeClient({manualReview:async(nodeId,body,key)=>{calls.push({nodeId,body,key});if(fail)throw Error('connection lost');return {operation:{id:'null-review'}};}});
  const node={id:'original-unbound-node',provider_instance_id:null,version:'unbound-version'};
  const controller=createOperatorController({client,storage});await controller.setAccount('owner');
  await assert.rejects(controller.manualReview(node,{account_absent:true,no_continuing_charge:true}));
  assert.deepEqual(calls[0].body,{expected_version:'unbound-version',provider_instance_id:null,account_absent:true,no_continuing_charge:true});
  await controller.poll();assert.equal(calls.length,1);fail=false;await controller.recover();
  assert.deepEqual(calls[1],calls[0]);assert.equal(controller.getState().pending,null);
});
test('manual review projection stops scheduling without labelling provider removal or settled money',()=>{
  const review={state:'manually_reviewed',requested_at:100,last_checked_at:200,next_check_at:null,
    manual_review:{actor:'supervan',observed_at:300},last_observation:{state:'unknown',observed_at:200}};
  const result=nodeRemovalConfirmation({provider:'targon',state:'destroying',removal_confirmation:review});
  assert.equal(result.pending,false);assert.equal(result.reviewed,true);assert.equal(result.next_check_at,null);
  assert.match(result.label,/人工审核/);assert.doesNotMatch(result.label,/已确认删除/);
  assert.match(result.message,/预算预留未结算/);assert.equal(result.manual_review.actor,'supervan');
});
test('manual review client uses same-origin cookie API with attestation body and supplied idempotency',async()=>{
  const requests=[],client=createOperatorClient({fetcher:async(path,options)=>{requests.push({path,options});return new Response('{}',{headers:{'Content-Type':'application/json','X-Authenticated-Account':'owner'}});}});
  client.setAccount('owner');const body={expected_version:'v',provider_instance_id:'wrk-original',account_absent:true,no_continuing_charge:true};
  await client.manualReview('node-original',body,'review-original');
  assert.equal(requests[0].path,'/v1/operator/capacity/nodes/node-original/manual-review');
  assert.equal(requests[0].options.credentials,'same-origin');assert.deepEqual(JSON.parse(requests[0].options.body),body);
  assert.equal(requests[0].options.headers['Idempotency-Key'],'review-original');
});
test('uncertain start is retained across reload and retried only explicitly with original key and preview',async()=>{
  const storage=memory(),calls=[];let fail=true;
  const client=fakeClient({start:async(body,key)=>{calls.push({body,key});if(fail)throw Error('connection lost');return {operation:{id:'original',state:'accepted'}};}});
  const first=createOperatorController({client,storage});await first.setAccount('owner');await assert.rejects(first.start({preview_id:'p1'}));
  const saved=structuredClone(first.getState().pending);assert.ok(saved.key);await first.poll();assert.equal(calls.length,1);
  await assert.rejects(first.start({preview_id:'p2'}));assert.equal(calls.length,1);first.destroy();
  const next=createOperatorController({client,storage});await next.setAccount('owner');assert.deepEqual(next.getState().pending,saved);assert.equal(calls.length,1);
  fail=false;await next.recover();assert.deepEqual(calls[0],calls[1]);assert.equal(next.getState().pending,null);
});
test('old-account start response cannot erase its record or appear under another owner',async()=>{
  const held=deferred(),storage=memory(),client=fakeClient({start:()=>held.promise}),controller=createOperatorController({client,storage});
  await controller.setAccount('a');const call=controller.start({preview_id:'p'});await controller.setAccount('b');held.resolve({operation:{id:'a-op'}});
  await assert.rejects(call);assert.equal(controller.getState().account,'b');assert.equal(controller.getState().pending,null);assert.ok(storage.getItem('sixnine:operator:pending:a'));assert.equal(storage.getItem('sixnine:operator:pending:b'),null);
});
test('late old-account read never unlocks a newer account read or displays its nodes',async()=>{
  const a=deferred(),b=deferred();let account,reads=0;
  const client=fakeClient({setAccount(value){account=value;},state(){reads++;return account==='a'?a.promise:b.promise;}}),controller=createOperatorController({client,storage:memory()});
  const old=controller.setAccount('a'),current=controller.setAccount('b');a.resolve({operator:{account:'a'},nodes:[{id:'private-a'}]});await old;
  assert.equal(controller.getState().loading,true);await controller.refresh();assert.equal(reads,2);assert.equal(controller.getState().snapshot,null);
  b.resolve({operator:{account:'b'},nodes:[]});await current;assert.equal(controller.getState().loading,false);assert.equal(controller.getState().snapshot.operator.account,'b');
});
test('403 clears privileged stale data and cannot masquerade as empty healthy inventory',async()=>{
  let denied=false;const client=fakeClient({state:async()=>{if(denied)throw Object.assign(Error('forbidden'),{status:403});return {operator:{account:'a',permissions:{view:true}},nodes:[{id:'n'}]};}}),controller=createOperatorController({client,storage:memory()});
  await controller.setAccount('a');denied=true;await assert.rejects(controller.refresh());assert.equal(controller.getState().denied,true);assert.equal(controller.getState().snapshot,null);assert.equal(controller.getState().catalog,null);
});
test('storage failure prevents a start request and an explicit rejection releases its old intent',async()=>{
  let calls=0;const client=fakeClient({start:async()=>{calls++;throw Object.assign(Error('conflict'),{status:409});}});
  const blocked=createOperatorController({client,storage:{getItem:()=>null,setItem(){throw Error('blocked storage');}}});await blocked.setAccount('a');await assert.rejects(blocked.start({preview_id:'p'}));assert.equal(calls,0);
  const controller=createOperatorController({client,storage:memory()});await controller.setAccount('a');await assert.rejects(controller.start({preview_id:'p'}));assert.equal(calls,1);assert.equal(controller.getState().pending,null);
});
test('drain/stop bind observed node version; client uses only same-origin cookie endpoint and supplied idempotency',async()=>{
  const requests=[],client=createOperatorClient({fetcher:async(path,options)=>{requests.push({path,options});return new Response(JSON.stringify({operation:{id:'op'}}),{status:202,headers:{'Content-Type':'application/json','X-Authenticated-Account':'owner'}});}});
  client.setAccount('owner');await client.stop('node/one',{expected_version:'original-version'},'original-key');
  assert.equal(requests[0].path,'/v1/operator/capacity/nodes/node%2Fone/stop');assert.equal(requests[0].options.credentials,'same-origin');assert.equal(requests[0].options.headers['Idempotency-Key'],'original-key');assert.deepEqual(JSON.parse(requests[0].options.body),{expected_version:'original-version'});
});
test('stale, absent and unauthorized observations never enable an operator action',()=>{
  const now=Date.now(),snapshot={observed_at:now/1000,operator:{permissions:{start:true,stop:false}}};assert.equal(allowed(snapshot,'start',now),true);assert.equal(allowed(snapshot,'stop',now),false);assert.equal(allowed(snapshot,'start',now+31000),false);assert.equal(allowed(null,'start',now),false);
});
test('inventory and preview preserve explicit FL/REF mode without initiating rental',async()=>{
  const requests=[],client=createOperatorClient({fetcher:async(path,options)=>{requests.push({path,options});return new Response('{}',{headers:{'Content-Type':'application/json','X-Authenticated-Account':'owner'}});}});
  client.setAccount('owner');const selection={runtime_profile_id:'p',mode:'ref',gpu_type:'GPU exact',gpu_count:2,node_count:1,ttl_seconds:3600};await client.offers(selection);await client.preview(selection);
  const query=new URL(requests[0].path,'https://local.invalid').searchParams;assert.equal(query.get('mode'),'ref');assert.equal(query.get('gpu_count'),'2');assert.deepEqual(JSON.parse(requests[1].options.body),selection);assert.equal(requests.some(r=>r.path.endsWith('/starts')),false);
});
test('provider-aware inventory sends full selection with optional filters and never starts a rental',async()=>{
  const requests=[],client=createOperatorClient({fetcher:async(path,options)=>{requests.push({path,options});return new Response('{}',{headers:{'Content-Type':'application/json','X-Authenticated-Account':'owner'}});}});
  client.setAccount('owner');
  const selection={...initialOperatorSelection({max_ttl_seconds:10800}),runtime_profile_id:'pruned',mode:'ref',provider:'targon',gpu_type:'RTX PRO 6000',node_count:2,filters:{min_ram_gib:96,min_disk_gib:128,max_price_per_gpu_hour_microusd:850000}};
  await client.offers(operatorStartPayload(selection));await client.offers(operatorStartPayload(selection,true));await client.preview(operatorStartPayload(selection));
  const query=new URL(requests[0].path,'https://local.invalid').searchParams,customQuery=new URL(requests[1].path,'https://local.invalid').searchParams;
  assert.equal(query.get('provider'),'targon');assert.equal(query.get('node_count'),'2');assert.equal(query.get('ttl_seconds'),'10800');assert.equal(query.get('mode'),'ref');assert.equal(query.has('filters'),false);
  assert.deepEqual(JSON.parse(customQuery.get('filters')),selection.filters);assert.equal(JSON.parse(requests[2].options.body).provider,'targon');assert.equal(requests.some(item=>item.path.endsWith('/starts')),false);
});
test('default Lium and authored provider survive profile edits and authoritative previews',()=>{
  const initial=initialOperatorSelection({max_ttl_seconds:10800});assert.equal(initial.provider,'lium');assert.equal(operatorStartPayload(initial).provider,'lium');
  const selection={...initial,provider:'targon'},next=profileSelection({id:'same-recipe',gpu_models:['RTX 5090'],gpu_count_options:[1]},selection);
  assert.equal(next.provider,'targon');assert.equal(operatorStartPayload(next).provider,'targon');
  const {provider,...legacySelection}=initial;
  assert.equal(previewOperatorSelection(initial,{configuration_id:'lium-bound',selection:legacySelection}).provider,'lium');
  assert.equal(previewOperatorSelection(selection,{configuration_id:'targon-bound',selection:{...next,filters:{}}}).provider,'targon');
});
test('explicit hardware recommendation changes only provider and GPU while keeping recipe, precision, controls and authored limits',()=>{
  const selection={...initialOperatorSelection({max_ttl_seconds:10800}),runtime_profile_id:'pruned-int8',mode:'ref',provider:'lium',gpu_type:'RTX 5090',node_count:2,precision:'int8',controls:{steps:20},filters:{min_ram_gib:96,min_download_mbps:200,max_price_per_gpu_hour_microusd:850000}};
  const row={provider:'targon',gpu_type:'RTX PRO 6000 Blackwell Server Edition',selection:{runtime_profile_id:'different-bf16',mode:'fl',node_count:1,ttl_seconds:120,filters:{}}};
  const next=recommendationSelection(selection,row);
  assert.deepEqual(next,{...selection,provider:row.provider,gpu_type:row.gpu_type});assert.equal(next.filters,selection.filters);assert.equal(next.controls,selection.controls);assert.equal(selection.gpu_type,'RTX 5090');
  assert.deepEqual(operatorHardwareOptions({gpu_models:['RTX 5090']},next).map(item=>item.id),['RTX 5090',row.gpu_type]);assert.equal(recommendationSelection(selection,{provider:'unknown',gpu_type:'GPU'}),null);
});
test('inventory distinguishes known empty stock from failed, missing, expired and future observations',()=>{
  const now=1000000,selection={provider:'lium',gpu_type:'RTX 5090'},market={status:'partial',fresh_seconds:120,observed_at:990,providers:[{provider:'lium',status:'ok',observed_at:990},{provider:'targon',status:'error',observed_at:990}],offers:[]};
  assert.equal(operatorMarketProvider(market,'lium',now).fresh,true);assert.match(operatorInventorySummary({market},selection,now),/没有所选 RTX 5090/);
  assert.equal(operatorMarketProvider(market,'targon',now).fresh,false);assert.match(operatorInventorySummary({market},{...selection,provider:'targon'},now),/不能判断有无库存/);
  for(const observation of [{status:'ok',observed_at:879},{status:'ok',observed_at:1001},{status:'ok'},{status:'stale',observed_at:990},{status:'unconfigured',observed_at:990}]){
    const stale={...market,providers:[{provider:'lium',...observation}]};assert.equal(operatorMarketProvider(stale,'lium',now).fresh,false);assert.match(operatorInventorySummary({market:stale},selection,now),/不能判断有无库存/);
  }
  assert.equal(operatorMarketProvider({...market,status:'unconfirmed'},'lium',now).fresh,true);
  assert.match(operatorInventorySummary({market:{...market,status:'unconfirmed'}},selection,now),/筛选条件尚未确认/);
  assert.match(operatorInventorySummary({status:'available',offers:[]},selection,now),/未返回逐项报价/);
  assert.match(reasonText('operator_provider_start_unqualified'),/尚未验收/);
});
test('fresh provider scans with unconfirmed matching specifications do not claim staleness or missing stock',()=>{
  const now=1000000,selection={provider:'lium',gpu_type:'RTX 5090'},market={status:'unconfirmed',reason_code:'inventory_specs_unconfirmed',fresh_seconds:120,providers:[{provider:'lium',status:'ok',observed_at:990},{provider:'targon',status:'ok',observed_at:990}],offers:[{gpu_type:'RTX 5090',download_mbps:null}]};
  for(const provider of ['lium','targon']){
    const observation=operatorMarketProvider(market,provider,now);assert.equal(observation.fresh,true);assert.equal(observation.status,'ok');assert.equal(observation.label,'库存已核对');
  }
  assert.match(operatorInventorySummary({market},selection,now),/拆分或规格仍需核对，不能认定 RTX 5090 缺货/);
  assert.equal(operatorRecommendationsCurrent(market,selection,now),false);
  const empty={...market,status:'partial',reason_code:'inventory_no_matching_stock'};
  assert.equal(operatorRecommendationsCurrent(empty,selection,now),true);
  assert.equal(operatorRecommendationsCurrent({...empty,reason_code:'inventory_matches_found'},selection,now),false);
  assert.equal(operatorRecommendationsCurrent({...empty,status:'unconfirmed'},selection,now),false);
  assert.equal(operatorRecommendationsCurrent(empty,selection,now+121000),false);
});
test('stock readiness distinguishes missing specifications, deployment qualification and adjustable limits with legacy compatibility',()=>{
  const row={qualification:'unqualified',deployment_qualified:true,specs_confirmed:false,blockers:['inventory_unknown_bandwidth']};
  assert.equal(operatorOfferReadiness(row),'规格待核对');
  assert.equal(operatorOfferReadiness({...row,deployment_qualified:false}),'规格待核对');
  assert.equal(operatorOfferReadiness({...row,specs_confirmed:true,deployment_qualified:false,blockers:['operator_provider_start_unqualified']}),'部署待验收');
  assert.equal(operatorOfferReadiness({...row,specs_confirmed:true,blockers:['inventory_price_above_limit']}),'条件待调整');
  assert.equal(operatorOfferReadiness({...row,specs_confirmed:true,qualification:'qualified',blockers:[]}),'待启动预览');
  assert.equal(operatorOfferReadiness({qualification:'qualified',blockers:[]}),'待启动预览');
  assert.equal(operatorOfferReadiness({qualification:'unqualified',blockers:[]}),'部署待验收');
});
test('paused pool explains disabled starts without claiming its measured deployment is unqualified',()=>{
  for(const blocker of ['operator_pool_paused',{code:'operator_pool_paused'}]){
    const row={qualification:'unqualified',deployment_qualified:true,specs_confirmed:true,blockers:[blocker]};
    assert.equal(operatorOfferReadiness(row),'启动已暂停');
    assert.equal(reasonText(blocker),'该执行池已暂停启动，请联系管理员核对运行配置。');
    assert.equal(operatorOfferReadiness({...row,blockers:[]}),'待启动预览');
  }
});
test('late inventory response from a previous account cannot be used for recommendations',async()=>{
  const held=deferred(),controller=createOperatorController({client:fakeClient({offers:()=>held.promise}),storage:memory()});
  await controller.setAccount('a');const scan=controller.offers({provider:'targon'});await controller.setAccount('b');held.resolve({market:{recommendations:[{provider:'targon',offer_id:'old-account-offer'}]}});
  await assert.rejects(scan,/账户已改变/);assert.equal(controller.getState().account,'b');assert.equal(controller.getState().pending,null);
});
test('custom deployment profile selects reported GPU topology while preserving authored price/TTL and node count',()=>{
  const selection={node_count:2,ttl_seconds:1800,filters:{max_price_per_gpu_hour_microusd:1500000}};
  const next=profileSelection({id:'pro-bf16',gpu_models:['PRO exact'],gpu_count_options:[2],minimum_ram_bytes:256*1024**3,minimum_disk_bytes:350*1024**3,hardware_filters:{minimum_download_mbps:500,maximum_price_per_gpu_hour_microusd:2000000}},selection,true);
  assert.equal(next.gpu_count,2);assert.equal(next.gpu_type,'PRO exact');assert.equal(next.filters.min_ram_gib,256);assert.equal(next.node_count,2);assert.equal(next.ttl_seconds,1800);assert.equal(next.filters.max_price_per_gpu_hour_microusd,1500000);
  assert.equal(Object.hasOwn(next.filters,'min_download_mbps'),false);
});
test('catalog filters supply profile-specific defaults without leaking another profile or CPU requirement',()=>{
  const initial=initialOperatorSelection({max_ttl_seconds:10800});
  assert.deepEqual(initial.filters,{allowed_countries:[]});
  const primary={id:'5090',gpu_models:['RTX 5090'],gpu_count_options:[1],minimum_ram_bytes:96*1024**3,minimum_disk_bytes:128*1024**3,hardware_filters:{minimum_cpu_cores:12,minimum_download_mbps:200,maximum_price_per_gpu_hour_microusd:850000}};
  const selected=profileSelection(primary,initial);
  assert.deepEqual(selected.filters,{allowed_countries:[],min_ram_gib:96,min_disk_gib:128,min_download_mbps:200,max_price_per_gpu_hour_microusd:850000});
  assert.equal(Object.hasOwn(operatorStartPayload(selected),'filters'),false);
  const pro=profileSelection({...primary,id:'pro',minimum_ram_bytes:256*1024**3,minimum_disk_bytes:350*1024**3,hardware_filters:{minimum_download_mbps:500,maximum_price_per_gpu_hour_microusd:2000000}},selected);
  assert.deepEqual(pro.filters,{allowed_countries:[],min_ram_gib:256,min_disk_gib:350,min_download_mbps:500,max_price_per_gpu_hour_microusd:2000000});
  const noNetwork=profileSelection({...primary,hardware_filters:{maximum_price_per_gpu_hour_microusd:850000}},pro);
  assert.equal(Object.hasOwn(noNetwork.filters,'min_download_mbps'),false);
});
test('preview replaces catalog suggestions with exact server filters, including old deployed defaults and absent bandwidth',()=>{
  const selection={...initialOperatorSelection({max_ttl_seconds:10800}),filters:{min_ram_gib:96,min_disk_gib:128,min_download_mbps:200,allowed_countries:[]}};
  const oldPreview={configuration_id:'configured-old',selection:{...selection,filters:{min_ram_gib:96,min_disk_gib:250,min_download_mbps:500}}};
  assert.deepEqual(previewOperatorSelection(selection,oldPreview).filters,oldPreview.selection.filters);
  const noNetworkPreview={configuration_id:'configured-no-network',selection:{...selection,filters:{min_ram_gib:96,min_disk_gib:128}}};
  const applied=previewOperatorSelection(selection,noNetworkPreview);
  assert.deepEqual(applied.filters,noNetworkPreview.selection.filters);
  assert.equal(Object.hasOwn(applied.filters,'min_download_mbps'),false);
  assert.equal(Object.hasOwn(applied.filters,'allowed_countries'),false);
  assert.notEqual(applied.filters,noNetworkPreview.selection.filters);
  assert.equal(selection.filters.min_download_mbps,200);
  assert.equal(previewOperatorSelection(selection,{}),selection);
});
test('unresolved preview preserves draft filters and cannot label them as bound server conditions',()=>{
  const selection={...initialOperatorSelection({max_ttl_seconds:10800}),filters:{min_ram_gib:96,min_disk_gib:128,min_download_mbps:200}};
  for(const configuration_id of [null,undefined,'',' ',123]){
    const preview={configuration_id,selection:{...selection,filters:{}},can_start:false,blockers:[{code:'operator_deployment_not_configured'}]};
    assert.equal(hasBoundOperatorSelection(preview),false);
    assert.equal(previewOperatorSelection(selection,preview),selection);
  }
  assert.equal(hasBoundOperatorSelection({configuration_id:'configured',selection}),true);
  assert.equal(hasBoundOperatorSelection({configuration_id:'configured'}),false);
});
test('blank optional custom filters are omitted while explicit numeric values still reach server validation',()=>{
  const selection={...initialOperatorSelection({max_ttl_seconds:10800}),filters:{min_download_mbps:200,min_ram_gib:96}};
  selection.filters=operatorFilterEdit(selection.filters,'min_download_mbps','');
  assert.equal(Object.hasOwn(operatorStartPayload(selection,true).filters,'min_download_mbps'),false);
  assert.equal(selection.filters.min_ram_gib,96);
  assert.deepEqual(operatorFilterEdit({min_download_mbps:200},'min_download_mbps',null),{});
  assert.equal(operatorFilterEdit({},'min_download_mbps',0).min_download_mbps,0);
  assert.equal(operatorFilterEdit({},'min_download_mbps',-1).min_download_mbps,-1);
});
test('new operator run uses a policy-bounded 180-minute default and reports provider minimum without changing an override',()=>{
  assert.equal(initialOperatorSelection({max_ttl_seconds:14400}).ttl_seconds,10800);
  assert.equal(initialOperatorSelection({max_ttl_seconds:7200}).ttl_seconds,7200);
  const authored={ttl_seconds:3600},bounds=runtimeDuration({max_ttl_seconds:10800},3780,authored.ttl_seconds);
  assert.equal(bounds.minimumMinutes,63);assert.equal(bounds.maximumMinutes,180);assert.ok(bounds.problem);assert.equal(bounds.suggestedSeconds,10800);assert.equal(authored.ttl_seconds,3600);
  assert.equal(runtimeDuration({max_ttl_seconds:10800},3780,7200).problem,null);
  assert.equal(runtimeDuration({max_ttl_seconds:3600},3780,3600).suggestedSeconds,null);
  assert.ok(runtimeDuration({max_ttl_seconds:3600},3780,3600).problem);
  assert.equal(runtimeDuration({max_ttl_seconds:10800}).minimumMinutes,null);
});
test('default start omits filters and custom Lium filters never invent a CPU requirement',()=>{
  const selection=initialOperatorSelection({max_ttl_seconds:10800});
  assert.equal(Object.hasOwn(selection.filters,'min_cpu_cores'),false);
  assert.equal(Object.hasOwn(operatorStartPayload(selection),'filters'),false);
  selection.filters.min_ram_gib=128;selection.filters.max_price_per_gpu_hour_microusd=1200000;
  assert.equal(operatorStartPayload(selection,true),selection);
  assert.equal(operatorStartPayload(selection,true).filters.min_ram_gib,128);
  assert.equal(operatorStartPayload(selection,true).filters.max_price_per_gpu_hour_microusd,1200000);
  assert.equal(Object.hasOwn(operatorStartPayload(selection,true).filters,'min_cpu_cores'),false);
});
test('explicit deployment receives own control schema without legacy pool limits, old requests remain unchanged',()=>{
  const base={id:'fl',mode:'fl',controls:{steps:{type:'integer',maximum:100}},limits:{max_images:9},execution_support:{constraints:{max_steps:20,input_limits:{max_images:1}}},deployment_preset:{controls:{video_decode:'tiled'}}};
  const capabilities={recipes:[base],deployment_profiles:[{id:'bf16',model_id:'base-bf16',generation_support:{fl:{configured:false,enabled:false,controls:{steps:{type:'integer',maximum:50}},constraints:{input_limits:{max_images:2}}}}}]};
  const settings={recipe_id:'fl',deployment_profile_id:'bf16',controls:{steps:50}},selected=recipeFor(capabilities,settings);
  assert.equal(effectiveControlSchema(selected).steps.maximum,50);assert.equal(effectiveLimits(selected).max_images,2);assert.equal(selected.deployment_preset,null);assert.equal(selected.model_id,'base-bf16');assert.equal(settings.controls.steps,50);assert.equal(recipeFor(capabilities,{recipe_id:'fl'}),base);assert.equal(effectiveControlSchema(base).steps.maximum,20);
});

test('explicit REF profile keeps audio and video clip limits separate from each other and legacy limits',()=>{
  const base={id:'ref',mode:'ref',limits:{min_clip_duration:2,max_clip_duration:3,max_images:9},controls:{},custom_canvas_constraints:{maximum_pixel_area:400000},execution_support:{constraints:{input_limits:{max_audio_duration_seconds:3}}}};
  const support={controls:{},limits:{min_clip_duration:2,max_clip_duration:5.2,max_video_clip_duration:56/24,max_audio_clip_duration:5.2,max_total_video_duration:56/24,max_total_audio_duration:5.2,max_images:1,max_videos:1,max_audios:1,max_guides:0},constraints:{input_limits:{max_video_duration_seconds:56/24,max_audio_duration_seconds:5.2}}};
  const capabilities={recipes:[base],deployment_profiles:[{id:'native',generation_support:{ref:support}}]},selected=recipeFor(capabilities,{recipe_id:'ref',deployment_profile_id:'native'}),limits=effectiveLimits(selected);
  assert.deepEqual(clipLimits(limits,'audio'),{minimum:2,maximum:5.2});assert.deepEqual(clipLimits(limits,'video'),{minimum:2,maximum:56/24});assert.equal(selected.custom_canvas_constraints,null);
  const refs=[{asset_id:'audio',role:'reference'},{asset_id:'video',role:'reference',use_audio:false}],assets=[{id:'audio',kind:'audio',file_name:'voice.wav',metadata:{duration:5.2}},{id:'video',kind:'video',file_name:'motion.mp4',metadata:{duration:56/24,has_audio:false}}];
  assert.deepEqual(inputProblems(refs,assets,selected),[]);
  assert.match(inputProblems(refs,[assets[0],{...assets[1],metadata:{duration:3}}],selected).join(' '),/motion.mp4/);
  assert.match(inputProblems(refs,[{...assets[0],metadata:{duration:5.21}},assets[1]],selected).join(' '),/voice.wav/);
  assert.match(inputProblems([refs[0]],[assets[0]],base).join(' '),/2–3/);
  assert.equal(base.limits.max_clip_duration,3);assert.equal(effectiveLimits(base).max_audio_clip_duration,3);
});

test('missing explicit profile limits never inherit unrelated legacy media or custom canvas limits',()=>{
  const base={id:'ref',mode:'ref',limits:{max_images:9,max_audios:3,max_clip_duration:3},custom_canvas_constraints:{maximum_pixel_area:1},controls:{}};
  const selected=recipeFor({recipes:[base],deployment_profiles:[{id:'draft',generation_support:{ref:{controls:{}}}}]},{recipe_id:'ref',deployment_profile_id:'draft'});
  assert.deepEqual(selected.limits,{});assert.equal(effectiveLimits(selected).max_audio_clip_duration,null);assert.equal(selected.custom_canvas_constraints,null);assert.equal(recipeFor({recipes:[base]},{recipe_id:'ref'}),base);
});


test('safe stop deadline requires fresh verified evidence and never exceeds durable cap',()=>{
  const now=1000000,node={provider_safe_deadline:2000,hard_deadline:1800,provider_lifetime_state:'verified',provider_lifetime_observed_at:990};
  assert.deepEqual(nodeDeadline(node,now),{verified:true,label:'安全停止期限',deadline:1800});
  assert.equal(nodeDeadline({...node,provider_safe_deadline:1700},now).deadline,1700);
  for(const patch of [{provider_lifetime_observed_at:969},{provider_lifetime_observed_at:1001},{provider_lifetime_state:'unverified'},{provider_safe_deadline:null}]){
    const result=nodeDeadline({...node,...patch},now);assert.equal(result.verified,false);assert.equal(result.deadline,1800);assert.match(result.label,/待核对/);
  }
  assert.equal(nodeDeadline({},now).deadline,null);
});

test('removal presentation follows saved confirmation without deriving success or attention from elapsed time',()=>{
  const confirmation={state:'pending',requested_at:1,last_checked_at:null,next_check_at:100,check_interval_seconds:60,attention_after_seconds:300,last_observation:{state:'unknown',provider_status:null,observed_at:null}};
  const node={id:'original-node',state:'destroying',runtime_state:'removal_pending',removal_confirmation:confirmation},original=structuredClone(node);
  const pending=nodeRemovalConfirmation(node);
  assert.equal(pending.label,'等待确认删除');assert.equal(pending.pending,true);assert.equal(pending.tone,'neutral');assert.equal(pending.last_checked_at,null);
  assert.match(pending.message,/供应商尚未确认删除；后台每分钟继续核对/);assert.doesNotMatch(pending.message,/5 分钟|已确认|已停止|不再计费/);
  assert.deepEqual(node,original);
  assert.equal(nodeRemovalConfirmation({state:'destroyed'}),null);assert.equal(nodeRemovalConfirmation({removal_confirmation:{state:'unexpected'}}),null);
  const overdue=nodeRemovalConfirmation({...node,removal_confirmation:{...confirmation,state:'overdue',last_checked_at:400,next_check_at:460}});
  assert.equal(overdue.label,'删除尚未确认');assert.equal(overdue.tone,'warn');assert.equal(overdue.last_checked_at,400);assert.equal(overdue.next_check_at,460);assert.match(overdue.message,/5 分钟以上/);assert.match(overdue.message,/尚未确认删除/);
  const confirmed=nodeRemovalConfirmation({...node,state:'destroyed',removal_confirmation:{...confirmation,state:'confirmed',next_check_at:null}});
  assert.equal(confirmed.pending,false);assert.equal(confirmed.label,'已确认删除');assert.match(confirmed.message,/费用以账本核对结果为准/);assert.doesNotMatch(confirmed.message,/继续核对|结清|免费|不再计费/);
});

test('provider observations remain historical and unknown or conflicting facts do not imply final removal',()=>{
  const node={removal_confirmation:{state:'overdue',last_checked_at:400,last_observation:{state:'running',provider_status:'STOPPED',observed_at:400}}};
  const removal=nodeRemovalConfirmation(node);
  assert.match(removal.observationText,/上次供应商观测/);assert.match(removal.observationText,/运行/);assert.match(removal.observationText,/已停止/);assert.match(removal.message,/尚未确认删除/);
  for(const last_observation of [null,{state:'unknown',provider_status:null},{state:'unrecognized',provider_status:'private-raw-response'}]){
    const unknown=nodeRemovalConfirmation({removal_confirmation:{...node.removal_confirmation,last_observation}});
    assert.equal(unknown.observationText,'上次未能确认供应商状态。');assert.doesNotMatch(unknown.observationText,/private-raw-response/);
  }
});

test('repeated console refresh reads saved deletion checks and retains original node and operation',async()=>{
  const requests=[],node={id:'original-node',runtime_state:'removal_pending',removal_confirmation:{state:'overdue',requested_at:1,last_checked_at:400,next_check_at:460}},operation={id:'original-stop',kind:'stop',node_ids:[node.id],state:'waiting'};
  const client=createOperatorClient({fetcher:async(path,options)=>{requests.push({path,options});return new Response(JSON.stringify(path.endsWith('/state')?{operator:{account:'owner'},nodes:[node],operations:[operation]}:{profiles:[]}),{headers:{'Content-Type':'application/json','X-Authenticated-Account':'owner'}});}});
  const controller=createOperatorController({client,storage:memory()});await controller.setAccount('owner');await controller.refresh();await controller.poll();
  assert.equal(requests.length,6);assert.ok(requests.every(({path,options})=>['/v1/operator/capacity/state','/v1/operator/capacity/catalog'].includes(path)&&options.method==='GET'));
  assert.deepEqual(controller.getState().snapshot.nodes,[node]);assert.deepEqual(controller.getState().snapshot.operations,[operation]);assert.equal(controller.getState().snapshot.nodes[0].removal_confirmation.last_checked_at,400);controller.destroy();
});
