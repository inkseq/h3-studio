import test from 'node:test';
import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {Module} from 'node:module';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {createP4OperatorClient} from './p4-operator-client.js';
import {createP4OperatorController} from './p4-operator-controller.js';
import {billingAllocationInputs,billingOperation,billingReceipt,billingTimeBounds,validBillingPage,validBillingPreview,validBillingRequest} from './operator-billing-model.js';

const hash='a'.repeat(64),owner='supervan';
const row=(id='job-1',patch={})=>({owner_id:owner,reference_type:'job',reference_id:id,snapshot:hash,state:'settled',
  provider:null,provider_instance_id:null,provider_actual_cost_microusd:null,allocated_cost_microusd:null,
  user_charge_microusd:null,reserved_estimate_microusd:800000,waived:false,eligible_actions:['waive'],block_reason:null,...patch});
const request=(items=[row()],patch={})=>({owner_id:owner,action:'waive',reason:'Pre-launch internal account',
  evidence_note:'Reviewed original reference and retained cost',evidence_sha256:hash,
  items:items.map(value=>({reference_type:value.reference_type,reference_id:value.reference_id,expected_snapshot:value.snapshot})),...patch});
const totals=count=>({reference_count:count,instance_actual_microusd:1000000,job_allocated_microusd:1000000,waived_reference_count:count,waived_exposure_microusd:800000});
const preview=(body=request())=>({preview_id:'preview-1',snapshot:'b'.repeat(64),request_hash:'c'.repeat(64),expires_at:Date.now()/1000+300,
  request:structuredClone(body),items:body.items.map(value=>row(value.reference_id,{reference_type:value.reference_type,snapshot:value.expected_snapshot})),totals:totals(body.items.length)});
const receipt=p=>({action_id:'action-1',state:'completed',owner_id:p.request.owner_id,action:p.request.action,request_hash:p.request_hash,
  preview_id:p.preview_id,items:p.items.map(value=>({...value,waived:true,snapshot:'d'.repeat(64)})),totals:p.totals,audit_event_id:'audit-1'});
const memory=()=>{const values=new Map();return {getItem:key=>values.get(key)||null,setItem:(key,value)=>values.set(key,value),removeItem:key=>values.delete(key)};};
function fake(overrides={}){let account;return {setAccount:value=>{account=value;},reset(){},
  state:async()=>({operator:{account,admin:true},nodes:[],groups:[],queue:[]}),adminState:async()=>({operator:{account,admin:true},nodes:[],groups:[],queue:[]}),catalog:async()=>({profiles:[]}),
  billingReferences:async()=>({owner_id:owner,items:[row()],next_cursor:null}),previewBilling:async body=>preview(body),...overrides};}

test('97-reference waiver requires evidence and immutable selection; foreign owner, duplicate and missing costs cannot be projected as valid',()=>{
  const rows=Array.from({length:97},(_,i)=>row('job-'+i)),body=request(rows),p=preview(body);
  assert.equal(validBillingRequest(body),true);assert.equal(validBillingPreview(p,body),true);
  assert.equal(validBillingPage({owner_id:owner,items:rows,next_cursor:null},owner),true);
  assert.equal(validBillingPage({owner_id:owner,items:[row('foreign',{owner_id:'superdan'})],next_cursor:null},owner),false);
  for(const patch of [{reason:''},{reason:'two\nlines'},{evidence_sha256:null},{evidence_note:''},{evidence_note:'two\tparts'},{items:[body.items[0],body.items[0]]},{unexpected:true}])assert.equal(validBillingRequest({...body,...patch}),false);
  assert.equal(validBillingPreview({...p,request:{...body,owner_id:'superdan'}},body),false);
  assert.equal(validBillingPreview({...p,items:[{...p.items[0],owner_id:'superdan'},...p.items.slice(1)]},body),false);
  assert.equal(validBillingPreview({...p,expires_at:0},body),false);
  const original=structuredClone(p);assert.equal(billingReceipt(receipt(p),billingOperation(p)),true);assert.deepEqual(p,original);
  assert.equal(billingReceipt({...receipt(p),request_hash:hash},billingOperation(p)),false);
});

