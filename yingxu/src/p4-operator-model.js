import {dstackProvider,dstackMode,dstackLifecycle} from './dstack-presentation.js';

export {dstackProvider};
export const modeLabel=mode=>mode==='both'?'FL / REF · 可切换':dstackMode(mode);
export const finite=value=>typeof value==='number'&&Number.isFinite(value);
export const money=value=>finite(value)?'$'+(value/1e6).toFixed(value<100000?3:2):'—';
export const time=value=>finite(value)?new Date(value*1000).toLocaleString('zh-CN'):'—';
export const elapsed=value=>!finite(value)?'—':value<60?Math.round(value)+' 秒':value<3600?Math.round(value/60)+' 分钟':(value/3600).toFixed(1)+' 小时';
export const terminal=node=>node?.state==='stopped'||node?.lifecycle_phase==='stopped';
export const stopping=node=>!terminal(node)&&(node?.desired_state==='stopped'||['draining','stopping'].includes(node?.state));
export function phase(node){
  if(terminal(node))return {id:'stopped',label:'已停止',tone:'muted'};
  if(stopping(node))return {id:'stopping',label:'停止中',tone:'muted'};
  if(node?.current_job)return {id:'running',label:'正在生成',tone:'good'};
  if(node?.ready===true&&node?.observation_fresh===true)return {id:'ready',label:'就绪',tone:'good'};
  const id=node?.lifecycle_phase;
  return {id:id||'preparing',label:({renting:'申请机器中',downloading_weights:'下载权重中',loading_model:'加载模型中',preparing:'准备中',starting:'准备中',runtime_unconfirmed:'准备中',ready:'准备中'}[id]||'准备中'),tone:'warm'};
}
export function keepText(node){
  const policy=dstackLifecycle(node);
  if(!policy)return finite(node?.hard_deadline)?'至 '+time(node.hard_deadline):'运行策略待更新';
  if(policy.keep_on==='until_stopped')return '直到我手动停止';
  const idle='空闲 '+Math.round(policy.idle_shutdown_seconds/60)+' 分钟后停止';
  return policy.keep_on==='until'?'保持至 '+time(policy.hold_until):idle;
}
export function rate(node){
  const quoted=node?.quoted_hourly_cost_microusd??node?.hourly_price_microusd;
  return finite(quoted)?{value:quoted,label:money(quoted),basis:'供应商报价'}:{value:node?.hourly_cost_microusd,label:finite(node?.hourly_cost_microusd)?'≤ '+money(node.hourly_cost_microusd):'—',basis:'每小时上限'};
}
export function costLabel(basis){return ({settled:'已结算',actual:'实际',dstack_estimate:'估算',quoted_estimate:'估算',runtime_quote_estimate:'估算',estimated_compute:'估算',approved_ceiling:'上限估算',approved_ceiling_estimate:'上限估算',provisioning_quote_estimate:'估算',unavailable:'尚未确认'}[basis]||'尚未确认');}
export function queueLabel(queue){
  if(!queue?.waiting)return '没有等待任务';
  if(!finite(queue.rough_wait_seconds))return '等待兼容机器准备好';
  return queue.rough_wait_seconds<=0?'有机器可接任务':'队列粗略等待约 '+elapsed(queue.rough_wait_seconds);
}
export function initialRows(profiles,{profileId,mode,one=false}={}){
  const selected=profiles.find(profile=>profile.id===profileId)||profiles.find(profile=>profile.mode===mode)||profiles[0];
  if(!selected)return [];
  const make=profile=>({id:globalThis.crypto.randomUUID(),profile_id:profile.id,quantity:1});
  if(one||profileId||mode)return [make(selected)];
  const ref=profiles.find(profile=>profile.model_id===selected.model_id&&profile.backend===selected.backend&&profile.mode==='ref');
  const fl=profiles.find(profile=>profile.model_id===selected.model_id&&profile.backend===selected.backend&&profile.mode==='fl');
  return fl&&ref?[make(fl),make(ref)]:[make(selected)];
}
export function lifetimeBody({keep_on='idle',idle_minutes=10,until},now=Date.now()){
  const idle=Number(idle_minutes)*60;
  if(!Number.isInteger(idle)||idle<1||idle>86400||!['idle','until','until_stopped'].includes(keep_on))return null;
  const seconds=keep_on==='until'?Math.ceil((Date.parse(until)-now)/1000):0;
  if(!Number.isInteger(seconds)||seconds<0||seconds>86400||keep_on==='until'&&seconds<1)return null;
  return {keep_on,hold_seconds:seconds,idle_shutdown_seconds:idle};
}
export function previewCurrent(preview,selection,now=Date.now()){
  const policy=dstackLifecycle(preview);
  return preview?.capacity_backend==='dstack-v1'&&preview.can_start===true&&typeof preview.preview_id==='string'&&finite(preview.expires_at)&&preview.expires_at*1000>now&&policy&&typeof selection?.profile_id==='string'&&policy.keep_on===selection.keep_on&&(!Object.hasOwn(selection,'idle_shutdown_seconds')||policy.idle_shutdown_seconds===selection.idle_shutdown_seconds)&&(!preview.profile_id||preview.profile_id===selection.profile_id)&&(!Object.hasOwn(preview,'group_id')||preview.group_id===selection.group_id)&&finite(preview.hourly_cost_microusd)&&preview.hourly_cost_microusd>0&&finite(preview.reservation_microusd)&&preview.reservation_microusd>=0;
}
export function batchRows(rows,profiles){
  if(!Array.isArray(rows)||!rows.length||rows.length>16)return null;
  const result=[];
  for(const row of rows){
    if(!Number.isInteger(row.quantity)||row.quantity<1||row.quantity>8||!profiles.some(profile=>profile.id===row.profile_id))return null;
    for(let count=0;count<row.quantity;count++)result.push({profile_id:row.profile_id,row_id:row.id,number:count+1});
  }
  return result.length<=16?result:null;
}
export function groupedNodes(nodes,groups){
  const live=(nodes||[]).filter(node=>!terminal(node));
  const buckets=(groups||[]).map(group=>({...group,nodes:live.filter(node=>node.group_id===group.id)}));
  const ungrouped=live.filter(node=>!node.group_id||!groups.some(group=>group.id===node.group_id));
  if(ungrouped.length||!buckets.length)buckets.push({id:null,name:'未分组',nodes:ungrouped});
  return buckets;
}
export function todayMetrics(node){
  const usage=node?.usage||{};
  return {cost:money(usage.today_cost_microusd),count:Number.isInteger(usage.today_video_count)?usage.today_video_count:'—',basis:costLabel(usage.today_cost_basis||usage.cost_basis)};
}
