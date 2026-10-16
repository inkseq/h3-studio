import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {Module} from 'node:module';
import {build} from 'esbuild';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {createP4OperatorClient} from './p4-operator-client.js';
import {createP4OperatorController} from './p4-operator-controller.js';
import {cancellationBody,cancellationReceipt,canCancelJob,jobDiagnostics,jobStatus,selectionCurrent,validAdminJobs} from './operator-jobs-model.js';

const memory=()=>{const data=new Map();return {getItem:key=>data.get(key)||null,setItem:(key,value)=>data.set(key,value),removeItem:key=>data.delete(key)};};
const job=(id='one',extra={})=>({id,owner_id:'supervan',project_id:'original-project',status:'queued',created_at:100,updated_at:101,cancellable:true,snapshot:'a'.repeat(64),...extra});
const page=(jobs=[job()],extra={})=>({operator:{account:'superdan',admin:true},jobs,next_after_id:null,...extra});
function client(overrides={}){let account;return {setAccount:value=>{account=value;},reset(){},state:async()=>({operator:{account,admin:true},nodes:[],groups:[],queue:[]}),catalog:async()=>({profiles:[]}),adminState:async()=>({operator:{account,admin:true},nodes:[],groups:[],queue:[]}),adminJobs:async()=>page(),...overrides};}
const receipt=body=>({command_id:'original-command',state:'completed',reason_code:body.reason_code,cancelled_job_ids:body.jobs.map(row=>row.id)});

test('admin cancellation API preserves cookie actor, pagination, exact body and original key',async()=>{
  const calls=[];
  const api=createP4OperatorClient({fetcher:async(path,options)=>{calls.push({path,options});return new Response(JSON.stringify(options.method==='POST'?receipt(JSON.parse(options.body)):page()),{headers:{'Content-Type':'application/json','X-Authenticated-Account':'superdan'}});}});
  api.setAccount('superdan');await api.adminJobs({after_id:'cursor/one'});const body=cancellationBody([job()],'operator_queue_cleared');await api.cancelJobs(body,'same-original-key');
  assert.deepEqual(calls.map(call=>call.path),['/v1/operator/admin/jobs?limit=100&after_id=cursor%2Fone','/v1/operator/admin/jobs/cancel']);
  assert.deepEqual(JSON.parse(calls[1].options.body),body);assert.equal(calls[1].options.headers['Idempotency-Key'],'same-original-key');
  for(const {options} of calls){assert.equal(options.credentials,'same-origin');assert.equal(options.headers['X-Expected-Account'],'superdan');assert.equal(options.headers.Authorization,undefined);}
});

test('only supported explicit server eligibility and immutable selection snapshots permit cancellation',()=>{
  assert.equal(canCancelJob(job()),true);assert.equal(canCancelJob(job('wait',{status:'waiting_capacity'})),true);
  for(const extra of [{status:'running'},{status:'collecting'},{status:'reconciling'},{status:'unknown'},{cancellable:false},{snapshot:'unconfirmed'},{owner_id:null}])assert.equal(canCancelJob(job('unsafe',extra)),false);
  assert.equal(cancellationBody([job()],'free raw reason'),null);assert.equal(cancellationBody([],'operator_queue_cleared'),null);assert.equal(cancellationBody([job(),job()],'operator_queue_cleared'),null);
  assert.equal(cancellationBody(Array.from({length:101},(_,index)=>job('id'+index)),'operator_queue_cleared'),null);
  assert.equal(selectionCurrent([job()],[job('one',{snapshot:'b'.repeat(64)})]),false);assert.equal(selectionCurrent([job()],[job('one',{owner_id:'other'})]),false);
  assert.equal(validAdminJobs(page(),'superdan'),true);assert.equal(validAdminJobs(page([job(),job()]),'superdan'),false);assert.equal(validAdminJobs(page([],{operator:{account:'supervan',admin:true}}),'superdan'),false);
});

test('non-admin, invalid reason and ineligible selections never create an operator command or write',async()=>{
  for(const [admin,rows,reason] of [[false,[job()],'operator_queue_cleared'],[true,[job()],''],[true,[job('run',{status:'running'})],'operator_queue_cleared']]){
    let writes=0,starts=0;const storage=memory(),controller=createP4OperatorController({storage,client:client({state:async()=>({operator:{account:'superdan',admin},nodes:[],groups:[],queue:[]}),cancelJobs:async()=>{writes++;},start:async()=>{starts++;}})});
    await controller.setAccount('superdan');await assert.rejects(controller.cancelJobs(rows,reason));assert.equal(controller.getState().pending,null);assert.equal(writes,0);assert.equal(starts,0);controller.destroy();
  }
});

