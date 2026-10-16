import test from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {build} from 'esbuild';
import {readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {Module} from 'node:module';
import {createDstackClient} from './dstack-client.js';
import {createOperatorController} from './operator-controller.js';
import {dstackGroups,dstackKeepOnSelection,dstackLifecycle,dstackLifetime,dstackNodeStatus,dstackPreviewCurrent} from './dstack-presentation.js';

const memory=()=>{const values=new Map();return {getItem:key=>values.get(key)||null,setItem:(key,value)=>values.set(key,value),removeItem:key=>values.delete(key)};};
function client(overrides={}){let account;return {setAccount(value){account=value;},reset(){},state:async()=>({capacity_backend:'dstack-v1',enabled:true,operator:{account},nodes:[]}),catalog:async()=>({enabled:true,profiles:[]}),...overrides};}

test('dstack client uses authenticated same-origin endpoints and original command keys',async()=>{
  const calls=[],api=createDstackClient({fetcher:async(path,options)=>{calls.push({path,options});return new Response(JSON.stringify(path.endsWith('/state')?{capacity_backend:'dstack-v1',operator:{account:'superdan'},nodes:[{node_id:'original-node'}]}:{}),{headers:{'Content-Type':'application/json','X-Authenticated-Account':'superdan'}});}});
  api.setAccount('superdan');
  assert.equal((await api.state()).nodes[0].id,'original-node');
  await api.preview({profile_id:'p',ttl_seconds:900});
  await api.start({preview_id:'quote'},'original-start');
  await api.stop('node/one',{expected_version:1},'original-stop');
  await api.hold('node/one',{hold_seconds:300,expected_version:1},'original-hold');
  assert.deepEqual(calls.map(call=>call.path),['/v1/operator/dstack/state','/v1/operator/dstack/previews','/v1/operator/dstack/starts','/v1/operator/dstack/nodes/node%2Fone/stop','/v1/operator/dstack/nodes/node%2Fone/hold']);
  assert.equal(calls[1].options.headers['Idempotency-Key'],undefined);
  assert.deepEqual(calls.slice(2).map(call=>call.options.headers['Idempotency-Key']),['original-start','original-stop','original-hold']);
  for(const call of calls){assert.equal(call.options.credentials,'same-origin');assert.equal(call.options.headers['X-Expected-Account'],'superdan');}
});

test('dstack reads reject missing adapter identity rather than display dummy empty capacity',async()=>{
  const api=createDstackClient({fetcher:async()=>new Response(JSON.stringify({nodes:[]}),{headers:{'Content-Type':'application/json'}})});
  await assert.rejects(api.state(),/dstack state could not be confirmed/);
});

test('dstack unknown start survives reload and replays only the same original request',async()=>{
  const storage=memory(),calls=[];let fail=true;
  const api=client({start:async(body,key)=>{calls.push({body,key});if(fail)throw Error('connection lost');return {command_id:'same-command',node_id:'same-node'};}});
  const first=createOperatorController({client:api,storage,pendingNamespace:'dstack'});
  await first.setAccount('superdan');
  await assert.rejects(first.start({preview_id:'original-preview'}));
  const original=structuredClone(first.getState().pending);first.destroy();
  const resumed=createOperatorController({client:api,storage,pendingNamespace:'dstack'});
  await resumed.setAccount('superdan');await resumed.poll();
  assert.equal(calls.length,1);assert.deepEqual(resumed.getState().pending,original);
  fail=false;await resumed.recover();
  assert.deepEqual(calls[1],calls[0]);assert.equal(resumed.getState().pending,null);
});

test('dstack and legacy pending commands remain in distinct existing owner namespaces',async()=>{
  const storage=memory(),lost=async()=>{throw Error('lost');};
  const legacy=createOperatorController({client:client({start:lost}),storage});
  const dstack=createOperatorController({client:client({start:lost}),storage,pendingNamespace:'dstack'});
  await legacy.setAccount('superdan');await dstack.setAccount('superdan');
  await assert.rejects(legacy.start({preview_id:'legacy'}));await assert.rejects(dstack.start({preview_id:'dstack'}));
  assert.equal(JSON.parse(storage.getItem('sixnine:operator:pending:superdan')).body.preview_id,'legacy');
  assert.equal(JSON.parse(storage.getItem('sixnine:operator:dstack:pending:superdan')).body.preview_id,'dstack');
  await dstack.setAccount('supervan');assert.equal(dstack.getState().pending,null);
  await dstack.setAccount('superdan');assert.equal(dstack.getState().pending.body.preview_id,'dstack');
});

test('hold and stop retain original version and key after unknown responses',async()=>{
  const storage=memory(),calls=[];let fail=true;
  const method=name=>async(id,body,key)=>{calls.push({name,id,body,key});if(fail)throw Error('lost');return {node_id:id};};
  const controller=createOperatorController({client:client({hold:method('hold'),stop:method('stop')}),storage,pendingNamespace:'dstack'});
  await controller.setAccount('superdan');
  await assert.rejects(controller.hold({id:'node',version:1000},300));
  assert.deepEqual(calls[0].body,{expected_version:1000,hold_seconds:300});
  fail=false;await controller.recover();assert.deepEqual(calls[1],calls[0]);
  await controller.stop({id:'node',version:1010});
  assert.deepEqual(calls[2].body,{expected_version:1010});
});

test('authorization loss clears modal data without forgetting an unknown original operation',async()=>{
  for(const status of [401,403]){
    let denied=false;
    const storage=memory(),calls=[];
    const controller=createOperatorController({storage,pendingNamespace:'dstack',client:client({
      state:async()=>{if(denied)throw Object.assign(Error('authorization lost'),{status});return {operator:{account:'superdan'},nodes:[{id:'owned-node'}]};},
      catalog:async()=>({enabled:true,profiles:[{id:'owned-profile'}]}),
      start:async(body,key)=>{calls.push({body,key});throw Error('unknown response');},
    })});
    await controller.setAccount('superdan');
    assert.equal(controller.getState().catalog.profiles[0].id,'owned-profile');
    await assert.rejects(controller.start({preview_id:'owned-preview'}));
    const original=structuredClone(controller.getState().pending);
    denied=true;
    await assert.rejects(controller.refresh());
    assert.equal(controller.getState().snapshot,null);
    assert.equal(controller.getState().catalog,null);
    assert.deepEqual(controller.getState().pending,original);
    assert.deepEqual(JSON.parse(storage.getItem('sixnine:operator:dstack:pending:superdan')),original);
    assert.equal(calls.length,1);
    controller.destroy();
  }
});

test('acknowledged command remains accepted when the subsequent read fails',async()=>{
  let acknowledged=false;
  const controller=createOperatorController({storage:memory(),pendingNamespace:'dstack',client:client({
    state:async()=>{if(acknowledged)throw Error('read temporarily unavailable');return {operator:{account:'superdan'},nodes:[]};},
    start:async()=>{acknowledged=true;return {command_id:'accepted'};}})});
  await controller.setAccount('superdan');
  assert.deepEqual(await controller.start({preview_id:'p'}),{command_id:'accepted'});
  assert.equal(controller.getState().pending,null);
  assert.match(controller.getState().error,/read temporarily unavailable/);
});

test('running is never presented as ready without exact fresh native state',()=>{
  const now=1000000,node={id:'n',state:'runtime_unconfirmed',ready:false,desired_state:'running',observation_fresh:true,observed_at:1000,hard_deadline:2000};
  assert.equal(dstackNodeStatus(node,now).label,'等待运行接口验收');
  assert.equal(dstackNodeStatus({...node,state:'ready',ready:true},now).label,'运行接口就绪');
  assert.equal(dstackNodeStatus({...node,state:'ready',native_ready:true,ready:false},now).label,'等待业务执行槽与准入');
  assert.equal(dstackNodeStatus({...node,state:'ready',ready:true},now+31000).label,'状态待核对');
  assert.equal(dstackNodeStatus({...node,state:'ready',ready:true,observation_fresh:false},now).label,'状态待核对');
});

test('unknown deletion and unsettled costs remain visible; only proven unpaid end becomes history',()=>{
  const groups=dstackGroups([{id:'unknown',state:'removal_unknown',desired_state:'stopped'},
    {id:'terminal',state:'stopped',billing_state:'unsettled'},
    {id:'cancelled',state:'stopped',billing_state:'no_charge_confirmed'}],1000000);
  assert.deepEqual(groups.current.map(node=>node.id),['unknown']);
  assert.deepEqual(groups.review.map(node=>node.id),['terminal']);
  assert.deepEqual(groups.history.map(node=>node.id),['cancelled']);
});

test('expired or changed profile/mode selection cannot confirm another preview',()=>{
  const preview={capacity_backend:'dstack-v1',preview_id:'p',selection:{profile_id:'fl-profile',ttl_seconds:900},
    expires_at:1100,hard_deadline:1900,reservation_microusd:250000,hourly_cost_microusd:750000};
  assert.equal(dstackPreviewCurrent(preview,'fl-profile',900,1000000),true);
  assert.equal(dstackPreviewCurrent(preview,'ref-profile',900,1000000),false);
  assert.equal(dstackPreviewCurrent(preview,'fl-profile',1800,1000000),false);
  assert.equal(dstackPreviewCurrent(preview,'fl-profile',900,1100000),false);
});

const lifecycle=(changes={})=>({version:2,keep_on:'idle',hold_until:0,idle_shutdown_seconds:600,
  reservation_seconds:3600,expected_runtime_s:1800,budget_account_ids:['owner-cap','global-cap'],...changes});
const continuingPreview=(changes={})=>({capacity_backend:'dstack-v1',preview_id:'original-quote',
  selection:{profile_id:'ref-profile',keep_on:'idle',hold_seconds:0},expires_at:1100,hard_deadline:null,
  reservation_microusd:920834,hourly_cost_microusd:850000,price_basis:'approved_ceiling',can_start:true,
  initial_reservation_seconds:3600,lifecycle:lifecycle(),...changes});

test('explicit continuing preview accepts null deadline while retaining selection, expiry and funded quote guards',()=>{
  const preview=continuingPreview(),policy=dstackKeepOnSelection('idle',10),now=1000000;
  assert.equal(dstackPreviewCurrent(preview,'ref-profile',policy,now),true);
  // A catalog refresh may change lifecycle versions: an old finite form must
  // never consent to a continuing rental through the API's TTL compatibility.
  assert.equal(dstackPreviewCurrent({...preview,selection:{profile_id:'ref-profile',ttl_seconds:900}},'ref-profile',900,now),false);
  for(const [changed,profile,selection] of [
    [preview,'fl-profile',policy],
    [preview,'ref-profile',dstackKeepOnSelection('until_stopped',10)],
    [{...preview,expires_at:1000},'ref-profile',policy],
    [{...preview,expires_at:null},'ref-profile',policy],
    [{...preview,lifecycle:undefined},'ref-profile',policy],
    [{...preview,hard_deadline:undefined},'ref-profile',policy],
    [{...preview,hard_deadline:1900},'ref-profile',policy],
    [{...preview,lifecycle:lifecycle({version:1})},'ref-profile',policy],
    [{...preview,lifecycle:lifecycle({keep_on:'always'})},'ref-profile',policy],
    [{...preview,lifecycle:lifecycle({idle_shutdown_seconds:0})},'ref-profile',policy],
    [{...preview,lifecycle:lifecycle({budget_account_ids:[]})},'ref-profile',policy],
    [{...preview,initial_reservation_seconds:NaN},'ref-profile',policy],
    [{...preview,price_basis:'observed'},'ref-profile',policy],
    [{...preview,can_start:false},'ref-profile',policy],
    [{...preview,hourly_cost_microusd:0},'ref-profile',policy],
    [{...preview,reservation_microusd:-1},'ref-profile',policy],
  ])assert.equal(dstackPreviewCurrent(changed,profile,selection,now),false);
  const held=continuingPreview({selection:{profile_id:'ref-profile',keep_on:'until',hold_seconds:600},lifecycle:lifecycle({keep_on:'until',hold_until:1600})});
  assert.equal(dstackPreviewCurrent(held,'ref-profile',dstackKeepOnSelection('until',10),now),true);
  assert.equal(dstackPreviewCurrent(held,'ref-profile',dstackKeepOnSelection('until',11),now),false);
  const idleChanged={...preview,selection:{...preview.selection,idle_shutdown_seconds:300}};
  assert.equal(dstackPreviewCurrent(idleChanged,'ref-profile',policy,now),false);
});

test('continuing status uses the exact node policy without turning null deadlines into readiness',()=>{
  const now=1000000,node={id:'n',state:'runtime_unconfirmed',ready:false,desired_state:'running',
    observation_fresh:true,observed_at:1000,hard_deadline:null,hold_until:0,lifecycle:lifecycle()};
  assert.equal(dstackNodeStatus(node,now).label,'等待运行接口验收');
  assert.equal(dstackNodeStatus({...node,state:'ready',ready:true},now).label,'运行接口就绪');
  assert.equal(dstackNodeStatus({...node,state:'ready',ready:false},now).label,'等待业务执行槽与准入');
  assert.equal(dstackNodeStatus(node,now+31000).label,'状态待核对');
  assert.equal(dstackNodeStatus({...node,lifecycle:undefined},now).group,'review');
  assert.equal(dstackNodeStatus({...node,lifecycle:lifecycle({version:1})},now).group,'review');
  assert.equal(dstackNodeStatus({...node,hold_until:123},now).group,'review');
  assert.match(dstackLifetime(node),/空闲 10 分钟后关机/);
  assert.match(dstackLifetime({...node,lifecycle:lifecycle({keep_on:'until_stopped'})}),/保留至手动关机.*原预算限制/);
  assert.match(dstackLifetime({...node,hold_until:1600,lifecycle:lifecycle({keep_on:'until',hold_until:1600})}),/保留至.*之后空闲/);
  assert.equal(dstackLifecycle({...node,lifecycle:lifecycle({budget_account_ids:['duplicate','duplicate']})}),null);
  const finite={...node,lifecycle:undefined,hard_deadline:999};
  assert.equal(dstackNodeStatus(finite,now).label,'期限已到 · 等待停止核对');
});

test('all continuing keep-on choices use the original versioned hold journal and replay identity',async()=>{
  const calls=[],storage=memory();let fail=true;
  const controller=createOperatorController({storage,pendingNamespace:'dstack',client:client({
    hold:async(id,body,key)=>{calls.push({id,body,key});if(fail)throw Error('unknown hold');return {node_id:id};},
  })});
  await controller.setAccount('superdan');
  assert.equal(dstackKeepOnSelection('until',0),null);
  assert.equal(dstackKeepOnSelection('until',1441),null);
  const original=dstackKeepOnSelection('until_stopped',10);
  await assert.rejects(controller.hold({id:'original-node',version:1000},original));
  assert.deepEqual(calls[0].body,{keep_on:'until_stopped',hold_seconds:0,expected_version:1000});
  fail=false;await controller.recover();assert.deepEqual(calls[1],calls[0]);
  await controller.hold({id:'original-node',version:1010},dstackKeepOnSelection('until',10));
  assert.deepEqual(calls[2].body,{keep_on:'until',hold_seconds:600,expected_version:1010});
  await controller.hold({id:'original-node',version:1020},dstackKeepOnSelection('idle',10));
  assert.deepEqual(calls[3].body,{keep_on:'idle',hold_seconds:0,expected_version:1020});
  controller.destroy();
});

test('actual start/detail forms use v2 policy controls while old finite machines retain their deadline bounds',async()=>{
  const path=fileURLToPath(new URL('./DstackCapacity.jsx',import.meta.url));
  const source=await readFile(path,'utf8');
  const bundled=await build({stdin:{contents:source+"\nexport {Start,NodeDialog}; export {I18nProvider as TestI18nProvider} from './i18n/I18nProvider.jsx';",resolveDir:path.replace(/[/\\][^/\\]+$/,''),loader:'jsx'},bundle:true,write:false,platform:'node',format:'cjs',packages:'external',loader:{'.css':'empty'}});
  const module=new Module(path);module.filename=path;module.paths=Module._nodeModulePaths(path.replace(/[/\\][^/\\]+$/,''));module._compile(bundled.outputFiles[0].text,path);
  const prior=globalThis.document;globalThis.document={activeElement:null};
  const storage=Object.getOwnPropertyDescriptor(globalThis,'localStorage');Object.defineProperty(globalThis,'localStorage',{configurable:true,value:{getItem:()=> 'zh'}});
  const renderLocalized=element=>renderToStaticMarkup(React.createElement(module.exports.TestI18nProvider,null,element));
  try{
    const props={controller:{},busy:false,now:1000000,onClose:()=>{}},profile={id:'ref-profile',model_id:'pruned-int8',mode:'ref',backend:'vastai',gpu_names:['RTX5090'],memory_gib:29,disk_gib:200,gpu_memory_gib:32,max_ttl_seconds:3600};
    const form=version=>renderLocalized(React.createElement(module.exports.Start,{...props,profiles:[profile],lifecycleVersion:version}));
    assert.match(form(2),/直到我手动关机/);assert.match(form(2),/没有固定运行截止时间/);assert.doesNotMatch(form(2),/最多运行多久/);
    assert.match(form(1),/最多运行多久/);assert.doesNotMatch(form(1),/直到我手动关机/);
    const node={id:'n',provider:'vast',runtime_profile_id:'ref-profile',mode:'ref',state:'ready',ready:true,desired_state:'running',observation_fresh:true,observed_at:1000,hard_deadline:null,hold_until:0,hourly_cost_microusd:850000,lifecycle:lifecycle()};
    const details=value=>renderLocalized(React.createElement(module.exports.NodeDialog,{...props,node:value}));
    assert.match(details(node),/确认待机策略/);assert.doesNotMatch(details(node),/原停止上限内|尚未确认<\/dd>|<button disabled/);
    assert.match(details({...node,lifecycle:lifecycle({keep_on:'until_stopped'})}),/保留至手动关机.*原预算限制/);
    const old=details({...node,lifecycle:undefined,hard_deadline:1600});
    assert.match(old,/原停止上限内/);assert.match(old,/max="10"/);assert.doesNotMatch(old,/直到我手动关机/);
  }finally{if(storage)Object.defineProperty(globalThis,'localStorage',storage);else delete globalThis.localStorage;if(prior===undefined)delete globalThis.document;else globalThis.document=prior;}
});
