import test from 'node:test';
import assert from 'node:assert/strict';
import {createCloudClient} from './cloud-client.js';

const tick=()=>new Promise(resolve=>setImmediate(resolve));
const busy=(headers={})=>new Response('{"detail":"busy"}',{status:429,headers:{'Retry-After':'0',...headers}});

test('native private content URLs freeze the original owner and use only authenticated content routes without fetching',()=>{
  let calls=0;const api=createCloudClient({fetcher:async()=>{calls++;throw Error('native URL construction must not fetch');}});
  assert.throws(()=>api.nativeContentURL('/v1/artifacts/original/content'),/Confirm the signed-in account/);api.setAccount('original-owner');
  const preview=api.nativeContentURL('/v1/artifacts/original/content'),attachment=api.nativeContentURL('/v1/artifacts/original/content',{download:true});
  assert.equal(preview,'/v1/artifacts/original/content?expected_account=original-owner');assert.equal(attachment,preview+'&download=1');assert.equal(api.nativeContentURL('/v1/assets/source/content?download=1'),'/v1/assets/source/content?expected_account=original-owner&download=1');
  api.setAccount('new-owner');assert.equal(new URL(preview,'https://studio.invalid').searchParams.get('expected_account'),'original-owner');assert.equal(new URL(attachment,'https://studio.invalid').searchParams.get('expected_account'),'original-owner');assert.equal(api.nativeContentURL('/v1/artifacts/original/content'),'/v1/artifacts/original/content?expected_account=new-owner');
  api.setAccount('账户 name');assert.equal(new URL(api.nativeContentURL('/v1/assets/source/content'),'https://studio.invalid').searchParams.get('expected_account'),'账户 name');assert.equal(calls,0);
});

test('native private content URLs reject arbitrary API routes, normalized paths and query authority overrides',()=>{
  const api=createCloudClient();api.setAccount('supervan');
  for(const path of ['https://store.invalid/video?signature=secret','//store.invalid/video','/v1/jobs/original','/api/auth/me','/v1/artifacts/original/content/extra','/v1/artifacts/../assets/original/content','/v1/artifacts/%61/content','/v1/artifacts/original/content#fragment','/v1/artifacts/original/content?token=secret','/v1/artifacts/original/content?expected_account=supervan','/v1/artifacts/original/content?expected_account=foreign','/v1/artifacts/original/content?download=1&download=1','/v1/artifacts/original/content?download=0','/v1/artifacts/original/content?download=1&signature=secret'])assert.throws(()=>api.nativeContentURL(path));
  assert.throws(()=>api.nativeContentURL('/v1/artifacts/original/content',{download:'false'}));
});

test('media reads run two at a time with room for bounded control requests',async()=>{
  const calls=[],pending=[];let active=0,downloads=0,maxActive=0,maxDownloads=0;
  const api=createCloudClient({fetcher:(path)=>{
    const media=path.endsWith('/content');calls.push(path);active++;if(media)downloads++;
    maxActive=Math.max(maxActive,active);maxDownloads=Math.max(maxDownloads,downloads);
    return new Promise(resolve=>pending.push(()=>{active--;if(media)downloads--;resolve(new Response(media?'media':'{"projects":[]}'));}));
  }});
  const requests=Array.from({length:6},(_,i)=>api.download(`/v1/artifacts/a${i}/content`));
  requests.push(api.projects(),api.projects(),api.projects());
  assert.equal(calls.length,4);
  while(pending.length){pending.shift()();await tick();}
  await Promise.all(requests);
  assert.equal(calls.length,9);assert.equal(maxDownloads,2);assert.equal(maxActive,4);
});

test('account reset aborts active and queued reads without fetching old queued paths',async()=>{
  const calls=[];
  const api=createCloudClient({fetcher:(path,{signal})=>{
    calls.push(path);return new Promise((resolve,reject)=>signal.addEventListener('abort',()=>{const error=Error('abort');error.name='AbortError';reject(error);},{once:true}));
  }});
  api.setAccount('superdan');
  const old=Array.from({length:8},(_,i)=>api.download(`/v1/artifacts/old${i}/content`));
  const settled=Promise.allSettled(old);assert.equal(calls.length,2);
  api.setAccount('supervan');
  assert.ok((await settled).every(result=>result.status==='rejected'));
  assert.equal(calls.length,2);
});

test('disposing active and queued previews releases their slots without cancelling an explicit download',async()=>{
  const calls=[],pending=new Map(),api=createCloudClient({fetcher:(path,{signal,headers})=>{
    calls.push(path);assert.equal(headers['X-Expected-Account'],'supervan');
    return new Promise((resolve,reject)=>{pending.set(path,()=>resolve(new Response('original media')));signal.addEventListener('abort',()=>reject(new DOMException('aborted','AbortError')),{once:true});});
  }});api.setAccount('supervan');
  const first=new AbortController(),second=new AbortController(),queued=new AbortController();
  const old=[api.download('/v1/artifacts/old-first/content',{signal:first.signal}),api.download('/v1/artifacts/old-second/content',{signal:second.signal}),api.download('/v1/artifacts/old-queued/content',{signal:queued.signal})],settled=Promise.allSettled(old);
  const current=api.download('/v1/artifacts/current/content');assert.equal(calls.length,2);
  queued.abort();first.abort();await tick();assert.ok(calls.includes('/v1/artifacts/current/content'));assert.ok(!calls.includes('/v1/artifacts/old-queued/content'));
  second.abort();pending.get('/v1/artifacts/current/content')();assert.equal(await(await current).text(),'original media');
  assert.ok((await settled).every(result=>result.status==='rejected'));assert.equal(calls.length,3);
});

