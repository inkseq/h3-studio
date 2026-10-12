import {createP4OperatorClient} from './p4-operator-client.js';

const pendingKey=account=>'sixnine:operator:dstack:pending:'+account;
const methods=new Set(['start','stop','hold','createGroup','move','groupStop','groupHold']);
/** UI command journal only. Runtime, jobs, groups and accounting remain API-owned. */
export function createP4OperatorController({client=createP4OperatorClient(),storage=globalThis.localStorage}={}){
  let epoch=0,reading=false,state={account:null,snapshot:null,catalog:null,adminSnapshot:null,pending:null,busy:false,loading:false,error:'',denied:false};
  const listeners=new Set(),emit=patch=>{state={...state,...patch};listeners.forEach(fn=>fn());};
  const guard=(account,version)=>{if(account!==state.account||version!==epoch)throw Error('登录账户已改变，原响应未展示。');};
  function pending(value){if(!storage||!state.account)throw Error('无法保留原操作标识，未提交操作。');if(value)storage.setItem(pendingKey(state.account),JSON.stringify(value));else storage.removeItem(pendingKey(state.account));emit({pending:value});}
  async function refresh(admin=false){
    if(!state.account||reading)return;
    reading=true;const account=state.account,version=epoch;emit({loading:true});
    try{const [snapshot,catalog]=await Promise.all([client.state(),client.catalog()]);guard(account,version);if(snapshot.operator?.account!==account)throw Error('控制台身份与登录账户不一致。');emit({snapshot,catalog,denied:false,error:'',...(snapshot.operator?.admin!==true?{adminSnapshot:null}:{})});if(admin&&snapshot.operator?.admin===true){const value=await client.adminState();guard(account,version);if(value.operator?.account!==account||value.operator?.admin!==true)throw Error('管理员身份尚未确认。');emit({adminSnapshot:value});}return snapshot;}
    catch(error){if(version===epoch)emit({error:error.message,denied:error.status===403,...([401,403].includes(error.status)?{snapshot:null,catalog:null,adminSnapshot:null}:{})});throw error;}
    finally{if(version===epoch){reading=false;emit({loading:false});}}
  }
  async function readAdmin(){const account=state.account,version=epoch;if(state.snapshot?.operator?.admin!==true)throw Error('没有管理员权限。');try{const value=await client.adminState();guard(account,version);if(value.operator?.account!==account||value.operator?.admin!==true)throw Error('管理员身份尚未确认。');emit({adminSnapshot:value});return value;}catch(error){if(version===epoch)emit({error:error.message,adminSnapshot:null,...(error.status===403&&state.snapshot?{snapshot:{...state.snapshot,operator:{...state.snapshot.operator,admin:false}}}:{})});throw error;}}
  async function act(fn){if(state.busy)throw Error('原操作仍在处理中。');const account=state.account,version=epoch;emit({busy:true,error:''});try{const value=await fn();guard(account,version);return value;}catch(error){if(version===epoch)emit({error:error.message});throw error;}finally{if(version===epoch)emit({busy:false});}}
  async function send(operation){const account=state.account,version=epoch;try{const value=await client[operation.method](...operation.args,operation.body,operation.key);guard(account,version);pending(null);try{await refresh(!!state.adminSnapshot);}catch{}return value;}catch(error){guard(account,version);if(error.status>=400&&error.status<500&&![408,429].includes(error.status))pending(null);if(error.status===409&&(error.detail?.detail==='operator_node_version_conflict'||error.detail?.code==='operator_node_version_conflict'||error.message==='operator_node_version_conflict')){try{await refresh(!!state.adminSnapshot);}catch{}guard(account,version);error.message='机器状态刚刚更新，本次操作未接受。请重试（operator_node_version_conflict）。';}throw error;}}
  const command=(method,args,body,prepare)=>act(async()=>{if(state.pending)throw Error('原操作结果尚未确认，请先核对原操作。');const account=state.account,version=epoch;const prepared=prepare?await prepare(account,version):body;guard(account,version);const operation={account,method,args,body:structuredClone(prepared),key:'operator-'+crypto.randomUUID(),created_at:Date.now()};pending(operation);return send(operation);});
  async function freshNode(node,account,version){
    if(typeof node?.id!=='string'||!node.id)throw Error('原机器身份尚未确认。');
    const admin=typeof node.owner_id==='string'&&node.owner_id!==account;
    if(admin&&state.snapshot?.operator?.admin!==true)throw Error('没有跨账户管理权限。');
    const snapshot=await (admin?client.adminState():client.state());guard(account,version);
    if(snapshot.operator?.account!==account||admin&&snapshot.operator?.admin!==true)throw Error('管理身份已改变，未提交机器操作。');
    const fresh=snapshot.nodes?.find(value=>(value.id||value.node_id)===node.id);
    if(!fresh||fresh.owner_id!==(admin?node.owner_id:account)||typeof fresh.version!=='number'||!Number.isFinite(fresh.version))throw Error('原机器身份或当前版本尚未确认，未提交操作。');
    emit(admin?{adminSnapshot:snapshot}:{snapshot});
    return fresh;
  }
  const nodeCommand=(method,node,body)=>command(method,[node.id],body,async(account,version)=>({...body,expected_version:(await freshNode(node,account,version)).version}));
  return {client,subscribe(fn){listeners.add(fn);return()=>listeners.delete(fn);},getState:()=>state,
    async setAccount(account){epoch++;reading=false;client.setAccount(account);let saved=null;try{saved=account?JSON.parse(storage?.getItem(pendingKey(account))||'null'):null;}catch{saved={corrupt:true};}emit({account,snapshot:null,catalog:null,adminSnapshot:null,pending:saved,busy:false,loading:false,error:'',denied:false});if(account)try{await refresh();}catch{}},
    refresh,readAdmin,poll:admin=>state.busy?Promise.resolve():refresh(admin),preview:body=>client.preview(body),stopImpact:(kind,target)=>client.stopImpact(kind,target),
    async metrics(nodeId){const account=state.account,version=epoch;const value=await client.metrics(nodeId);guard(account,version);if(value?.node_id!==nodeId)throw Error('机器统计身份尚未确认。');return value;},
    start:preview=>command('start',[],{preview_id:preview.preview_id}),
    stop:(node,impact)=>nodeCommand('stop',node,{...(impact?.warnings?.length?{acknowledge_capacity_loss:impact.fingerprint}:{})}),
    hold:(node,body)=>nodeCommand('hold',node,body),
    createGroup:name=>command('createGroup',[],{name}),
    move:(node,groupId)=>nodeCommand('move',node,{group_id:groupId}),
    groupStop:(group,impact)=>command('groupStop',[group.id],{expected_version:group.version,...(impact?.warnings?.length?{acknowledge_capacity_loss:impact.fingerprint}:{})}),
    groupHold:(group,body)=>command('groupHold',[group.id],{...body,expected_version:group.version}),
    recover:()=>act(async()=>{const original=state.pending;if(!original||original.corrupt||original.account!==state.account||!methods.has(original.method)||!Array.isArray(original.args)||typeof original.key!=='string')throw Error('原操作记录无法核对，请保留并联系管理员。');return send(original);}),
    destroy(){epoch++;client.reset();listeners.clear();},
  };
}
