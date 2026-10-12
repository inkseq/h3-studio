import {createDstackClient} from './dstack-client.js';
import {CloudError} from './cloud-client.js';

const ROOT='/v1/operator/dstack',id=value=>encodeURIComponent(value);
export function createP4OperatorClient(options={}){
  const api=createDstackClient(options);
  async function workspace(admin=false){
    const value=await api.request(ROOT+(admin?'/admin/workspace':'/workspace'));
    if(value?.capacity_backend!=='dstack-v1'||!Array.isArray(value.nodes)||!Array.isArray(value.groups)||!Array.isArray(value.queue))throw new CloudError('控制台状态尚未确认，请刷新。');
    return {...value,nodes:value.nodes.map(node=>({...node,id:node.node_id||node.id}))};
  }
  return {...api,state:()=>workspace(false),adminState:()=>workspace(true),
    createGroup:(body,key)=>api.request(ROOT+'/groups',{method:'POST',body,key}),
    move:(nodeId,body,key)=>api.request(ROOT+`/nodes/${id(nodeId)}/group`,{method:'POST',body,key}),
    stopImpact:(kind,target)=>api.request(ROOT+`/${kind==='group'?'groups':'nodes'}/${id(target)}/stop-impact`),
    metrics:nodeId=>api.request(ROOT+`/nodes/${id(nodeId)}/metrics`),
    groupStop:(groupId,body,key)=>api.request(ROOT+`/groups/${id(groupId)}/stop`,{method:'POST',body,key}),
    groupHold:(groupId,body,key)=>api.request(ROOT+`/groups/${id(groupId)}/hold`,{method:'POST',body,key}),
  };
}
