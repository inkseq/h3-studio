import {useI18n} from './i18n/I18nProvider.jsx';
import {translate} from './i18n/locale.js';
import React from 'react';
import {duration} from './operator-model.js';

export function deploymentLabel(capabilities,id,locale='zh'){return id?(capabilities?.deployment_profiles||[]).find(p=>p.id===id)?.label||id:translate(locale,'deployment.default');}
export function DeploymentProfileSelect({capabilities,value,onChange,disabled=false,mode}){
  const {t}=useI18n();
  const profiles=capabilities?.deployment_profiles||[],known=profiles.some(p=>p.id===value),profile=profiles.find(p=>p.id===value),support=profile?.generation_support?.[mode];
  if(!profiles.length&&!value)return null;
  return <label className="qc-field qc-deployment-field"><span>{t('deployment.title')}</span><select aria-label={t('deployment.title')} value={value||''} disabled={disabled} onChange={e=>onChange(e.target.value||null)}><option value="">{t('deployment.default')}</option>{value&&!known&&<option value={value}>{t('deployment.private')}</option>}{profiles.map(p=><option key={p.id} value={p.id}>{p.label}</option>)}</select>{support?.enabled===false&&<small>{t('deployment.disabled')}</small>}<small>{t('deployment.help')}</small></label>;
}
export function HistoricalTiming({hint}){
 const {t,locale}=useI18n();
  if(!hint||hint.scope!=='historical_exact_case'||!hint.cases?.length)return null;
  return <details className="qc-timing-hint"><summary>{t('deployment.history')}</summary><ul>{hint.cases.flatMap(row=>(row.measurements||[]).map((m,index)=><li key={row.id+'-'+index}>{t('deployment.measurement',{width:row.width,height:row.height,frames:row.frames,steps:row.steps})}<b>{duration(m.total_seconds,locale)}</b><small>{t('deployment.samples',{date:m.measured_at?.slice(0,10),count:m.sample_count})}</small></li>))}</ul><small>{t('deployment.historyHelp')}</small></details>;
}
