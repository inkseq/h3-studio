import {createP4OperatorClient} from './p4-operator-client.js';
import {cancellationBody,cancellationReceipt,validAdminJobs,validCancellationBody} from './operator-jobs-model.js';
import {hostPolicyBody,hostPolicyReceipt,validHostAudit,validHostPage,validHostPolicyBody} from './operator-host-model.js';

const pendingKey=account=>'sixnine:operator:dstack:pending:'+account;
const methods=new Set(['start','stop','hold','createGroup','move','groupStop','groupHold','updateBudget','updatePrice','cancelJobs','updateHostPolicy']);
// These application admission refusals happen before a rental is accepted.
// A bare/provider 429 or an unknown write response must keep its original key.
const rejectedStart=(operation,error)=>operation.method==='start'&&error.status===429&&
  ['budget_exceeded','budget_not_configured','capacity_unavailable'].includes(error.detail?.code);
/** UI command journal only. Runtime, jobs, groups and accounting remain API-owned. */
export function createP4OperatorController({client=createP4OperatorClient(),storage=globalThis.localStorage}={}){
  let epoch=0,reading=false,budgetReading=0,jobReading=0,hostReading={owner:0,admin:0,audit:0,target:0},state={account:null,snapshot:null,catalog:null,hosts:null,adminSnapshot:null,adminBudgets:null,adminPrices:null,budgetAudits:null,adminJobs:null,adminHosts:null,hostAudits:null,hostTarget:null,pending:null,busy:false,loading:false,error:'',denied:false};
  const listeners=new Set(),emit=patch=>{state={...state,...patch};listeners.forEach(fn=>fn());};
  const guard=(account,version)=>{if(account!==state.account||version!==epoch)throw Error('登录账户已改变，原响应未展示。');};
  function pending(value){if(!storage||!state.account)throw Error('无法保留原操作标识，未提交操作。');if(value)storage.setItem(pendingKey(state.account),JSON.stringify(value));else storage.removeItem(pendingKey(state.account));emit({pending:value});}
  async function refresh(admin=false){
    if(!state.account||reading)return;
    reading=true;const account=state.account,version=epoch;emit({loading:true});
    try{const [snapshot,catalog]=await Promise.all([client.state(),client.catalog()]);guard(account,version);if(snapshot.operator?.account!==account)throw Error('控制台身份与登录账户不一致。');emit({snapshot,catalog,denied:false,error:'',...(snapshot.operator?.admin!==true?{adminSnapshot:null,adminBudgets:null,adminPrices:null,budgetAudits:null,adminJobs:null,adminHosts:null,hostAudits:null,hostTarget:null}:{})});if(admin&&snapshot.operator?.admin===true){const value=await client.adminState();guard(account,version);if(value.operator?.account!==account||value.operator?.admin!==true)throw Error('管理员身份尚未确认。');emit({adminSnapshot:value});}return snapshot;}
    catch(error){if(version===epoch)emit({error:error.message,denied:error.status===403,...([401,403].includes(error.status)?{snapshot:null,catalog:null,hosts:null,adminSnapshot:null,adminBudgets:null,adminPrices:null,budgetAudits:null,adminJobs:null,adminHosts:null,hostAudits:null,hostTarget:null}:{})});throw error;}
    finally{if(version===epoch){reading=false;emit({loading:false});}}
  }
  async function readAdmin(){const account=state.account,version=epoch;if(state.snapshot?.operator?.admin!==true)throw Error('没有管理员权限。');try{const value=await client.adminState();guard(account,version);if(value.operator?.account!==account||value.operator?.admin!==true)throw Error('管理员身份尚未确认。');emit({adminSnapshot:value});return value;}catch(error){if(version===epoch)emit({error:error.message,adminSnapshot:null,adminBudgets:null,adminPrices:null,budgetAudits:null,adminJobs:null,adminHosts:null,hostAudits:null,hostTarget:null,...(error.status===403&&state.snapshot?{snapshot:{...state.snapshot,operator:{...state.snapshot.operator,admin:false}}}:{})});throw error;}}
  async function readBudgetAdmin({after_id}={}){
    const account=state.account,version=epoch,request=++budgetReading,prior=state.adminBudgets;
    if(state.snapshot?.operator?.admin!==true)throw Error('没有管理员权限。');
    if(after_id&&prior?.next_after_id!==after_id)throw Error('原预算分页已改变，请刷新。');
    try{
      const [page,prices,audits]=await Promise.all([client.adminBudgets({after_id}),after_id?Promise.resolve(state.adminPrices):client.adminPrices(),after_id?Promise.resolve(state.budgetAudits):client.budgetAudit()]);
      guard(account,version);
      if(state.snapshot?.operator?.admin!==true)throw Object.assign(Error('管理员权限已改变，原预算响应未展示。'),{status:403});
      if(request!==budgetReading)throw Error('较新的预算读取已完成，原响应未展示。');
      if(page.account!==account||page.admin!==true||prices?.operator?.account!==account||prices.operator.admin!==true||!Array.isArray(audits?.events))throw Error('管理员身份已改变，原预算响应未展示。');
      if(after_id&&(state.adminBudgets!==prior||page.next_after_id===after_id))throw Error('原预算分页已改变，请刷新。');
      const budgets=after_id?{...page,budgets:[...new Map([...prior.budgets,...page.budgets].map(row=>[row.id,row])).values()]}:page;
      emit({adminBudgets:budgets,adminPrices:prices,budgetAudits:audits});return {budgets,prices,audits};
    }catch(error){if(version===epoch&&request===budgetReading)emit({adminBudgets:null,adminPrices:null,budgetAudits:null,adminJobs:null,adminHosts:null,hostAudits:null,hostTarget:null,error:error.message,...(error.status===403&&state.snapshot?{snapshot:{...state.snapshot,operator:{...state.snapshot.operator,admin:false}}}:{})});throw error;}
  }
  async function readJobAdmin({after_id}={}){
    const account=state.account,version=epoch,request=++jobReading;
    if(state.snapshot?.operator?.admin!==true)throw Error('没有管理员权限。');
    const prior=state.adminJobs;
    if(after_id&&prior?.next_after_id!==after_id)throw Error('原任务分页已改变，请刷新任务。');
    try{const value=await client.adminJobs({after_id});guard(account,version);if(state.snapshot?.operator?.admin!==true)throw Object.assign(Error('管理员权限已改变，原任务响应未展示。'),{status:403});if(request!==jobReading)throw Error('较新的任务读取已完成，原响应未展示。');if(!validAdminJobs(value,account))throw Error('管理员任务身份或原状态尚未确认。');if(after_id&&(state.adminJobs!==prior||value.next_after_id===after_id))throw Error('原任务分页已改变，请刷新任务。');emit({adminJobs:after_id?{...value,jobs:[...new Map([...prior.jobs,...value.jobs].map(job=>[job.id,job])).values()]}:value});return value;}
    catch(error){if(version===epoch&&request===jobReading)emit({error:error.message,...([401,403].includes(error.status)?{adminJobs:null,adminHosts:null,hostAudits:null,hostTarget:null}:{}),...(error.status===403&&state.snapshot?{snapshot:{...state.snapshot,operator:{...state.snapshot.operator,admin:false}}}:{})});throw error;}
  }
  async function readHosts({admin=false,offset=0}={}){
    const account=state.account,version=epoch,key=admin?'admin':'owner',request=++hostReading[key],field=admin?'adminHosts':'hosts',prior=state[field];
    if(!account||admin&&state.snapshot?.operator?.admin!==true)throw Error('没有读取主机记录的权限。');
    if(!Number.isSafeInteger(offset)||offset<0||offset&&(prior?.has_more!==true||offset!==prior.offset+prior.limit))throw Error('原主机分页已改变，请刷新。');
    try{const value=await client.hosts({admin,offset});guard(account,version);if(admin&&state.snapshot?.operator?.admin!==true)throw Object.assign(Error('管理员权限已改变，原主机响应未展示。'),{status:403});if(request!==hostReading[key])throw Error('较新的主机读取已完成，原响应未展示。');if(!validHostPage(value,account,{admin})||value.offset!==offset)throw Error('主机观测身份或分页尚未确认。');if(offset&&state[field]!==prior)throw Error('原主机分页已改变，请刷新。');emit({[field]:offset?{...value,hosts:[...new Map([...prior.hosts,...value.hosts].map(host=>[String(host.machine_id),host])).values()]}:value});return value;}
    catch(error){if(version===epoch&&request===hostReading[key])emit({error:error.message,...([401,403].includes(error.status)?{[field]:null,adminHosts:null,hostAudits:null,hostTarget:null}:{}),...(error.status===403&&admin&&state.snapshot?{snapshot:{...state.snapshot,operator:{...state.snapshot.operator,admin:false}}}:{})});throw error;}
  }
  async function readHostTarget(machine_id){
    const account=state.account,version=epoch,request=++hostReading.target;
    if(state.snapshot?.operator?.admin!==true||typeof machine_id!=='string'||!/^[1-9][0-9]{0,17}$/.test(machine_id))throw Error('仅管理员可查询明确的物理 machine_id。');
    try{const value=await client.hosts({admin:true,machine_id,offset:0});guard(account,version);if(state.snapshot?.operator?.admin!==true)throw Object.assign(Error('管理员权限已改变，原策略未展示。'),{status:403});if(request!==hostReading.target||!validHostPage(value,account,{admin:true})||value.hosts.length!==1||String(value.hosts[0].machine_id)!==machine_id)throw Error('原物理主机策略身份尚未确认。');emit({hostTarget:value.hosts[0]});return value.hosts[0];}
    catch(error){if(version===epoch&&request===hostReading.target)emit({hostTarget:null,error:error.message,...([401,403].includes(error.status)?{adminHosts:null,hostAudits:null}:{}),...(error.status===403&&state.snapshot?{snapshot:{...state.snapshot,operator:{...state.snapshot.operator,admin:false}}}:{})});throw error;}
  }
  async function readHostAudit({offset=0}={}){
    const account=state.account,version=epoch,request=++hostReading.audit,prior=state.hostAudits;
    if(state.snapshot?.operator?.admin!==true)throw Error('没有管理员权限。');
    if(!Number.isSafeInteger(offset)||offset<0||offset&&(prior?.has_more!==true||offset!==prior.offset+prior.limit))throw Error('原主机审核分页已改变，请刷新。');
    try{const value=await client.hostAudit({offset});guard(account,version);if(state.snapshot?.operator?.admin!==true)throw Object.assign(Error('管理员权限已改变，原审核响应未展示。'),{status:403});if(request!==hostReading.audit||!validHostAudit(value,account)||value.offset!==offset||offset&&state.hostAudits!==prior)throw Error('主机审核身份或分页尚未确认。');emit({hostAudits:offset?{...value,events:[...new Map([...prior.events,...value.events].map(event=>[event.id,event])).values()]}:value});return value;}
    catch(error){if(version===epoch&&request===hostReading.audit)emit({error:error.message,...([401,403].includes(error.status)?{adminHosts:null,hostAudits:null,hostTarget:null}:{}),...(error.status===403&&state.snapshot?{snapshot:{...state.snapshot,operator:{...state.snapshot.operator,admin:false}}}:{})});throw error;}
  }
  async function reconcilePolicy(operation){
    const account=state.account,version=epoch;
    if(state.snapshot?.operator?.admin!==true)throw Error('没有管理员权限，原修改仍待核对。');
    let row;
    if(operation.method==='updateBudget'){
      let cursor;const seen=new Set();
      for(let page=0;page<10;page++){const value=await client.adminBudgets({after_id:cursor});guard(account,version);if(value.account!==account||value.admin!==true)throw Error('管理员身份尚未确认。');row=value.budgets.find(budget=>budget.id===operation.args[0]);if(row||!value.next_after_id)break;if(seen.has(value.next_after_id))throw Error('预算分页尚未确认。');seen.add(value.next_after_id);cursor=value.next_after_id;}
      if(!row||row.limit_microusd!==operation.body.limit_microusd||row.snapshot===operation.body.expected_snapshot)throw Error('原额度修改尚未证实，保留原操作；请刷新审核记录，不重复修改。');
    }else{
      const value=await client.adminPrices();guard(account,version);if(value.operator?.account!==account||value.operator.admin!==true)throw Error('管理员身份尚未确认。');row=value.profiles.find(profile=>profile.id===operation.args[0]);
      if(!row||row.max_price_microusd!==operation.body.max_price_microusd||row.price_ceiling_microusd!==operation.body.price_ceiling_microusd||row.snapshot===operation.body.expected_snapshot)throw Error('原价格修改尚未证实，保留原操作；请刷新审核记录，不重复修改。');
    }
    pending(null);await readBudgetAdmin();try{await refresh();}catch{}return {state:'reconciled',desired_values_confirmed:true};
  }
  async function act(fn){if(state.busy)throw Error('原操作仍在处理中。');const account=state.account,version=epoch;emit({busy:true,error:''});try{const value=await fn();guard(account,version);return value;}catch(error){if(version===epoch)emit({error:error.message});throw error;}finally{if(version===epoch)emit({busy:false});}}
  async function send(operation){const account=state.account,version=epoch;try{const value=await client[operation.method](...operation.args,operation.body,operation.key);guard(account,version);if(operation.method==='updateBudget'&&(value?.budget?.id!==operation.args[0]||value.budget.limit_microusd!==operation.body.limit_microusd||typeof value.budget.snapshot!=='string'||typeof value.changed!=='boolean')||operation.method==='updatePrice'&&(value?.profile?.id!==operation.args[0]||value.profile.max_price_microusd!==operation.body.max_price_microusd||value.profile.price_ceiling_microusd!==operation.body.price_ceiling_microusd||typeof value.profile.snapshot!=='string'||typeof value.changed!=='boolean'))throw Error('修改收据尚未确认，请核对原操作。');if(['updateBudget','updatePrice'].includes(operation.method)&&value.changed===true&&(typeof value.audit_event_id!=='string'||!value.audit_event_id))throw Error('修改审核收据尚未确认，请核对原操作。');if(operation.method==='cancelJobs'&&!cancellationReceipt(value,operation.body))throw Error('原取消收据尚未确认，请核对原操作。');if(operation.method==='updateHostPolicy'&&!hostPolicyReceipt(value,operation.body))throw Error('原主机策略收据尚未确认，请核对原操作。');pending(null);try{await refresh(!!state.adminSnapshot);}catch{}if(operation.method==='cancelJobs')try{await readJobAdmin();}catch{}if(operation.method==='updateHostPolicy')try{await readHosts({admin:true});await readHostAudit();if(String(state.hostTarget?.machine_id)===operation.body.machine_id)await readHostTarget(operation.body.machine_id);}catch{}return value;}catch(error){guard(account,version);if(operation.method==='updateHostPolicy'&&[401,403].includes(error.status))emit({adminHosts:null,hostAudits:null,hostTarget:null,...(state.snapshot?{snapshot:{...state.snapshot,operator:{...state.snapshot.operator,admin:false}}}:{})});const startRejected=rejectedStart(operation,error);if(startRejected||error.status>=400&&error.status<500&&![408,429].includes(error.status))pending(null);if(startRejected&&error.detail.code==='capacity_unavailable')error.message='启动未接受：机器池尚未配置或已达到容量上限，没有新增付费执行。请联系管理员核对容量配置（capacity_unavailable）。';if(error.status===409&&(error.detail?.detail==='operator_node_version_conflict'||error.detail?.code==='operator_node_version_conflict'||error.message==='operator_node_version_conflict')){try{await refresh(!!state.adminSnapshot);}catch{}guard(account,version);error.message='机器状态刚刚更新，本次操作未接受。请重试（operator_node_version_conflict）。';}throw error;}}
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
    async setAccount(account){epoch++;reading=false;budgetReading++;jobReading++;for(const key of Object.keys(hostReading))hostReading[key]++;client.setAccount(account);let saved=null;try{saved=account?JSON.parse(storage?.getItem(pendingKey(account))||'null'):null;}catch{saved={corrupt:true};}emit({account,snapshot:null,catalog:null,hosts:null,adminSnapshot:null,adminBudgets:null,adminPrices:null,budgetAudits:null,adminJobs:null,adminHosts:null,hostAudits:null,hostTarget:null,pending:saved,busy:false,loading:false,error:'',denied:false});if(account)try{await refresh();}catch{}},
    refresh,readAdmin,readBudgetAdmin,readJobAdmin,readHosts,readHostAudit,readHostTarget,poll:admin=>state.busy?Promise.resolve():refresh(admin),
    async preview(body){const account=state.account,version=epoch;const value=await client.preview(body);guard(account,version);return value;},stopImpact:(kind,target)=>client.stopImpact(kind,target),
    async metrics(nodeId){const account=state.account,version=epoch;const value=await client.metrics(nodeId);guard(account,version);if(value?.node_id!==nodeId)throw Error('机器统计身份尚未确认。');return value;},
    start:preview=>command('start',[],{preview_id:preview.preview_id}),
    stop:(node,impact)=>nodeCommand('stop',node,{...(impact?.warnings?.length?{acknowledge_capacity_loss:impact.fingerprint}:{})}),
    hold:(node,body)=>nodeCommand('hold',node,body),
    createGroup:name=>command('createGroup',[],{name}),
    move:(node,groupId)=>nodeCommand('move',node,{group_id:groupId}),
    groupStop:(group,impact)=>command('groupStop',[group.id],{expected_version:group.version,...(impact?.warnings?.length?{acknowledge_capacity_loss:impact.fingerprint}:{})}),
    groupHold:(group,body)=>command('groupHold',[group.id],{...body,expected_version:group.version}),
    updateBudget:(row,limit)=>command('updateBudget',[row.id],{expected_snapshot:row.snapshot,limit_microusd:limit},async(account,version)=>{await readAdmin();guard(account,version);return {expected_snapshot:row.snapshot,limit_microusd:limit};}),
    updatePrice:(row,max,ceiling)=>command('updatePrice',[row.id],{expected_snapshot:row.snapshot,max_price_microusd:max,price_ceiling_microusd:ceiling},async(account,version)=>{await readAdmin();guard(account,version);return {expected_snapshot:row.snapshot,max_price_microusd:max,price_ceiling_microusd:ceiling};}),
    cancelJobs:(jobs,reason)=>command('cancelJobs',[],null,async(account,version)=>{const body=cancellationBody(jobs,reason);if(!body)throw Error('请选择最多 100 个可取消的待执行任务，并填写取消原因。');await readAdmin();guard(account,version);return body;}),
    updateHostPolicy:(host,selection)=>command('updateHostPolicy',[],null,async(account,version)=>{const body=hostPolicyBody(host,selection);if(!body)throw Error('原物理主机与策略版本尚未确认，未发送修改。');await readAdmin();guard(account,version);if(state.snapshot?.operator?.admin!==true)throw Error('管理员权限已改变，未发送修改。');return body;}),
    recover:()=>act(async()=>{const account=state.account,version=epoch,original=state.pending;if(!original||original.corrupt||original.account!==state.account||!methods.has(original.method)||!Array.isArray(original.args)||typeof original.key!=='string')throw Error('原操作记录无法核对，请保留并联系管理员。');if(['cancelJobs','updateHostPolicy'].includes(original.method)){if(original.args.length!==0||!(original.method==='cancelJobs'?validCancellationBody(original.body):validHostPolicyBody(original.body)))throw Error('原管理员操作记录无法核对，请保留并联系管理员。');await readAdmin();guard(account,version);if(state.snapshot?.operator?.admin!==true)throw Error('管理员权限已改变，原操作仍待核对。');}return ['updateBudget','updatePrice'].includes(original.method)?reconcilePolicy(original):send(original);}),
    destroy(){epoch++;client.reset();listeners.clear();},
  };
}