test('reviewed allocation import fills only 95 selected jobs and 2 rentals with exact integer amounts, never creates or calculates a reference',()=>{
  const rows=Array.from({length:95},(_,i)=>row('job-'+i)).concat([row('rental-a',{reference_type:'instance'}),row('rental-b',{reference_type:'instance'})]);
  const body=request(rows,{action:'settle'});body.items=body.items.map((item,i)=>({...item,actual_cost_microusd:i<95?1001:50000000,...(item.reference_type==='job'?{allocation_instance_id:i<50?'rental-a':'rental-b'}:{})}));
  const imported=billingAllocationInputs(JSON.stringify(body),rows);
  assert.equal(Object.keys(imported).length,97);assert.equal(imported['job:job-1'].amount,1001);assert.equal(imported['job:job-1'].allocation,'rental-a');
  assert.equal(imported['instance:rental-b'].amount,50000000);assert.equal(validBillingRequest(body),true);
  for(const changed of [{...body,owner_id:'superdan'},{...body,items:body.items.slice(1)},
    {...body,items:body.items.map((item,i)=>i===0?{...item,reference_id:'not-selected'}:item)},
    {...body,items:body.items.map((item,i)=>i===0?{...item,expected_snapshot:'e'.repeat(64)}:item)},
    {...body,items:body.items.map((item,i)=>i===0?{...item,actual_cost_microusd:-1}:item)}])assert.equal(billingAllocationInputs(JSON.stringify(changed),rows),null);
});

test('explicit-offset creation filters preserve the requested Sydney day and reject guessed local dates or invalid calendar bounds',()=>{
  assert.deepEqual(billingTimeBounds(),{});
  assert.deepEqual(billingTimeBounds('2026-10-17T00:00:00+11:00','2026-10-18T00:00:00+11:00'),{created_after:Date.parse('2026-10-16T13:00:00Z')/1000,created_before:Date.parse('2026-10-17T13:00:00Z')/1000});
  for(const value of ['2026-10-17','2026-10-17T00:00','2026-02-30T00:00:00Z','2026-10-17T24:00:00Z'])assert.equal(billingTimeBounds(value),null);
  assert.equal(billingTimeBounds('2026-10-18T00:00:00Z','2026-10-17T00:00:00Z'),null);
});

test('billing pagination cannot combine another time window and forwards the original filters to the server',async()=>{
  const calls=[],controller=createP4OperatorController({client:fake({billingReferences:async value=>{calls.push(value);return {owner_id:owner,items:[row(value.after?'older':'newer')],next_cursor:value.after?null:'next'};}}),storage:memory()});
  await controller.setAccount('superdan');await controller.readBillingReferences({owner_id:owner,created_after:100,created_before:200});
  await assert.rejects(controller.readBillingReferences({owner_id:owner,after:'next',created_after:100,created_before:201}));assert.equal(calls.length,1);
  await controller.readBillingReferences({owner_id:owner,after:'next',created_after:100,created_before:200});assert.equal(controller.getState().billingReferences.items.length,2);assert.equal(calls[1].created_before,200);
});

test('unknown financial write is persisted before send and recovered by exact original key without another mutation',async()=>{
  const storage=memory(),calls=[];let p;
  const api=fake({previewBilling:async body=>(p=preview(body)),applyBilling:async(body,key)=>{const stored=JSON.parse(storage.getItem('sixnine:operator:dstack:pending:superdan'));assert.deepEqual(stored.body,body);assert.equal(stored.key,key);calls.push({body:structuredClone(body),key});throw Error('ack lost');},billingActionByKey:async key=>{assert.equal(key,calls[0].key);return receipt(p);}});
  const controller=createP4OperatorController({client:api,storage});await controller.setAccount('superdan');const view=await controller.previewBilling(request());
  await assert.rejects(controller.applyBilling(view));const original=structuredClone(controller.getState().pending);assert.ok(original);assert.equal(calls.length,1);
  const resumed=createP4OperatorController({client:api,storage});await resumed.setAccount('superdan');assert.equal((await resumed.recover()).action_id,'action-1');
  assert.equal(calls.length,1);assert.equal(resumed.getState().pending,null);assert.equal(original.body.confirmed,true);
});

