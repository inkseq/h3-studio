import {createCloudClient,CloudError} from './cloud-client.js';
import {translateForAccount} from './i18n/locale.js';

const ROOT='/v1/operator/dstack';
const id=value=>encodeURIComponent(value);
export function createDstackClient(options={}){
  const api=createCloudClient(options);
  let account=null;
  return {...api,
    setAccount(value){account=value;api.setAccount(value);},
    state:async()=>{
      const value=await api.request(ROOT+'/state');
      if(value?.capacity_backend!=='dstack-v1'||!Array.isArray(value.nodes))throw new CloudError(translateForAccount(account,'client.dstackState'));
      return {...value,nodes:value.nodes.map(node=>({...node,id:node.node_id}))};
    },
    catalog:()=>api.request(ROOT+'/catalog'),
    preview:body=>api.request(ROOT+'/previews',{method:'POST',body}),
    start:(body,key)=>api.request(ROOT+'/starts',{method:'POST',body,key}),
    stop:(nodeId,body,key)=>api.request(ROOT+`/nodes/${id(nodeId)}/stop`,{method:'POST',body,key}),
    hold:(nodeId,body,key)=>api.request(ROOT+`/nodes/${id(nodeId)}/hold`,{method:'POST',body,key}),
  };
}
