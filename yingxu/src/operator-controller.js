import {createOperatorClient} from './operator-client.js';

const uid=()=>crypto.randomUUID();
const keyFor=(account,namespace)=>`sixnine:operator:${namespace==='dstack'?'dstack:':''}pending:`+account;
const clone=value=>structuredClone(value);
export function createOperatorController({client=createOperatorClient(),storage=globalThis.localStorage,wait=ms=>new Promise(resolve=>setTimeout(resolve,ms)),now=()=>Date.now(),pendingNamespace='capacity'}={}){
  let epoch=0,loading=false,scanEpoch=0,state={account:null,snapshot:null,catalog:null,pending:null,busy:false,loading:false,refreshingCandidates:false,error:'',denied:false,unavailable:false};
  const listeners=new Set();
  const emit=patch=>{state={...state,...patch};listeners.forEach(fn=>fn());};
  function guard(account,version){if(state.account!==account||epoch!==version)throw Error('登录账户已改变，原账户响应未展示。');}
  function pending(value){if(!state.account||!storage)throw Error('浏览器无法保留操作标识，未发出管理操作。');if(value)storage.setItem(keyFor(state.account,pendingNamespace),JSON.stringify(value));else storage.removeItem(keyFor(state.account,pendingNamespace));emit({pending:value});}
  async function refresh(){
    if(!state.account||loading)return;loading=true;const account=state.account,current=epoch;emit({loading:true});
    try{const [snapshot,catalog]=await Promise.all([client.state(),client.catalog()]);guard(account,current);if(snapshot.operator?.account!==account)throw Error('管理身份与登录账户不一致。');emit({snapshot,catalog,denied:false,unavailable:false,error:''});return snapshot;}
    catch(error){if(current===epoch)emit({error:error.message,denied:error.status===403,unavailable:error.status===404,...([401,403].includes(error.status)?{snapshot:null,catalog:null}:{})});throw error;}
    finally{if(current===epoch){loading=false;emit({loading:false});}}
  }
  async function act(fn){if(state.busy)throw Error('原操作仍在处理中。');const account=state.account,current=epoch;emit({busy:true,error:''});try{const result=await fn();guard(account,current);return result;}catch(error){if(current===epoch)emit({error:error.message});throw error;}finally{if(current===epoch)emit({busy:false});}}
  async function send(operation){
    const account=state.account,current=epoch;
    try{const result=await client[operation.method](...operation.args,operation.body,operation.key);guard(account,current);pending(null);try{await refresh();}catch{}return result;}
    catch(error){guard(account,current);if(error.status>=400&&error.status<500&&![408,429].includes(error.status))pending(null);throw error;}
  }
  function command(method,args,body){return act(async()=>{if(state.pending)throw Error('有原管理操作待核对，请先核对原操作。');const operation={account:state.account,method,args,body:clone(body),key:'operator-'+uid(),created_at:Date.now()};pending(operation);return send(operation);});}
  return {client,subscribe(fn){listeners.add(fn);return()=>listeners.delete(fn);},getState:()=>state,
    async setAccount(account){epoch++;scanEpoch++;client.setAccount(account);loading=false;let saved=null;try{saved=account?JSON.parse(storage?.getItem(keyFor(account,pendingNamespace))||'null'):null;}catch{saved={corrupt:true};}emit({account,snapshot:null,catalog:null,pending:saved,busy:false,loading:false,refreshingCandidates:false,error:'',denied:false,unavailable:false});if(account)try{await refresh();}catch{}},
    refresh,poll:()=>state.busy?Promise.resolve():refresh(),
    preview:selection=>act(()=>client.preview(selection)),
    candidates:query=>act(()=>client.candidates(query)),
    refreshCandidates:async(query,onUpdate=()=>{},signal)=>{
      const account=state.account,current=epoch,scan=++scanEpoch,selection=clone(query);emit({refreshingCandidates:true});
      try{
      const receipt=await client.marketRefresh();guard(account,current);
      if(typeof receipt?.request_id!=='string'||!receipt.request_id||!Number.isFinite(receipt.requested_at)||!['lium','targon'].every(provider=>receipt.providers?.includes(provider)))throw Error('供应商库存刷新尚未确认，请稍后重新查询。');
      const deadline=now()+60000;let last=null;
      for(let read=0;read<31;read++){
        if(signal?.aborted||scan!==scanEpoch||last&&now()>=deadline)return last;
        const result=await client.candidates(selection);guard(account,current);
        if(signal?.aborted||scan!==scanEpoch)return result;
        if(result.model_id!==selection.model_id||result.mode!==selection.mode)throw Error('库存响应与当前模型或模式不一致，请重新查询。');
        if(!['lium','targon'].every(provider=>result.providers?.some(item=>item.provider===provider&&typeof item.refresh_request_id==='string'&&Number.isFinite(item.refresh_requested_at)&&item.refresh_requested_at>=receipt.requested_at&&['pending','complete','failed','timeout'].includes(item.refresh_status))))throw Error('供应商库存刷新尚未返回本轮记录，请重新查询。');
        last=result;
        onUpdate(result);
        if(!result.providers.some(item=>item.refresh_status==='pending')||read===30||now()>=deadline)return result;
        await wait(Math.max(0,Math.min(2000,deadline-now())));guard(account,current);
      }
      }finally{if(current===epoch&&scan===scanEpoch)emit({refreshingCandidates:false});}
    },
    offers:selection=>act(()=>client.offers(selection)),
    start:preview=>command('start',[],{preview_id:preview.preview_id}),
    drain:node=>command('drain',[node.id],{expected_version:node.version}),
    stop:node=>command('stop',[node.id],{expected_version:node.version}),
    hold:(node,seconds)=>command('hold',[node.id],{...(pendingNamespace==='dstack'&&seconds&&typeof seconds==='object'?seconds:{hold_seconds:seconds}),expected_version:node.version}),
    manualReview:(node,attestation)=>command('manualReview',[node.id],{expected_version:node.version,provider_instance_id:node.provider_instance_id,...attestation}),
    extensionPreview:(node,additionalSeconds)=>act(()=>client.extensionPreview(node.id,{expected_version:node.version,additional_seconds:additionalSeconds})),
    extend:preview=>command('extend',[preview.node_id],{preview_id:preview.preview_id}),
    updatePolicy:body=>act(async()=>{if(state.pending)throw Error('请先核对原管理操作。');const result=await client.updatePolicy(body);await refresh();return result;}),
    recover:()=>act(async()=>{const op=state.pending;if(!op||op.corrupt||op.account!==state.account||!['start','drain','stop','manualReview','extend','hold'].includes(op.method)||!Array.isArray(op.args)||typeof op.key!=='string')throw Error('原操作记录无法核对，请保留记录并联系管理员。');return send(op);}),
    destroy(){epoch++;client.reset();listeners.clear();},
  };
}