test('definitive by-key absence replays the same body/key once; arbitrary 404, mismatched receipts and bare conflicts retain original recovery',async()=>{
  for(const mode of ['absent','arbitrary404','wrongReceipt','bareConflict']){
    const calls=[];let p;
    const api=fake({previewBilling:async body=>(p=preview(body)),applyBilling:async(body,key)=>{calls.push({body:structuredClone(body),key});if(calls.length===1)throw Error('lost');return receipt(p);},billingActionByKey:async()=>{if(mode==='wrongReceipt')return {...receipt(p),request_hash:hash};throw Object.assign(Error('unconfirmed'),{status:mode==='bareConflict'?409:404,detail:{code:mode==='absent'?'billing_action_not_found':'other'}});}});
    const controller=createP4OperatorController({client:api,storage:memory()});await controller.setAccount('superdan');await assert.rejects(controller.applyBilling(await controller.previewBilling(request())));const original=structuredClone(controller.getState().pending);
    if(mode==='absent'){await controller.recover();assert.deepEqual(calls[1],calls[0]);assert.equal(controller.getState().pending,null);}else{await assert.rejects(controller.recover());assert.equal(calls.length,1);assert.deepEqual(controller.getState().pending,original);}
  }
});

test('only explicit no-mutation proof releases a stale apply; expired/foreign-operator/permission-lost previews never submit',async()=>{
  for(const definite of [false,true]){
    let calls=0;const controller=createP4OperatorController({client:fake({applyBilling:async()=>{calls++;throw Object.assign(Error('stale'),{status:409,detail:{code:'billing_snapshot_changed',...(definite?{no_mutation_committed:true}:{})}});}}),storage:memory()});
    await controller.setAccount('superdan');await assert.rejects(controller.applyBilling(await controller.previewBilling(request())));assert.equal(calls,1);assert.equal(!!controller.getState().pending,!definite);
  }
  let calls=0,allowed=true;const api=fake({adminState:async()=>({operator:{account:'superdan',admin:allowed},nodes:[],groups:[],queue:[]}),applyBilling:async()=>{calls++;}}),controller=createP4OperatorController({client:api,storage:memory()});await controller.setAccount('superdan');const p=await controller.previewBilling(request());
  await assert.rejects(controller.applyBilling({...p,expires_at:0}));await assert.rejects(controller.applyBilling({...p,operator_account:'supervan'}));allowed=false;await assert.rejects(controller.applyBilling(p));assert.equal(calls,0);assert.equal(controller.getState().pending,null);
  const conflict=createP4OperatorController({client:fake({applyBilling:async()=>{throw Object.assign(Error('conflicting original key'),{status:409,detail:{code:'billing_idempotency_conflict',no_mutation_committed:true}});}}),storage:memory()});await conflict.setAccount('superdan');await assert.rejects(conflict.applyBilling(await conflict.previewBilling(request())));assert.ok(conflict.getState().pending,'a conflicting original key still needs original-receipt investigation');
});

test('read-only obligation pages are discarded on account switch or permission loss, and ordinary operators cannot read',async()=>{
  let resolve;const api=fake({billingReferences:()=>new Promise(done=>{resolve=done;})}),controller=createP4OperatorController({client:api,storage:memory()});await controller.setAccount('superdan');const reading=controller.readBillingReferences({owner_id:owner});await controller.setAccount('supervan');resolve({owner_id:owner,items:[row()],next_cursor:null});await assert.rejects(reading);assert.equal(controller.getState().billingReferences,null);
  const denied=createP4OperatorController({client:fake({state:async()=>({operator:{account:owner,admin:false},nodes:[],groups:[],queue:[]}),billingReferences:async()=>{throw Error('must not read');}}),storage:memory()});await denied.setAccount(owner);await assert.rejects(denied.readBillingReferences({owner_id:owner}),/管理员/);
});

