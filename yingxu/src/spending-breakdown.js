const fields=['settled_today_microusd','reserved_microusd','awaiting_invoice_estimate_microusd',
  'awaiting_invoice_count','prior_day_pending_estimate_microusd','legacy_settled_unknown_date_microusd',
  'exposure_microusd','unconfirmed_exposure_microusd','unconfirmed_reference_count'];
const amount=value=>Number.isSafeInteger(value)&&value>=0;
const bucket=value=>value&&fields.every(field=>amount(value[field]))
  ?Object.fromEntries(fields.map(field=>[field,value[field]])):null;

// Presentation only. The server remains authoritative for attribution, day
// boundaries, reconciliation and admission; never derive these from a cap total.
export function spendingBreakdown(usage,scope){
  const value=usage?.breakdown;
  if(!['account','connection'].includes(scope)||value?.schema_version!==1||value.attribution_scope!==scope
      ||usage.timezone!=='Australia/Sydney'||typeof usage.day!=='string'||!/^\d{4}-\d{2}-\d{2}$/.test(usage.day))return null;
  const totals=bucket(value),job=bucket(value.by_kind?.job),instance=bucket(value.by_kind?.instance);
  if(!totals||!job||!instance)return null;
  const result={day:usage.day,scope,totals,byKind:{job,instance}};
  if(scope==='account'){
    if(!amount(value.unattributed_exposure_microusd)||!amount(value.unattributed_actor_exposure_microusd))return null;
    result.unattributedShared=value.unattributed_exposure_microusd;
    result.unattributedActor=value.unattributed_actor_exposure_microusd;
  }
  return result;
}

export function admissionExposure(usage,limit){
  if(!amount(usage?.projected_microusd)||!amount(limit))return null;
  return {amount:usage.projected_microusd,limit,percent:limit>0?Math.min(100,usage.projected_microusd/limit*100):null};
}

export function spendingReconciliation(usage){
  const value=usage?.reconciliation,instance=value?.instance;
  if(!instance||!['not_needed','not_checked','current','overdue','unavailable'].includes(instance.state)
      ||!amount(instance.pending_count)||!amount(instance.checked_count)
      ||instance.final_cost_confirmed!==false
      ||instance.reported_cost_microusd!==null&&!amount(instance.reported_cost_microusd)
      ||instance.observed_at!==null&&(typeof instance.observed_at!=='number'||!Number.isFinite(instance.observed_at)
        ||instance.observed_at<0||!Number.isFinite(new Date(instance.observed_at*1000).getTime())))return null;
  return {state:instance.state,pending:instance.pending_count,checked:instance.checked_count,
    observedAt:instance.observed_at,reportedCost:instance.reported_cost_microusd};
}
