/* Advisory capacity only. Submission and original-operation recovery stay server-owned. */
export const MODE_LABELS={fl:'FL2VA 首尾帧',ref:'Ref2VA 全能参考'};
const copy={
  ready:['可生成','当前模式已有就绪机器，提交时仍会检查输入和执行条件。'],
  busy:['可排队','对应机器正在生成，可提交排队；以后台准入结果为准。'],
  starting:['正在准备','对应机器正在准备，请等待就绪。'],
  unavailable:['暂无机器','暂无可用机器，请联系管理员启动对应模式。'],
  disabled:['暂未开放','该模式暂未开放，请联系管理员核对运行配置。'],
  unknown:['状态待确认','暂时无法确认执行状态，请刷新后重试。'],
  stale:['状态已过期','执行状态已过期，请刷新后再生成。'],
};
export function modeAvailability(snapshot,capabilities,settings={},mode,now=Date.now()){
  const selected=capabilities?.recipes?.find(item=>item.id===settings.recipe_id);
  mode=mode||selected?.mode;
  const result=(state,available=false,reason_code=null)=>({mode,state,available,reason_code,label:copy[state][0],message:copy[state][1]});
  if(!['fl','ref'].includes(mode)||!selected||snapshot?.version!==1||snapshot.advisory_only!==true||!Array.isArray(snapshot.profiles))return result('unknown');
  const observed=snapshot.observed_at,expires=snapshot.expires_at,seconds=now/1000;
  if(!Number.isFinite(observed)||!Number.isFinite(expires)||expires<=observed||expires-observed>10.01||observed>seconds+5)return result('unknown');
  if(seconds>=expires)return result('stale');
  // Never borrow another profile or the service default for an authored choice.
  const profiles=snapshot.profiles.filter(item=>item?.deployment_profile_id===(settings.deployment_profile_id??null));
  const row=profiles.length===1?profiles[0].modes?.[mode]:null;
  const recipe=mode===selected.mode?selected:capabilities.recipes.find(item=>item.mode===mode);
  if(!row||row.recipe_id!==recipe?.id||!Object.hasOwn(copy,row.state)||row.state==='stale'||typeof row.available!=='boolean')return result('unknown');
  const available=['ready','busy'].includes(row.state);
  if(row.available!==available)return result('unknown');
  return result(row.state,available,row.reason_code||null);
}

export function requireModeAvailable(snapshot,capabilities,settings,mode,now){
  const status=modeAvailability(snapshot,capabilities,settings,mode,now);
  if(!status.available)throw Error(`${MODE_LABELS[status.mode]||'当前模式'}：${status.message} 原草稿和素材保持不变。`);
  return status;
}
