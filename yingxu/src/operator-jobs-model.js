export const cancellationReasons=[
  {code:'operator_queue_cleared',label:'管理员清理队列'},
  {code:'operator_request_cancelled',label:'管理员取消请求'},
  {code:'capacity_unavailable',label:'暂时没有可用算力'},
];
const snapshot=value=>typeof value==='string'&&/^[a-f0-9]{64}$/.test(value);
const identity=value=>typeof value==='string'&&value.length>0&&value.length<=200;
export const canCancelJob=job=>identity(job?.id)&&identity(job.owner_id)&&['queued','waiting_capacity'].includes(job.status)&&job.cancellable===true&&snapshot(job.snapshot);
export function validAdminJobs(value,account){
  return value?.operator?.account===account&&value.operator.admin===true&&Array.isArray(value.jobs)&&value.jobs.length<=100&&
    new Set(value.jobs.map(job=>job?.id)).size===value.jobs.length&&value.jobs.every(job=>identity(job?.id)&&identity(job.owner_id)&&typeof job.status==='string'&&typeof job.cancellable==='boolean'&&snapshot(job.snapshot))&&
    (value.next_after_id===null||value.next_after_id===undefined||identity(value.next_after_id));
}
export function cancellationBody(jobs,reason){
  if(!Array.isArray(jobs)||!jobs.length||jobs.length>100||new Set(jobs.map(job=>job?.id)).size!==jobs.length||!jobs.every(canCancelJob)||!cancellationReasons.some(item=>item.code===reason))return null;
  return {jobs:jobs.map(job=>({id:job.id,expected_snapshot:job.snapshot})),reason_code:reason};
}
export function validCancellationBody(body){
  return body&&Object.keys(body).sort().join(',')==='jobs,reason_code'&&cancellationReasons.some(item=>item.code===body.reason_code)&&Array.isArray(body.jobs)&&body.jobs.length>0&&body.jobs.length<=100&&new Set(body.jobs.map(job=>job?.id)).size===body.jobs.length&&body.jobs.every(job=>job&&Object.keys(job).sort().join(',')==='expected_snapshot,id'&&identity(job.id)&&snapshot(job.expected_snapshot));
}
export function cancellationReceipt(value,body){
  return identity(value?.command_id)&&value.state==='completed'&&value.reason_code===body.reason_code&&Array.isArray(value.cancelled_job_ids)&&
    value.cancelled_job_ids.length===body.jobs.length&&new Set(value.cancelled_job_ids).size===body.jobs.length&&body.jobs.every(job=>value.cancelled_job_ids.includes(job.id));
}
export function selectionCurrent(selected,jobs){
  return selected.length>0&&selected.length<=100&&selected.every(original=>jobs.some(job=>job.id===original.id&&job.owner_id===original.owner_id&&job.snapshot===original.snapshot&&canCancelJob(job)));
}
export const jobStatus=status=>({queued:'队列中',waiting_capacity:'等待算力',running:'运行中',collecting:'保存结果中',reconciling:'结果待核对',cancel_requested:'取消待核对',cancelled:'已取消',succeeded:'已完成',failed:'已失败'}[status]||'状态待核对');
