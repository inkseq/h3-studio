import {dstackProvider,dstackMode,dstackLifecycle} from './dstack-presentation.js';

export {dstackProvider};
export const modeLabel=mode=>mode==='both'?'FL / REF · 可切换':dstackMode(mode);
export const finite=value=>typeof value==='number'&&Number.isFinite(value);
// Readback renders can occur after the timer tick which began the request.
// Allow small server/client clock skew, retaining the age and future guards.
export function workspaceStale(snapshot,now=Date.now()){
  return !!snapshot&&(!finite(snapshot.observed_at)||!finite(now)||now-snapshot.observed_at*1000>30000||snapshot.observed_at>(now+5000)/1000);
}
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
const preparationLabels={renting:'申请机器中',pulling_image:'拉取镜像中',downloading_weights:'下载权重中',starting_runtime:'启动推理服务',verifying_runtime:'检查运行接口',loading_model:'加载模型中',awaiting_first_job:'等待首个任务加载模型',ready:'就绪',running_job:'正在生成',stopping:'停止中',stopped:'已停止',preparing:'等待准备观测'};
const nonnegative=value=>finite(value)&&value>=0;
const diagnosticCode=value=>typeof value==='string'&&/^[a-zA-Z0-9_.:-]{1,160}$/.test(value)?value:null;
export function bytes(value){
  if(!nonnegative(value))return '—';
  const units=['B','KiB','MiB','GiB','TiB'];let amount=value,index=0;
  while(amount>=1024&&index<units.length-1){amount/=1024;index++;}
  return (index===0?Math.round(amount):amount.toFixed(amount>=100?0:amount>=10?1:2))+' '+units[index];
}
export function transferProgress(value){
  if(!value||typeof value!=='object')return {label:'下载量尚未上报',percent:null};
  const complete=nonnegative(value.bytes_complete)?value.bytes_complete:null,total=nonnegative(value.bytes_total)&&value.bytes_total>0?value.bytes_total:null;
  const files=Number.isSafeInteger(value.files_complete)&&value.files_complete>=0?value.files_complete:null,fileTotal=Number.isSafeInteger(value.files_total)&&value.files_total>0?value.files_total:null;
  const prefix=value.basis==='download_and_extract'?'拉取＋解压 ':value.basis==='completed_files'?'已完成文件 ':'';
  const amount=complete!==null?(total!==null&&complete<=total?prefix+bytes(complete)+' / '+bytes(total):(prefix||'已下载 ')+bytes(complete)):'下载字节数尚未上报';
  const fileAmount=files===null?'':fileTotal!==null&&files<=fileTotal?files+' / '+fileTotal+' 个文件':files+' 个文件';
  // A progress bar is only shown for a reported percentage backed by a valid
  // known total. File/byte counters are not converted into an invented rate.
  const percent=finite(value.percent)&&value.percent>=0&&value.percent<=100&&complete!==null&&total!==null&&complete<=total?value.percent:null;
  const downloaded=nonnegative(value.downloaded_bytes)?' · 已下载 '+bytes(value.downloaded_bytes):'',extraction=nonnegative(value.extracted_bytes)?' · 已解压 '+bytes(value.extracted_bytes):'';
  return {label:amount+(fileAmount?' · '+fileAmount:'')+downloaded+extraction,percent};
}
export function preparationView(node){
  const source=node?.preparation&&typeof node.preparation==='object'?node.preparation:null,authority=phase(node);
  let id=source&&Object.hasOwn(preparationLabels,source.phase)?source.phase:authority.id;
  if(terminal(node))id='stopped';else if(stopping(node))id='stopping';
  else if(node?.current_job)id=source?.model?.state==='loading'&&source?.stale!==true?'loading_model':'running_job';
  else if(authority.id==='ready')id=source?.model?.state==='loading'&&source?.stale!==true?'loading_model':'ready';
  const status={id,label:preparationLabels[id]||authority.label,tone:['ready','running_job','running'].includes(id)?'good':['stopping','stopped'].includes(id)?'muted':'warm'};
  if(id==='ready'&&authority.id!=='ready'&&authority.id!=='running')status.label='运行接口已启动，等待执行槽';
  const elapsedSeconds=nonnegative(source?.elapsed_seconds)?source.elapsed_seconds:null,phaseElapsed=nonnegative(source?.phase_elapsed_seconds)?source.phase_elapsed_seconds:null;
  const download=transferProgress(source?.download),image=transferProgress(source?.image);
  const modelState=['not_loaded','loading','loaded','not_observed'].includes(source?.model?.state)?source.model.state:'not_observed';
  const modelLabel=modelState==='not_loaded'&&terminal(node)?'本次未加载模型':({not_loaded:'模型尚未加载，将在首个任务加载',loading:'正在加载模型到 GPU',loaded:'模型已加载',not_observed:'模型加载状态尚未上报'})[modelState];
  const reasonCode=diagnosticCode(source?.reason_code??node?.reason_code),reasonText=typeof source?.reason_text==='string'&&source.reason_text.trim()?source.reason_text.trim().slice(0,600):null;
  const stale=source?.stale===true,stalled=source?.stalled===true;
  const observationAge=nonnegative(source?.observation_age_seconds)?source.observation_age_seconds:null;
  return {source,status,download,image,modelState,modelLabel,elapsedSeconds,phaseElapsed,reasonCode,reasonText,stale,stalled,observationAge};
}
export function preparationSteps(view){
  const steps=[['renting','申请机器'],['pulling_image','拉取镜像'],['downloading_weights','下载权重'],['starting_runtime','启动服务'],['loading_model','加载模型'],['ready','可接任务']];
  const current=({verifying_runtime:'starting_runtime',awaiting_first_job:'loading_model',running_job:'ready',running:'ready'})[view.status.id]||view.status.id;
  const result=steps.map(([id,label])=>({id,label,current:id===current,pending:id==='loading_model'&&view.status.id==='awaiting_first_job'}));
  if(view.status.id==='ready'&&view.status.label!=='就绪')result[result.length-1].label='等待执行槽';
  if(['running','running_job'].includes(view.status.id))result[result.length-1].label='执行任务';
  if(['stopping','stopped'].includes(current))result.push({id:current,label:preparationLabels[current],current:true,pending:false});
  return result;
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
  return preview?.capacity_backend==='dstack-v1'&&preview.can_start===true&&typeof preview.preview_id==='string'&&finite(preview.expires_at)&&preview.expires_at*1000>now&&policy&&typeof selection?.profile_id==='string'&&policy.keep_on===selection.keep_on&&(!Object.hasOwn(selection,'idle_shutdown_seconds')||policy.idle_shutdown_seconds===selection.idle_shutdown_seconds)&&(!preview.profile_id||preview.profile_id===selection.profile_id)&&(!Object.hasOwn(preview,'group_id')||preview.group_id===selection.group_id)&&finite(preview.hourly_cost_microusd)&&preview.hourly_cost_microusd>0&&(!Object.hasOwn(selection,'max_price_microusd')||preview.max_price_microusd===selection.max_price_microusd&&preview.hourly_cost_microusd===selection.max_price_microusd)&&finite(preview.reservation_microusd)&&preview.reservation_microusd>=0;
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