test('billing client uses existing expected-account cookie boundary and original idempotency header on exact API paths',async()=>{
  const calls=[],api=createP4OperatorClient({fetcher:async(path,options)=>{calls.push({path,options});const value=path.includes('/references?')?{owner_id:owner,items:[row()],next_cursor:null}:{};return new Response(JSON.stringify(value),{headers:{'Content-Type':'application/json','X-Authenticated-Account':'superdan'}});}});api.setAccount('superdan');
  await api.billingReferences({owner_id:owner,limit:100});await api.applyBilling({preview_id:'p',expected_snapshot:hash,confirmed:true},'original-key');await api.billingActionByKey('original-key');
  assert.equal(calls[0].path,'/v1/operator/admin/billing/references?owner_id=supervan&limit=100');assert.equal(calls[1].options.headers['Idempotency-Key'],'original-key');assert.equal(calls[2].path,'/v1/operator/admin/billing/actions/by-key?key=original-key');
  assert.ok(calls.every(({options})=>options.headers['X-Expected-Account']==='superdan'&&options.credentials==='same-origin'));
});

test('real admin component renders bulk action, evidence and authoritative confirmation in both languages without ordinary-owner controls',async()=>{
  const source=`export {default as Panel} from './src/OperatorBillingPanel.jsx';export {default as Spending} from './src/SpendingBreakdown.jsx';export {I18nProvider} from './src/i18n/I18nProvider.jsx';`;
  const result=await build({stdin:{contents:source,resolveDir:process.cwd(),loader:'jsx'},bundle:true,write:false,platform:'node',format:'cjs',external:['react','react-dom'],logLevel:'silent'});
  const module=new Module(import.meta.url);module.paths=Module._nodeModulePaths(process.cwd());module._compile(result.outputFiles[0].text,import.meta.url);
  const old=Object.getOwnPropertyDescriptor(globalThis,'localStorage');
  try{for(const language of ['en','zh']){Object.defineProperty(globalThis,'localStorage',{configurable:true,value:{getItem:()=>language}});const state={account:owner,snapshot:{operator:{admin:true}},billingFilter:{owner_id:owner},billingReferences:{owner_id:owner,items:[row()],next_cursor:null}};
    const html=renderToStaticMarkup(React.createElement(module.exports.I18nProvider,null,React.createElement(module.exports.Panel,{controller:{},state,busy:false})));
    assert.match(html,language==='en'?/Select up to 200 eligible loaded references/:/选择已加载的可处理记录/);assert.match(html,language==='en'?/Evidence SHA-256/:/依据文件 SHA-256/);assert.doesNotMatch(html,/billing\./);
    const ordinary=renderToStaticMarkup(React.createElement(module.exports.I18nProvider,null,React.createElement(module.exports.Panel,{controller:{},state:{...state,snapshot:{operator:{admin:false}}},busy:false})));assert.equal(ordinary,'');
    const zero={settled_today_microusd:0,reserved_microusd:0,awaiting_invoice_estimate_microusd:0,awaiting_invoice_count:0,prior_day_pending_estimate_microusd:0,legacy_settled_unknown_date_microusd:0,exposure_microusd:0,unconfirmed_exposure_microusd:0,unconfirmed_reference_count:0};
    const net={...zero,schema_version:1,attribution_scope:'connection',by_kind:{job:zero,instance:zero},waived_reference_count:97,waived_exposure_microusd:4000000};
    const usage={day:'2026-10-19',timezone:'Australia/Sydney',projected_microusd:195000000,charge_breakdown:net,breakdown:{...net,settled_today_microusd:4000000,exposure_microusd:4000000}};
    const spending=renderToStaticMarkup(React.createElement(module.exports.I18nProvider,null,React.createElement(module.exports.Spending,{usage,scope:'connection'})));
    assert.match(spending,/spend-breakdown-buckets[\s\S]*?\$0\.00/);assert.match(spending,language==='en'?/Charges from this assistant/:/此助手产生的应付费用/);assert.match(spending,language==='en'?/97 references waived/:/已免除 97 条记录/);assert.match(spending,/\$4\.00/);assert.doesNotMatch(spending,/\$195\.00/);
  }}finally{if(old)Object.defineProperty(globalThis,'localStorage',old);else delete globalThis.localStorage;}
});