test('lost cancellation receipt stays fenced through reload and recovers identical original body/key once',async()=>{
  const calls=[],storage=memory();let lost=true;
  const api=client({cancelJobs:async(body,key)=>{calls.push({body:structuredClone(body),key});if(lost)throw Error('lost');return receipt(body);}});
  const original=createP4OperatorController({storage,client:api});await original.setAccount('superdan');await assert.rejects(original.cancelJobs([job(),job('two',{owner_id:'superdan',status:'waiting_capacity'})],'capacity_unavailable'));
  const pending=structuredClone(original.getState().pending);assert.equal(pending.method,'cancelJobs');await assert.rejects(original.cancelJobs([job('three')],'operator_request_cancelled'),/原操作结果/);assert.equal(calls.length,1);original.destroy();
  const resumed=createP4OperatorController({storage,client:api});await resumed.setAccount('superdan');assert.deepEqual(resumed.getState().pending,pending);lost=false;await resumed.recover();assert.deepEqual(calls[1],calls[0]);assert.equal(calls.length,2);assert.equal(resumed.getState().pending,null);assert.ok(resumed.getState().adminJobs);resumed.destroy();
});

test('only exact completed cancellation receipt clears pending; partial, wrong IDs or reason stay unknown',async()=>{
  const body=cancellationBody([job()],'operator_queue_cleared');
  for(const change of [{state:'accepted'},{command_id:null},{cancelled_job_ids:[]},{cancelled_job_ids:['other']},{cancelled_job_ids:['one','one']},{reason_code:'capacity_unavailable'}]){
    const value={...receipt(body),...change};assert.equal(cancellationReceipt(value,body),false);
    const controller=createP4OperatorController({storage:memory(),client:client({cancelJobs:async()=>value})});await controller.setAccount('superdan');await assert.rejects(controller.cancelJobs([job()],'operator_queue_cleared'),/原取消收据/);assert.equal(controller.getState().pending.method,'cancelJobs');controller.destroy();
  }
});

test('atomic eligibility rejection clears only the command journal without a retry or changed selection',async()=>{
  const selected=[job()],original=structuredClone(selected);let writes=0;
  const controller=createP4OperatorController({storage:memory(),client:client({cancelJobs:async()=>{writes++;throw Object.assign(Error('operator_jobs_changed'),{status:409});}})});
  await controller.setAccount('superdan');await assert.rejects(controller.cancelJobs(selected,'operator_request_cancelled'),/operator_jobs_changed/);assert.equal(writes,1);assert.equal(controller.getState().pending,null);assert.deepEqual(selected,original);controller.destroy();
});

test('cross-account admin responses and delayed pre-write identity changes never leak or write',async()=>{
  let current='superdan',defer=false,resolve,writes=0;
  const api=client({setAccount:value=>{current=value;},state:async()=>({operator:{account:current,admin:true},nodes:[],groups:[],queue:[]}),adminState:()=>defer?new Promise(done=>{resolve=done;}):Promise.resolve({operator:{account:current,admin:true},nodes:[]}),cancelJobs:async()=>{writes++;}});
  const controller=createP4OperatorController({storage:memory(),client:api});await controller.setAccount('superdan');await controller.readJobAdmin();defer=true;
  const operation=controller.cancelJobs([job()],'operator_queue_cleared');await controller.setAccount('supervan');resolve({operator:{account:'superdan',admin:true},nodes:[]});await assert.rejects(operation,/账户已改变/);assert.equal(writes,0);assert.equal(controller.getState().adminJobs,null);assert.equal(controller.getState().pending,null);
  await assert.rejects(controller.readJobAdmin(),/身份或原状态/);assert.equal(controller.getState().adminJobs,null);controller.destroy();
});

test('unknown original cancellation recovery guards an account switch after admin-read emission before writing',async()=>{
  const calls=[],storage=memory();let account='superdan';
  const api=client({setAccount:value=>{account=value;},state:async()=>({operator:{account,admin:true},nodes:[],groups:[],queue:[]}),adminState:async()=>({operator:{account,admin:true},nodes:[]}),cancelJobs:async(body,key)=>{calls.push({account,body,key});throw Error('lost');}});
  const controller=createP4OperatorController({storage,client:api});await controller.setAccount('superdan');await assert.rejects(controller.cancelJobs([job()],'operator_queue_cleared'));const original=structuredClone(controller.getState().pending);
  const previous=controller.getState().adminSnapshot;let switched=false;const unsubscribe=controller.subscribe(()=>{if(!switched&&controller.getState().adminSnapshot!==previous&&controller.getState().adminSnapshot?.operator?.account==='superdan'){switched=true;void controller.setAccount('supervan');}});
  await assert.rejects(controller.recover(),/账户已改变/);assert.equal(switched,true);assert.equal(calls.length,1);assert.equal(controller.getState().pending,null);assert.deepEqual(JSON.parse(storage.getItem('sixnine:operator:dstack:pending:superdan')),original);assert.equal(storage.getItem('sixnine:operator:dstack:pending:supervan'),null);unsubscribe();controller.destroy();
});

