import {dollarsToMicros} from './money.js';
import {stableJSON} from './cloud-model.js';
const clone=value=>structuredClone(value);
export function capMicros(value){try{return dollarsToMicros(value);}catch{throw Error('assistants.invalid');}}
export function assistantScope(kind,{sessionId,projectId}={}){if(kind==='all')return {kind};const id=kind==='session'?sessionId:kind==='story'?projectId:null;if(typeof id!=='string'||!id)throw Error('assistants.scopeUnavailable');return {kind,id};}
const same=(a,b)=>stableJSON(a)===stableJSON(b);
const methods=new Set(['createConnection','updateConnection','revokeConnection','saveConnectionSettings']);
export function createAssistantOperations({client,account,storage=globalThis.localStorage,onChange=()=>{},keyFactory=()=>crypto.randomUUID()}){
 const name=`yingxu:assistant-operation:${account}`,oldName=`yingxu:agent-connection-intent:${account}`;let active=true,busy=false,pending=null;
 try{pending=JSON.parse(storage?.getItem(name)||'null');if(!pending){const old=JSON.parse(storage?.getItem(oldName)||'null');if(old)pending={method:'createConnection',args:[],account,...old};}}catch{pending={corrupt:true};}
 const notify=()=>onChange({busy,pending});
 function save(value){if(!storage)throw Error('assistants.storage');if(value)storage.setItem(name,JSON.stringify(value));else{storage.removeItem(name);storage.removeItem(oldName);}pending=value;notify();}
 function check(value,op){
  if(value?.no_mutation_committed===true){if(!['updateConnection','saveConnectionSettings'].includes(op.method)||value.error_code!=='connection_version_conflict'||!same(value.request,op.body)||op.method==='updateConnection'&&(value.connection||value).id!==op.args[0])throw Error('assistants.receipt');return;}
  if(op.method==='saveConnectionSettings'){if(!Number.isInteger(value?.version)||value.version<=op.body.expected_version||!same(value.value,op.body.value))throw Error('assistants.receipt');return;}
  const row=value?.connection||value;if(!row||typeof row.id!=='string'||op.args.length&&row.id!==op.args[0])throw Error('assistants.receipt');
  if(op.method==='createConnection'){if(row.name!==op.body.name||op.body.scope&&!same(row.scope,op.body.scope)||op.body.permission&&row.permission!==op.body.permission||row.daily_limit_microusd!==(op.body.daily_limit_microusd??0))throw Error('assistants.receipt');}
  if(op.method==='updateConnection'){if(!Number.isInteger(row.state_version)||row.state_version<=op.body.expected_version)throw Error('assistants.receipt');for(const k of ['paused','daily_limit_microusd'])if(Object.hasOwn(op.body,k)&&row[k]!==op.body[k])throw Error('assistants.receipt');}
  if(op.method==='revokeConnection'&&row.status!=='revoked')throw Error('assistants.receipt');
 }
 async function execute(method,args=[],body){if(!active||busy)throw Error('assistants.wait');if(pending&&method)throw Error('assistants.wait');const op=pending||{account,method,args:clone(args),...(body===undefined?{}:{body:clone(body)}),key:'assistant-'+keyFactory()};if(op.corrupt||op.account!==account||!methods.has(op.method)||!Array.isArray(op.args)||typeof op.key!=='string')throw Error('assistants.wait');save(op);busy=true;notify();try{let value=await (op.method==='revokeConnection'?client[op.method](...op.args,op.key):client[op.method](...op.args,op.body,op.key));if(!active)throw Error('assistants.scopeChanged');if(op.method==='revokeConnection'){if(value?.id!==op.args[0]||value.revoked!==true)throw Error('assistants.receipt');value=await client.connection(op.args[0]);if(!active)throw Error('assistants.scopeChanged');}check(value,op);save(null);return value;}finally{busy=false;if(active)notify();}}
 return {read:()=>({busy,pending}),execute,recover:()=>execute(),destroy(){active=false;}};
}
export function creatorName(value){return value?.kind==='agent'&&typeof value.connection_id==='string'&&typeof value.name==='string'?value.name:null;}

export function assistantDate(value,locale,unknown){if(typeof value!=='number'||!Number.isFinite(value))return unknown;const date=new Date(value*1000);if(!Number.isFinite(date.getTime()))return unknown;try{return date.toLocaleString(locale==='zh'?'zh-CN':'en');}catch{return unknown;}}

export function mergeConnectionResult(rows,result){const row=result?.connection||result;if(!row||typeof row.id!=='string'||typeof row.status!=='string')return rows;return rows.some(old=>old.id===row.id)?rows.map(old=>old.id===row.id?row:old):[row,...rows];}
