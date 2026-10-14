import {createDstackClient} from './dstack-client.js';
import {CloudError} from './cloud-client.js';
import {validBudgetPage} from './operator-budget-model.js';
import {validAdminJobs} from './operator-jobs-model.js';
import {validHostAudit,validHostPage} from './operator-host-model.js';

const ROOT='/v1/operator/dstack',id=value=>encodeURIComponent(value);
export function createP4OperatorClient(options={}){
  const api=createDstackClient(options);
  let account=null;
  async function budgets(admin=false,{after_id,limit=100}={}){
    const value=await api.request('/v1/operator/'+(admin?'admin/':'')+'budgets?limit='+limit+(after_id?'&after_id='+id(after_id):''));
    if(!validBudgetPage(value,account,{admin})||admin&&value.admin!==true)throw new CloudError('账户预算身份或累计记录尚未确认，请刷新。');
    return value;
  }
  async function workspace(admin=false){
    const value=await api.request(ROOT+(admin?'/admin/workspace':'/workspace'));
    if(value?.capacity_backend!=='dstack-v1'||!Array.isArray(value.nodes)||!Array.isArray(value.groups)||!Array.isArray(value.queue))throw new CloudError('控制台状态尚未确认，请刷新。');
    return {...value,nodes:value.nodes.map(node=>({...node,id:node.node_id||node.id}))};
  }
  return {...api,setAccount(value){account=value;api.setAccount(value);},state:()=>workspace(false),adminState:()=>workspace(true),
    budgets:options=>budgets(false,options),adminBudgets:options=>budgets(true,options),
    budgetAudit:()=>api.request('/v1/operator/admin/budgets/audit'),
    updateBudget:(budgetId,body,key)=>api.request('/v1/operator/admin/budgets/'+id(budgetId),{method:'PUT',body,key}),
    async adminPrices(){const value=await api.request(ROOT+'/admin/prices');if(value?.operator?.account!==account||value.operator.admin!==true||!Array.isArray(value.profiles)||!Array.isArray(value.audits))throw new CloudError('价格管理身份尚未确认，请刷新。');return value;},
    updatePrice:(profileId,body,key)=>api.request(ROOT+'/admin/prices/'+id(profileId),{method:'PUT',body,key}),
    async adminJobs({after_id,limit=100}={}){const value=await api.request('/v1/operator/admin/jobs?limit='+limit+(after_id?'&after_id='+id(after_id):''));if(!validAdminJobs(value,account))throw new CloudError('管理员任务身份或原状态尚未确认，请刷新。');return value;},
    cancelJobs:(body,key)=>api.request('/v1/operator/admin/jobs/cancel',{method:'POST',body,key}),
    async hosts({admin=false,offset=0,limit=100,machine_id}={}){if(machine_id!==undefined&&(!admin||typeof machine_id!=='string'||!/^[1-9][0-9]{0,17}$/.test(machine_id)))throw new CloudError('请输入真实物理 machine_id；普通账户不能查询任意主机策略。');const value=await api.request('/v1/operator/'+(admin?'admin/':'')+'hosts?limit='+limit+'&offset='+offset+(machine_id?'&machine_id='+id(machine_id):''));if(!validHostPage(value,account,{admin})||machine_id&&(value.hosts.length!==1||String(value.hosts[0].machine_id)!==machine_id))throw new CloudError('主机记录身份或观测数据尚未确认，请刷新。');return value;},
    async hostAudit({offset=0,limit=100}={}){const value=await api.request('/v1/operator/admin/hosts/audit?limit='+limit+'&offset='+offset);if(!validHostAudit(value,account))throw new CloudError('主机策略审核身份尚未确认，请刷新。');return value;},
    updateHostPolicy:(body,key)=>api.request('/v1/operator/admin/hosts/policy',{method:'POST',body,key}),
    createGroup:(body,key)=>api.request(ROOT+'/groups',{method:'POST',body,key}),
    move:(nodeId,body,key)=>api.request(ROOT+`/nodes/${id(nodeId)}/group`,{method:'POST',body,key}),
    stopImpact:(kind,target)=>api.request(ROOT+`/${kind==='group'?'groups':'nodes'}/${id(target)}/stop-impact`),
    metrics:nodeId=>api.request(ROOT+`/nodes/${id(nodeId)}/metrics`),
    groupStop:(groupId,body,key)=>api.request(ROOT+`/groups/${id(groupId)}/stop`,{method:'POST',body,key}),
    groupHold:(groupId,body,key)=>api.request(ROOT+`/groups/${id(groupId)}/hold`,{method:'POST',body,key}),
  };
}