test('bounded page append rejects cursor loops and admin demotion removes all-owner task data',async()=>{
  let admin=true,loop=false;
  const controller=createP4OperatorController({storage:memory(),client:client({state:async()=>({operator:{account:'superdan',admin},nodes:[],groups:[],queue:[]}),adminJobs:async({after_id})=>after_id?page([job('two')],{next_after_id:loop?'next':null}):page([job()],{next_after_id:'next'})})});
  await controller.setAccount('superdan');await controller.readJobAdmin();loop=true;await assert.rejects(controller.readJobAdmin({after_id:'next'}),/分页/);assert.deepEqual(controller.getState().adminJobs.jobs.map(row=>row.id),['one']);loop=false;await controller.readJobAdmin({after_id:'next'});assert.deepEqual(controller.getState().adminJobs.jobs.map(row=>row.id),['one','two']);
  admin=false;await controller.refresh();assert.equal(controller.getState().adminJobs,null);await assert.rejects(controller.readJobAdmin(),/管理员权限/);controller.destroy();
});

test('an in-flight admin task page cannot restore foreign-owner data after same-account demotion',async()=>{
  let admin=true,resolve;
  const controller=createP4OperatorController({storage:memory(),client:client({state:async()=>({operator:{account:'superdan',admin},nodes:[],groups:[],queue:[]}),adminJobs:()=>new Promise(done=>{resolve=done;})})});
  await controller.setAccount('superdan');const reading=controller.readJobAdmin();admin=false;await controller.refresh();resolve(page());await assert.rejects(reading,/权限已改变/);assert.equal(controller.getState().adminJobs,null);assert.equal(controller.getState().snapshot.operator.admin,false);controller.destroy();
});

test('task row renders any-owner queued selection while disabling running and uncertain work',async()=>{
  const path=fileURLToPath(new URL('./OperatorJobsPanel.jsx',import.meta.url)),contents=await readFile(path,'utf8');
  const bundled=await build({stdin:{contents:contents+"\nexport {I18nProvider as TestI18nProvider} from './i18n/I18nProvider.jsx';\n",resolveDir:path.replace(/[/\\][^/\\]+$/,''),loader:'jsx'},bundle:true,write:false,platform:'node',format:'cjs',packages:'external',loader:{'.css':'empty'}});
  const module=new Module(path);module.filename=path;module.paths=Module._nodeModulePaths(path.replace(/[/\\][^/\\]+$/,''));module._compile(bundled.outputFiles[0].text,path);
  const html=renderWithLocale(module,React.createElement(module.exports.OperatorJobsTable,{jobs:[job('queue'),job('wait',{owner_id:'superdan',status:'waiting_capacity'}),job('running',{status:'running'}),job('uncertain',{status:'reconciling'})]}));
  assert.match(html,/supervan/);assert.match(html,/superdan/);assert.match(html,/aria-label="选择任务 queue"\/>/);assert.match(html,/aria-label="选择任务 wait"\/>/);assert.match(html,/aria-label="选择任务 running" disabled=""/);assert.match(html,/aria-label="选择任务 uncertain" disabled=""/);assert.doesNotMatch(html,/生成|重新提交|启动机器/);
});

test('admin diagnostics retain original execution and notification identities without exposing raw errors',()=>{
  const row=job('held',{status:'recovery_hold',cancellable:false,diagnostic:{reason_code:'original_attempt_requires_reconciliation',age_seconds:185,attempt_no:2,current_attempt_id:'12345678-abcd',dispatch:{event_id:'abcdef12-abcd',state:'unknown'},error_code:'private raw response'}});
  assert.equal(jobStatus(row.status),'原执行待核对');assert.equal(jobStatus('submission_unknown'),'提交结果待核对');
  assert.deepEqual(jobDiagnostics(row),['保留原执行，等待核对','距最近任务更新 3 分钟','原执行 #2 · 12345678','派发 abcdef12 · 通知结果待核对']);
  assert.deepEqual(jobDiagnostics(job()),[]); // Compatible with an older server during rollout.
  assert.deepEqual(jobDiagnostics({diagnostic:{reason_code:'secret raw reason',age_seconds:-1,attempt_no:2,current_attempt_id:'https://private',dispatch:{event_id:'token',state:'secret'}}}),[]);
  assert.equal(canCancelJob(row),false);
});

function renderWithLocale(module,element){
  const descriptor=Object.getOwnPropertyDescriptor(globalThis,'localStorage');
  Object.defineProperty(globalThis,'localStorage',{configurable:true,value:{getItem:()=> 'zh'}});
  try{return renderToStaticMarkup(React.createElement(module.exports.TestI18nProvider,null,element));}
  finally{if(descriptor)Object.defineProperty(globalThis,'localStorage',descriptor);else delete globalThis.localStorage;}
}
