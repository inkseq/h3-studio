/* Same-origin application API only. This module never holds provider credentials. */
export class CloudError extends Error {
  constructor(message,{status=0,detail=null}={}){super(message);this.name='CloudError';this.status=status;this.detail=detail;}
}
export function safeCloudPath(path){
  if(typeof path!=='string'||!/^\/(?:v1\/|api\/auth\/)/.test(path)||path.includes('\\')||/[\r\n]/.test(path))throw new CloudError('服务返回的地址不在工作室 API 范围内。');
  const parsed=new URL(path,'https://studio.invalid');
  if(parsed.origin!=='https://studio.invalid'||parsed.username||parsed.password||parsed.hash||!/^\/(?:v1\/|api\/auth\/)/.test(parsed.pathname))throw new CloudError('服务地址无效。');
  return parsed.pathname+parsed.search;
}

function abortError(){const error=new Error('Aborted');error.name='AbortError';return error;}
function waitForRetry(ms,signal){
  return new Promise((resolve,reject)=>{
    if(signal.aborted){reject(abortError());return;}
    const stopped=()=>{clearTimeout(timer);signal.removeEventListener('abort',stopped);reject(abortError());};
    const timer=setTimeout(()=>{signal.removeEventListener('abort',stopped);resolve();},ms);
    signal.addEventListener('abort',stopped,{once:true});
  });
}

function readAdmission(){
  let active=0,downloads=0;const waiting=[];
  const room=blob=>active<4&&(!blob||downloads<2);
  function begin(blob){active++;if(blob)downloads++;let released=false;return()=>{if(released)return;released=true;active--;if(blob)downloads--;drain();};}
  function drain(){for(let i=0;i<waiting.length;){const entry=waiting[i];if(!room(entry.blob)){i++;continue;}waiting.splice(i,1);entry.signal.removeEventListener('abort',entry.cancel);entry.resolve(begin(entry.blob));}}
  return (blob,signal)=>{
    if(signal.aborted)throw abortError();
    if(room(blob))return begin(blob);
    if(waiting.length>=128)throw new CloudError('待读取的素材过多，请等当前内容加载完再打开更多内容。',{status:429});
    return new Promise((resolve,reject)=>{
      const entry={blob,signal,resolve,reject,cancel:()=>{const at=waiting.indexOf(entry);if(at>=0)waiting.splice(at,1);signal.removeEventListener('abort',entry.cancel);reject(abortError());}};
      waiting.push(entry);signal.addEventListener('abort',entry.cancel,{once:true});
    });
  };
}

function retryDelay(response){
  const raw=response.headers.get('Retry-After');
  if(!raw)return 3000;
  const seconds=/^\d+(?:\.\d+)?$/.test(raw)?Number(raw):NaN;
  const delay=Number.isFinite(seconds)?seconds*1000:Date.parse(raw)-Date.now();
  return Number.isFinite(delay)&&delay>=0&&delay<=30000?delay:null;
}

