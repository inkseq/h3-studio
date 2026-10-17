import test from 'node:test';
import assert from 'node:assert/strict';
import {createCloudClient} from './cloud-client.js';

const refusal=()=>new Response(JSON.stringify({code:'spending_confirmation_required',plan_id:'original-plan',
  plan_ids:['original-plan','second-plan'],estimate_microusd:100_000}),{status:409,headers:{'Content-Type':'application/json'}});
const ok=value=>new Response(JSON.stringify(value),{status:200,headers:{'Content-Type':'application/json'}});

test('A refused submit waits for explicit approval, then confirms the exact plans and repeats the original key once',async()=>{
  const calls=[];let approve;
  const api=createCloudClient({confirmSpending:()=>new Promise(resolve=>{approve=resolve;}),fetcher:async(path,options)=>{
    calls.push({path,...options});return calls.length===1?refusal():ok({accepted:true});
  }});api.setAccount('one');
  const submitting=api.request('/v1/jobs',{method:'POST',body:{plan_id:'original-plan'},key:'original-key'});
  await new Promise(resolve=>setImmediate(resolve));assert.equal(calls.length,1);
  approve(true);await submitting;assert.equal(calls.length,3);
  assert.equal(calls[1].path,'/v1/account/spending-confirmations');
  assert.deepEqual(JSON.parse(calls[1].body),{plan_ids:['original-plan','second-plan']});
  assert.equal(calls[2].path,calls[0].path);assert.equal(calls[2].body,calls[0].body);
  for(const call of calls)assert.equal(call.headers['X-Expected-Account'],'one');
  assert.equal(calls[2].headers['Idempotency-Key'],'original-key');
});

test('Declining approval creates no confirmation or retry',async()=>{
  let count=0;const api=createCloudClient({confirmSpending:()=>false,fetcher:async()=>{count++;return refusal();}});
  await assert.rejects(api.request('/v1/jobs',{method:'POST',body:{plan_id:'original-plan'},key:'original'}));
  assert.equal(count,1);
});

test('Changing accounts while approval is open cannot confirm or resubmit the old request',async()=>{
  let count=0,approve;const api=createCloudClient({confirmSpending:()=>new Promise(resolve=>{approve=resolve;}),fetcher:async()=>{count++;return refusal();}});
  api.setAccount('one');const submitting=api.request('/v1/jobs',{method:'POST',body:{plan_id:'original-plan'},key:'original'});
  await new Promise(resolve=>setImmediate(resolve));api.setAccount('two');approve(true);
  await assert.rejects(submitting);assert.equal(count,1);
});

test('Mutating the caller body while approval is open cannot change the original submission bytes',async()=>{
  const calls=[];let approve;
  const api=createCloudClient({confirmSpending:()=>new Promise(resolve=>{approve=resolve;}),fetcher:async(path,options)=>{
    calls.push({path,body:options.body,key:options.headers['Idempotency-Key']});return calls.length===1?refusal():ok({accepted:true});
  }});api.setAccount('one');
  const body={plan_id:'original-plan',settings:{selected:'original'}};
  const submitting=api.request('/v1/jobs',{method:'POST',body,key:'original-key'});
  await new Promise(resolve=>setImmediate(resolve));body.plan_id='changed-plan';body.settings.selected='changed';
  approve(true);await submitting;
  assert.equal(calls.length,3);assert.equal(calls[2].body,calls[0].body);assert.equal(calls[2].key,calls[0].key);
  assert.deepEqual(JSON.parse(calls[2].body),{plan_id:'original-plan',settings:{selected:'original'}});
});

test('Abort or timeout during human approval cannot send a confirmation or another submit',async()=>{
  for(const mode of ['abort','timeout']){
    let count=0,approve;const abort=new AbortController();
    const api=createCloudClient({timeoutMs:mode==='timeout'?10:30000,confirmSpending:()=>new Promise(resolve=>{approve=resolve;}),fetcher:async()=>{count++;return refusal();}});
    const submitting=api.request('/v1/jobs',{method:'POST',body:{plan_id:'original-plan'},key:'original',signal:abort.signal});
    await new Promise(resolve=>setImmediate(resolve));
    if(mode==='abort')abort.abort();else await new Promise(resolve=>setTimeout(resolve,25));
    approve(true);await assert.rejects(submitting);assert.equal(count,1,mode);
  }
});

test('Abort during confirmation cancels that original confirmation and never resubmits',async()=>{
  const calls=[],abort=new AbortController();let confirmationStarted;
  const started=new Promise(resolve=>{confirmationStarted=resolve;});
  const api=createCloudClient({confirmSpending:()=>true,fetcher:async(path,options)=>{
    calls.push(path);if(calls.length===1)return refusal();
    return await new Promise((resolve,reject)=>{options.signal.addEventListener('abort',()=>reject(Object.assign(Error('aborted'),{name:'AbortError'})),{once:true});confirmationStarted();});
  }});
  const submitting=api.request('/v1/jobs',{method:'POST',body:{plan_id:'original-plan'},key:'original',signal:abort.signal});
  await started;abort.abort();await assert.rejects(submitting);
  assert.deepEqual(calls,['/v1/jobs','/v1/account/spending-confirmations']);
});
