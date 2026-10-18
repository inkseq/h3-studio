import React from 'react';
import {formatMoneyMicros as money} from './money.js';
import {spendingBreakdown,spendingReconciliation} from './spending-breakdown.js';
import {useI18n} from './i18n/I18nProvider.jsx';

export default function SpendingBreakdown({usage,scope}){
  const {t,locale}=useI18n(),view=spendingBreakdown(usage,scope);
  if(!view)return <p className="spend-breakdown-unavailable" role="status">{t('settings.costUnavailable')}</p>;
  const value=view.totals;
  const reconciliation=scope==='account'?spendingReconciliation(usage):null;
  const count=(base,n)=>t(n===1?base+'One':base,{count:n});
  return <div className="spend-breakdown">
    <div className="spend-breakdown-heading"><b>{t(scope==='connection'?'settings.connectionCosts':'settings.accountCosts')}</b><small>{t('settings.costDay',{day:view.day})}</small></div>
    <dl className="spend-breakdown-buckets">
      {[
        ['settings.costConfirmed',value.settled_today_microusd],
        ['settings.costAwaiting',value.awaiting_invoice_estimate_microusd],
        ['settings.costReserved',value.reserved_microusd],
      ].map(([label,amount])=><div key={label}><dt>{t(label)}</dt><dd>{money(amount)}</dd></div>)}
    </dl>
    {value.awaiting_invoice_count>0&&<p className="spend-breakdown-note">{count('settings.costAwaitingCount',value.awaiting_invoice_count)}</p>}
    {value.prior_day_pending_estimate_microusd>0&&<p className="spend-breakdown-note">{t('settings.costPriorPending',{amount:money(value.prior_day_pending_estimate_microusd)})}</p>}
    {(value.unconfirmed_reference_count>0||value.unconfirmed_exposure_microusd>0)&&<p className="spend-breakdown-warning" role="status">{value.unconfirmed_reference_count>0?count('settings.costUnconfirmed',value.unconfirmed_reference_count):t('settings.costUnconfirmedUnknown')}{value.unconfirmed_exposure_microusd>0&&' '+t('settings.costUnconfirmedEstimate',{amount:money(value.unconfirmed_exposure_microusd)})}</p>}
    {value.legacy_settled_unknown_date_microusd>0&&<p className="spend-breakdown-note">{t('settings.costUnknownDate',{amount:money(value.legacy_settled_unknown_date_microusd)})}</p>}
    <details className="spend-breakdown-details"><summary>{t('settings.costDetails')}</summary>
      <dl>
        <div><dt>{t('settings.costMachinesAwaiting')}</dt><dd>{money(view.byKind.instance.awaiting_invoice_estimate_microusd)}</dd></div>
        <div><dt>{t('settings.costJobsAwaiting')}</dt><dd>{money(view.byKind.job.awaiting_invoice_estimate_microusd)}</dd></div>
        <div><dt>{t('settings.costExposure')}</dt><dd>{money(value.exposure_microusd)}</dd></div>
        {scope==='account'&&view.unattributedShared>0&&<div><dt>{t('settings.costUnattributedShared')}</dt><dd>{money(view.unattributedShared)}</dd></div>}
        {scope==='account'&&view.unattributedActor>0&&<div><dt>{t('settings.costUnattributedActor')}</dt><dd>{money(view.unattributedActor)}</dd></div>}
      </dl>
      <p className="spend-breakdown-note">{t('settings.costAllocationHelp')}</p>
      {scope==='account'&&<div className="spend-reconciliation">
        <p className="spend-breakdown-note">{t(reconciliation?'settings.invoiceCheck.'+reconciliation.state:'settings.invoiceCheck.unknown')}</p>
        {reconciliation&&reconciliation.observedAt!==null&&<p className="spend-breakdown-note">{t('settings.invoiceCheckedAt',{date:new Date(reconciliation.observedAt*1000).toLocaleString(locale==='zh'?'zh-CN':'en',{timeZone:'Australia/Sydney'})})}</p>}
        {reconciliation?.pending>0&&<p className="spend-breakdown-note">{t('settings.invoiceCheckedCount',{checked:reconciliation.checked,pending:reconciliation.pending})}</p>}
        {reconciliation&&reconciliation.reportedCost!==null&&<p className="spend-breakdown-note">{t('settings.invoiceReported',{amount:money(reconciliation.reportedCost)})}</p>}
      </div>}
    </details>
  </div>;
}