export function createCloudClient({fetcher=(...args)=>fetch(...args),onUnauthorized=()=>{},timeoutMs=30000,uploadTimeoutMs=900000,downloadTimeoutMs=300000}={}){
  let epoch=0,account=null;const inflight=new Set(),admitRead=readAdmission();
  function reset(){epoch++;for(const controller of inflight)controller.abort();inflight.clear();}
  async function request(path,{method='GET',body,key,blob=false,anonymous=false,signal}={}){
    const target=safeCloudPath(path),current=epoch,expectedAccount=account,controller=new AbortController();inflight.add(controller);
    const cancelRead=()=>controller.abort();
    if(signal?.aborted)controller.abort();else signal?.addEventListener('abort',cancelRead,{once:true});
    const multipart=typeof FormData!=='undefined'&&body instanceof FormData;
    const headers={...(body!==undefined&&!multipart?{'Content-Type':'application/json'}:{}),...(key?{'Idempotency-Key':key}:{}),...(expectedAccount&&!anonymous?{'X-Expected-Account':expectedAccount}:{})};
    let timedOut=false,releaseRead=null;const duration=blob?downloadTimeoutMs:multipart?uploadTimeoutMs:timeoutMs,deadline=Date.now()+duration;
    const timer=setTimeout(()=>{timedOut=true;controller.abort();},duration);
    try{
      // Only reads are queued/retried. A write retains its original explicit
      // confirmation/idempotency behavior, including after a 429 or timeout.
      if(method==='GET'){
        const slot=admitRead(blob,controller.signal);
        releaseRead=typeof slot==='function'?slot:await slot;
      }
      for(let attempt=0;;attempt++){
      if(current!==epoch)throw new CloudError('登录账户已改变，请重新打开项目。');
      if(controller.signal.aborted)throw abortError();
      const response=await fetcher(target,{method,headers,credentials:'same-origin',cache:'no-store',signal:controller.signal,...(body!==undefined?{body:multipart?body:JSON.stringify(body)}:{})});
      if(current!==epoch)throw new CloudError('登录账户已改变，请重新打开项目。');
      const responseAccount=response.headers.get('X-Authenticated-Account');
      if(expectedAccount&&!anonymous&&responseAccount&&responseAccount!==expectedAccount){onUnauthorized();throw new CloudError('登录身份已改变。旧账户响应未展示或写入，请重新打开当前账户项目。',{status:401});}
      if(!response.ok){let detail;try{detail=await response.json();}catch{detail=null;}
        if(detail?.code==='account_context_changed'){onUnauthorized();throw new CloudError('登录身份已改变。请求被阻止，旧账户草稿仍保留。',{status:401});}
        if(response.status===401&&!anonymous)onUnauthorized();
        if(method==='GET'&&response.status===429&&attempt<2){
          const delay=retryDelay(response);
          if(delay!==null&&Date.now()+delay<deadline){await waitForRetry(delay,controller.signal);continue;}
        }
        const reason=response.status===409&&detail?.detail==='capacity_drain_repreflight'?'GPU 正在休眠或切换，任务尚未提交。请重新检查后生成；作品和素材已保留。':typeof detail?.detail==='string'?detail.detail:typeof detail?.message==='string'?detail.message:response.status===409?'云端版本已改变。你的草稿已保留，请先比较新版本。':response.status===404?'此云功能尚未连接，或该内容不存在。':response.status===401?'请重新登录云工作室。':`服务暂未完成请求（${response.status}）。`;
        throw new CloudError(reason,{status:response.status,detail});
      }
      const result=blob?await response.blob():response.status===204?null:await response.json();
      if(current!==epoch)throw new CloudError('登录账户已改变，请重新打开项目。');
      return result;
      }
    }catch(error){if(error instanceof CloudError)throw error;if(timedOut)throw new CloudError(multipart?'上传等待超时。请先到“素材状态与存储用量”核对是否已收到文件，再决定恢复；未自动重新上传。':method==='GET'?(blob?'媒体读取等待超时。请重试预览或下载原文件；不会重新提交生成。':'云端内容读取等待超时。请刷新原记录；不会重新提交生成。'):'请求等待超时，作品已保留。提交是否成功仍需核对；不会自动重投，请刷新原任务或使用原标识恢复。');if(error.name==='AbortError')throw new CloudError('操作已结束；未确认的提交请从任务列表恢复。');throw new CloudError('无法连接云工作室。作品仍保留，请检查连接后重试；不会自动重新提交生成。');}
    finally{clearTimeout(timer);signal?.removeEventListener('abort',cancelRead);inflight.delete(controller);if(releaseRead)releaseRead();}
  }
  function nativeContentURL(path,{download=false}={}){
    if(!account)throw new CloudError('请先核对登录账户，再读取私有媒体。');
    if(typeof path!=='string'||!/^\/v1\/(?:assets|artifacts)\/[A-Za-z0-9_-]{1,200}\/content$/.test(path.split('?')[0])||typeof download!=='boolean')throw new CloudError('私有媒体地址不在工作室内容接口范围内。');
    const parsed=new URL(safeCloudPath(path),'https://studio.invalid'),entries=[...parsed.searchParams];
    if(entries.length>1||entries.some(([key,value])=>key!=='download'||value!=='1'))throw new CloudError('私有媒体地址包含不支持的查询参数。');
    const query=new URLSearchParams({expected_account:account,...(download||entries.length?{download:'1'}:{})});
    return parsed.pathname+'?'+query;
  }
  const id=value=>encodeURIComponent(value);
  return {request,reset,setAccount(value){const next=typeof value==='string'&&value?value:null;if(next!==account){account=next;reset();}},
    authConfig:()=>request('/api/auth/config',{anonymous:true}),me:()=>request('/api/auth/me',{anonymous:true}),
    login:(username,password)=>request('/api/auth/login',{method:'POST',body:{username,password},anonymous:true}),
    logout:()=>request('/api/auth/logout',{method:'POST',anonymous:true}),
    projects:({limit=100,offset=0}={})=>request(`/v1/projects?limit=${limit}&offset=${offset}`),createProject:(project,key)=>request('/v1/projects',{method:'POST',body:{project},key}),
    project:projectId=>request(`/v1/projects/${id(projectId)}`),saveProject:(projectId,project,version)=>request(`/v1/projects/${id(projectId)}`,{method:'PUT',body:{expected_version:version,project}}),
    capabilities:()=>request('/v1/capabilities'),
    generationDraft:(projectId,shotId)=>request(`/v1/projects/${id(projectId)}/shots/${id(shotId)}/generation-draft`),
    savedPlan:(projectId,shotId,body,key)=>request(`/v1/projects/${id(projectId)}/shots/${id(shotId)}/generation-plans`,{method:'POST',body,key}),
    saveFrame:(projectId,artifactId,body,key)=>request(`/v1/projects/${id(projectId)}/artifacts/${id(artifactId)}/frames`,{method:'POST',body,key}),
    generationAvailability:()=>request('/v1/generation-availability'),
    changePassword:(old_password,new_password)=>request('/v1/auth/password',{method:'POST',body:{old_password,new_password}}),
    apiKeys:()=>request('/v1/api-keys'),createApiKey:body=>request('/v1/api-keys',{method:'POST',body}),revokeApiKey:keyId=>request(`/v1/api-keys/${id(keyId)}`,{method:'DELETE'}),
    projectMeta:projectId=>request(`/v1/projects/${id(projectId)}/meta`),
    projectActivity:(projectId,{limit=50,beforeVersion}={})=>request(`/v1/projects/${id(projectId)}/activity?limit=${limit}${beforeVersion===undefined?'':`&before_version=${beforeVersion}`}`),
    upload:(file,projectId,assetId,key)=>{const body=new FormData();body.append('file',file,file.name||'reference');body.append('client_project_id',projectId);if(assetId)body.append('client_asset_id',assetId);return request('/v1/assets',{method:'POST',body,key});},
    asset:assetId=>request(`/v1/assets/${id(assetId)}`),
    assets:projectId=>request(`/v1/assets?client_project_id=${id(projectId)}`),
    storageUsage:()=>request('/v1/storage-usage'),
    resumeAsset:assetId=>request(`/v1/assets/${id(assetId)}/resume`,{method:'POST'}),
    derivative:(assetId,range,key)=>request(`/v1/assets/${id(assetId)}/derivatives`,{method:'POST',body:{start:range.start,end:range.end},key}),
    plan:(payload,key)=>request('/v1/generation-plans',{method:'POST',body:payload,key}),
    renderPlan:payload=>request('/v1/render-plans',{method:'POST',body:payload}),
    submit:(planId,key)=>request('/v1/jobs',{method:'POST',body:{plan_id:planId},key}),
    jobs:(projectId,{limit=100,offset=0}={})=>request(`/v1/jobs?client_project_id=${id(projectId)}&limit=${limit}&offset=${offset}`),activitySummary:projectId=>request(`/v1/activity-summary?client_project_id=${id(projectId)}`),job:jobId=>request(`/v1/jobs/${id(jobId)}`),
    cancel:jobId=>request(`/v1/jobs/${id(jobId)}/cancel`,{method:'POST'}),
    batch:(projectId,planIds,key)=>request('/v1/batches',{method:'POST',body:{client_project_id:projectId,plan_ids:planIds},key}),
    batches:(projectId,{limit=10,offset=0}={})=>request(`/v1/batches?client_project_id=${id(projectId)}&limit=${limit}&offset=${offset}`),
    getBatch:batchId=>request(`/v1/batches/${id(batchId)}`),cancelBatch:batchId=>request(`/v1/batches/${id(batchId)}/cancel`,{method:'POST'}),
    artifacts:jobId=>request(`/v1/jobs/${id(jobId)}/artifacts`),
    nativeContentURL,
    download:(path,{signal}={})=>request(path,{blob:true,signal}),
  };
}