test('cancelled preview body reads release admission and their late bodies cannot cross an account reset',async()=>{
  const bodies=[],calls=[],api=createCloudClient({fetcher:async(path,{signal})=>{
    calls.push(path);return new Response(new ReadableStream({start(controller){bodies.push(controller);signal.addEventListener('abort',()=>controller.error(new DOMException('aborted','AbortError')),{once:true});}}));
  }});api.setAccount('supervan');
  const first=new AbortController(),second=new AbortController();
  const old=[api.download('/v1/artifacts/body-first/content',{signal:first.signal}),api.download('/v1/artifacts/body-second/content',{signal:second.signal})],settled=Promise.allSettled(old);await tick();
  const current=api.download('/v1/artifacts/current/content'),currentRejected=assert.rejects(current);assert.equal(calls.length,2);
  first.abort();await tick();assert.equal(calls.length,3);second.abort();api.setAccount('other-owner');await currentRejected;
  assert.ok((await settled).every(result=>result.status==='rejected'));assert.equal(calls.length,3);
});

test('an already cancelled preview never fetches and completed reads detach the caller cancellation listener',async()=>{
  let calls=0,requestSignal;const api=createCloudClient({fetcher:async(_path,{signal})=>{calls++;requestSignal=signal;return new Response('media');}}),cancelled=new AbortController();cancelled.abort();
  await assert.rejects(api.download('/v1/artifacts/never/content',{signal:cancelled.signal}));assert.equal(calls,0);
  const completed=new AbortController();assert.equal(await(await api.download('/v1/artifacts/done/content',{signal:completed.signal})).text(),'media');completed.abort();assert.equal(requestSignal.aborted,false);assert.equal(calls,1);
});

test('GET and media timeouts describe reads while a submit timeout keeps its original uncertainty warning',async()=>{
  const calls=[],api=createCloudClient({timeoutMs:5,downloadTimeoutMs:5,fetcher:(path,{signal})=>{
    calls.push(path);return new Promise((_,reject)=>signal.addEventListener('abort',()=>reject(new DOMException('aborted','AbortError')),{once:true}));
  }});
  await assert.rejects(api.projects(),error=>error.message.includes('cloud read timed out')&&!error.message.includes('Submission may have completed'));
  await assert.rejects(api.download('/v1/artifacts/original/content'),error=>error.message.includes('media read timed out')&&!error.message.includes('Submission may have completed'));
  await assert.rejects(api.submit('original-plan','original-key'),/Submission may have completed/);assert.equal(calls.length,3);
});

test('429 GET retry keeps exact path and owner and stops after three attempts',async()=>{
  const calls=[];const api=createCloudClient({fetcher:async(path,options)=>{calls.push([path,options.headers['X-Expected-Account']]);return calls.length===1?busy():new Response('{"projects":[]}');}});
  api.setAccount('superdan');await api.projects();
  assert.deepEqual(calls,[['/v1/projects?limit=100&offset=0','superdan'],['/v1/projects?limit=100&offset=0','superdan']]);
  let count=0;const unavailable=createCloudClient({fetcher:async()=>{count++;return busy();}});
  await assert.rejects(unavailable.projects(),error=>error.status===429);assert.equal(count,3);
});

test('writes, network failures and non-429 responses are never automatically retried',async()=>{
  let count=0,last;
  const api=createCloudClient({fetcher:async(path,options)=>{count++;last={path,options};return busy();}});
  await assert.rejects(api.submit('plan-one','original-intent'),error=>error.status===429);
  assert.equal(count,1);assert.equal(last.options.headers['Idempotency-Key'],'original-intent');
  for(const response of [()=>new Response('{}',{status:503}),()=>{throw Error('offline');}]){
    let attempts=0;const read=createCloudClient({fetcher:async()=>{attempts++;return response();}});
    await assert.rejects(read.projects());assert.equal(attempts,1);
  }
});

test('reset interrupts a Retry-After wait and foreign-owner errors are not retried',async()=>{
  let calls=0;const api=createCloudClient({fetcher:async()=>{calls++;return busy({'Retry-After':'30'});},timeoutMs:60000});
  const pending=api.projects();await tick();api.reset();await assert.rejects(pending);assert.equal(calls,1);
  let foreignCalls=0,unauthorized=0;
  const foreign=createCloudClient({fetcher:async()=>{foreignCalls++;return busy({'X-Authenticated-Account':'supervan'});},onUnauthorized:()=>unauthorized++});
  foreign.setAccount('superdan');await assert.rejects(foreign.projects(),error=>error.status===401);
  assert.equal(foreignCalls,1);assert.equal(unauthorized,1);
});

test('oversized Retry-After is surfaced instead of retrying sooner than the server requested',async()=>{
  let attempts=0;
  const api=createCloudClient({fetcher:async()=>{attempts++;return busy({'Retry-After':'120'});}});
  await assert.rejects(api.projects(),error=>error.status===429);assert.equal(attempts,1);
});
