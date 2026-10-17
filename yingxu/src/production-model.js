import {children} from './store.js';
import {activeJobStatuses,shotSnapshot,stableJSON} from './cloud-model.js';

export const queueFilters={all:'全部状态',unplanned:'待预检',ready:'预检通过，待提交',attention:'失败 / 阻塞 / 需重检',active:'排队或运行中',review:'待审或待返修',accepted:'已确认候选'};
export const reviewLabels={accepted:'已确认候选',review:'待复核',revision:'待返修',unselected:'待选择候选',missing:'待补候选'};
export function shotReviewState(project,shot,{cloud=true}={}){
  const review=shot.data.uxReview||{},asset=project.entities.find(e=>e.id===shot.data.selectedAssetId);
  const usable=!!asset?.data.fileId&&!asset.data.missingFile||!cloud&&!!review.chosen;
  if(['revision','failed','retry','cancelled'].includes(review.status)||review.allRejected)return 'revision';
  if(usable)return shot.status==='review'?'review':'accepted';
  if((shot.data.candidateIds||[]).some(id=>project.entities.some(e=>e.id===id&&e.data.fileId&&!e.data.missingFile)))return 'unselected';
  return 'missing';
}
export function reviewProgress(project,shots,currentId,options){
  const remaining=shots.filter(s=>shotReviewState(project,s,options)!=='accepted'),index=shots.findIndex(s=>s.id===currentId);
  const ordered=[...shots.slice(index+1),...shots.slice(0,Math.max(0,index))];
  return {done:shots.length-remaining.length,total:shots.length,remaining:remaining.length,next:ordered.find(s=>remaining.some(r=>r.id===s.id))?.id||null};
}
export function productionRows(project,cloud,now=Date.now()){
  return children(project,null).filter(e=>e.type==='chapter').flatMap(chapter=>children(project,chapter.id).filter(e=>e.type==='scene').flatMap(scene=>children(project,scene.id).filter(e=>e.type==='shot').map(shot=>{
    const plan=cloud.plans?.[shot.id],jobs=(cloud.jobs||[]).filter(j=>j.client_ref?.project_id===project.id&&j.client_ref?.shot_id===shot.id).sort((a,b)=>(Number(b.created_at)||0)-(Number(a.created_at)||0)),latest=jobs[0];
    const current=!!plan&&plan.sourceSnapshot===stableJSON(shotSnapshot(project,shot.id));
    const expires=plan?.expires_at&&(typeof plan.expires_at==='number'?plan.expires_at*1000:Date.parse(plan.expires_at));
    const held=jobs.some(j=>j.status==='recovery_hold');
    const pending=!!cloud.unknown?.[shot.id]||!!cloud.batchPending?.shotIds.includes(shot.id)||jobs.some(j=>j.status==='submission_unknown');
    const active=jobs.some(j=>activeJobStatuses.has(j.status)||j.status==='planned');
    const submitted=!!plan&&jobs.some(j=>j.plan_id===plan.plan_id),error=cloud.preflightErrors?.[shot.id];
    const waiting=jobs.some(j=>j.status==='waiting_capacity');
    const phase=held?'recovery_hold':pending?'unknown':waiting?'waiting_capacity':active?'active':error?'error':plan&&!current?'stale':plan&&expires&&expires<=now?'expired':plan?.status==='blocked'?'blocked':plan?.status==='ready'&&!submitted?'ready':latest?.status==='failed'?'failed':latest?.status==='blocked'?'blocked':latest?.status==='cancelled'?'cancelled':submitted||latest?.status==='succeeded'?'submitted':'unplanned';
    const labels={waiting_capacity:'等待计算容量',recovery_hold:'恢复后待核对',unknown:'提交结果待核对',active:'排队或运行中',error:'预检未完成',stale:'内容已改，需重检',expired:'计划过期，需重检',blocked:'有阻塞项',ready:'预检通过',failed:'最近任务失败',cancelled:'最近任务已取消',submitted:'已有任务结果',unplanned:'尚未预检'};
    return {id:shot.id,shot,chapter,scene,plan,jobs,latest,current,phase,label:labels[phase],review:shotReviewState(project,shot),error,canSelect:!held&&!pending&&!active,canSubmit:phase==='ready',path:`${chapter.title} / ${scene.title}`};
  })));
}
export function filterProductionRows(rows,{chapterId='',status='all',query=''}={}){
  const text=query.trim().toLocaleLowerCase();return rows.filter(row=>(!chapterId||row.chapter.id===chapterId)&&(!text||(row.path+' '+row.shot.title).toLocaleLowerCase().includes(text))&&(status==='all'||status==='unplanned'&&row.phase==='unplanned'||status==='ready'&&row.canSubmit||status==='attention'&&['error','stale','expired','blocked','failed','cancelled','unknown','recovery_hold'].includes(row.phase)||status==='active'&&['active','waiting_capacity'].includes(row.phase)||status==='review'&&row.review!=='accepted'||status==='accepted'&&row.review==='accepted'));
}
export function selectedProductionRows(rows,ids){const selected=new Set(ids);return rows.filter(row=>selected.has(row.id));}
export function batchSummary(rows){
  const ready=rows.filter(row=>row.canSubmit),groups=new Map();let unknown=0;
  for(const row of ready){const estimate=row.plan.estimate;if(!estimate||!Number.isFinite(estimate.cost_microusd)||estimate.cost_microusd<0){unknown++;continue;}const key=(estimate.kind||'estimate')+':'+(estimate.currency||'USD')+':'+(estimate.source||'');const existing=groups.get(key);if(existing)existing.cost_microusd+=estimate.cost_microusd;else groups.set(key,{...estimate});}
  return {waitingCapacity:ready.filter(row=>row.plan.execution?.admission_state==='waiting_capacity').length,total:rows.length,ready:ready.length,blocked:rows.length-ready.length,estimates:[...groups.values()],unknown,signature:stableJSON(rows.map(row=>({id:row.id,plan:row.plan?.plan_id||null,requestHash:row.plan?.request_hash||null,snapshot:row.plan?.sourceSnapshot||null,phase:row.phase,estimate:row.plan?.estimate||null,execution:row.plan?.execution||null})))};
}
