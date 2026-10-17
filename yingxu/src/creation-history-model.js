export const HISTORY_ACTIONS={trash:'移到回收站',restore:'恢复创作',purge:'永久删除',empty_trash:'清空回收站'};
const integer=value=>Number.isSafeInteger(value)&&value>=0;
const identifier=value=>typeof value==='string'&&value.length>0&&value.length<=200;
const states=new Set(['active','trashed','purging','purged']);
const fail=()=>{throw Error('回收站收据不完整或与原选择不一致，请核对原操作。');};
export function historyReason(code){return ({active_work:'仍有任务未结束',session_busy:'仍有任务未结束',active_job:'仍有任务未结束',running_job:'仍有任务未结束',jobs_active_or_uncertain:'仍有任务运行或结果待核对',job_binding_unknown:'原任务关联待核对',upstream_stop_unconfirmed:'上游停止状态待核对',worker_obligation_unresolved:'原执行器仍有未完成工作',admission_unresolved:'原提交结果待核对',authoring_operation_unresolved:'原创作操作结果待核对',upload_unresolved:'原上传结果待核对',collection_unresolved:'原成片收集待完成',purge_already_started:'永久删除已经开始，不能恢复',session_not_active:'记录已不在创作列表',session_not_in_trash:'记录不在回收站',uncertain_work:'原任务结果待核对',upstream_unconfirmed:'上游停止状态待核对',version_conflict:'记录已更新，请重新预览',review_hold:'待人工复核，暂不自动清理',purging:'永久删除处理中',purged:'已永久删除，无法恢复',not_trashed:'记录不在回收站',shared_reference:'共享或故事引用会保留',chosen_story_reference:'故事已选版本会保留'})[code]||'当前不可操作，请刷新核对';}
export function historySelectable(row,view){return !!row&&identifier(row.id)&&integer(row.version)&&row.version>0&&(view==='active'?row.trash_state==='active'&&row.trashable===true:row.trash_state==='trashed');}
export function historySelection(rows,ids,view){const selected=ids.map(id=>rows.find(row=>row.id===id));if(!selected.length||selected.length>100||new Set(ids).size!==ids.length||selected.some(row=>!historySelectable(row,view)))throw Error('请选择当前可操作的创作；任务未结束或删除中的记录不能选择。');return selected.map(({id,version})=>({id,expected_version:version}));}
function totals(value,keys){if(!value||keys.some(key=>!integer(value[key])))fail();return Object.fromEntries(keys.map(key=>[key,value[key]]));}
export function checkedHistoryPreview(value,request,now=Date.now()/1000){
  if(!value||!identifier(value.preview_id)||value.action!==request.action||!HISTORY_ACTIONS[value.action]||!Number.isFinite(value.expires_at)||value.expires_at<=now||value.ledger_retained!==true||typeof value.review_hold_release_required!=='boolean'||!Array.isArray(value.items)||value.items.length>10000||!Array.isArray(value.warnings)||value.warnings.length>10000)fail();
  const items=value.items.map(item=>{if(!identifier(item.id)||!integer(item.expected_version)||item.expected_version<1||typeof item.eligible!=='boolean')fail();return {...item};});
  if(new Set(items.map(item=>item.id)).size!==items.length)fail();
  if(request.action!=='empty_trash'&&(items.length!==request.sessions.length||request.sessions.some(wanted=>!items.some(item=>item.id===wanted.id&&item.expected_version===wanted.expected_version))))fail();
  return {...value,items,removed:totals(value.removed,['sessions','turns','cards','assets','artifacts','objects','bytes']),kept:totals(value.kept,['assets','artifacts','objects','bytes'])};
}
export function checkedHistoryResult(value,operation){
  if(!value||!identifier(value.action_id)||value.action!==operation.action||!['pending','completed'].includes(value.state)||value.ledger_retained!==true||operation.actionId&&operation.actionId!==value.action_id||!Array.isArray(value.items)||value.items.length!==operation.items.length)fail();
  if(new Set(value.items.map(item=>item.id)).size!==value.items.length||operation.items.some(wanted=>!value.items.some(item=>item.id===wanted.id)))fail();
  for(const item of value.items){if(!states.has(item.trash_state)||!['completed','pending','blocked'].includes(item.state))fail();if(value.state==='completed'&&item.state!=='blocked'&&(item.state!=='completed'||(value.action==='trash'?item.trash_state!=='trashed':value.action==='restore'?item.trash_state!=='active':item.trash_state!=='purged')))fail();}
  return {...value,removed:totals(value.removed,['sessions','turns','cards','assets','artifacts','objects','bytes']),kept:totals(value.kept,['assets','artifacts','objects','bytes'])};
}
export function purgeCountdown(row,asOf){
  if(row.trash_state==='purging')return '永久删除处理中';
  if(row.trash_state==='purged')return '已永久删除';
  if(row.review_hold)return '人工复核保留 · 不自动删除';
  if(row.auto_purge_enabled!==true)return row.purge_blocked_reason?'缺少永久清理授权 · 不自动删除':'未启用自动清理';
  if(!Number.isFinite(row.purge_at)||!Number.isFinite(asOf))return '自动清理时间待核对';
  const seconds=row.purge_at-asOf;if(seconds<=0)return '已到清理时间 · 以服务端结果为准';
  return seconds>=86400?`${Math.ceil(seconds/86400)} 天后自动清理`:`${Math.max(1,Math.ceil(seconds/3600))} 小时后自动清理`;
}
export function historyBytes(value){if(!integer(value))return '待核对';return value>=1024**3?(value/1024**3).toFixed(2)+' GiB':value>=1024**2?(value/1024**2).toFixed(1)+' MiB':value.toLocaleString()+' B';}
