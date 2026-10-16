import {useI18n} from './i18n/I18nProvider.jsx';
import './i18n/operator-language.css';
import React,{useEffect,useMemo,useRef,useState} from 'react';
import {createP4OperatorClient} from './p4-operator-client.js';
import {money,time} from './p4-operator-model.js';
import {budgetLabel,budgetUsage,usdInput,usdMicrousd} from './operator-budget-model.js';
import './operator-budget.css';

export function BudgetAccounts({budgets=[],compact=false}){const {t,locale}=useI18n();
  return <div className={'budget-accounts'+(compact?' compact':'')}>{budgets.map(budget=>{const usage=budgetUsage(budget),status=budget.status==='exhausted'||usage?.status==='exhausted'?'exhausted':budget.status==='warning'||usage?.status==='warning'?'warning':'ok';return <section key={budget.id} className={'budget-account '+status} aria-label={budgetLabel(budget,locale)}><header><b>{budgetLabel(budget,locale)}</b>{status!=='ok'&&<span role="status">{status==='exhausted'?t('operator.status95'):t('operator.status96')}</span>}</header><dl>{[[t('operator.status97'),budget.limit_microusd],[t('operator.status98'),budget.spent_microusd],[t('operator.status99'),budget.reserved_microusd],[t('operator.status100'),budget.remaining_microusd]].map(([label,value])=><div key={label}><dt>{label}</dt><dd>{money(value)}</dd></div>)}</dl>{usage&&<progress max="100" value={usage.percent} aria-label={t('operatorPanel.usage',{value:usage.percent})}/>}<small>{t('operator.copy1')}</small>{!compact&&<details><summary>{t('operator.copy2')}</summary>{budget.daily7.length?<ul>{budget.daily7.map(day=><li key={day.date}><time>{day.date}</time><b>{money(day.cost_microusd)}</b></li>)}</ul>:<p>{t('operator.copy3')}</p>}<p>{t('operator.copy4')}</p>{budget.unattributed_legacy&&<p>{t('operatorPanel.legacy',{amount:money(budget.unattributed_legacy.cost_microusd),count:budget.unattributed_legacy.reservation_count??'—'})}</p>}</details>}</section>;})}</div>;
}

// Each mounted view owns an account-bound read client. No budget state is saved
// in the project, draft or generation command journal.
export default function OperatorBudgetPanel({account,compact=false}){const {t,locale}=useI18n();
  const client=useMemo(()=>createP4OperatorClient(),[]),generation=useRef(0),[state,setState]=useState({account:null,value:null,loading:false,error:''});
  async function read(after_id){
    if(!account)return;const current=++generation.current;
    setState(previous=>({account,value:previous.account===account?previous.value:null,loading:true,error:''}));
    try{const value=await client.budgets({after_id});if(value.account!==account)throw Error(t('operator.status101'));if(generation.current===current)setState(previous=>({account,value:after_id&&previous.account===account&&previous.value?{...value,budgets:[...new Map([...previous.value.budgets,...value.budgets].map(budget=>[budget.id,budget])).values()]}:value,loading:false,error:''}));}
    catch(error){if(generation.current===current)setState({account,value:null,loading:false,error:error.message});}
  }
  useEffect(()=>{client.setAccount(account);generation.current++;setState({account,value:null,loading:false,error:''});if(account)void read();const timer=account?setInterval(()=>{if(globalThis.document?.visibilityState!=='hidden')void read();},30000):null;return()=>{clearInterval(timer);generation.current++;client.reset();};},[account,client]);
  const value=state.account===account?state.value:null;
  if(!account)return null;
  return <section className={'operator-budget'+(compact?' compact':'')} aria-label={t('operator.copy5')}><header><div><h3>{t('operator.copy5')}</h3><small>{t('operator.copy6')}</small></div><button type="button" disabled={state.loading} onClick={()=>void read()}>{state.loading?t('operator.status70'):t('operator.status102')}</button></header>{value?<><BudgetAccounts budgets={value.budgets} compact={compact}/><small>{t('operatorPanel.readBudget',{time:time(value.as_of,locale)})}</small>{!value.budgets.length&&<p>{t('operator.copy7')}</p>}{value.next_after_id&&<button type="button" disabled={state.loading} onClick={()=>void read(value.next_after_id)}>{t('operator.copy8')}</button>}</>:<p role={state.error?'alert':'status'}>{state.error||t('operator.status103')}</p>}</section>;
}

function BudgetEditor({row,busy,onSave}){const {t,locale}=useI18n();
  const [input,setInput]=useState(()=>usdInput(row.limit_microusd)),[error,setError]=useState('');
  useEffect(()=>{setInput(usdInput(row.limit_microusd));setError('');},[row.id,row.snapshot]);
  const limit=usdMicrousd(input),minimum=row.spent_microusd+row.reserved_microusd;
  return <section className="budget-admin-row"><h3>{row.owner_id||t('operator.status104')}{row.project_id&&' · '+row.project_id}</h3><small>{row.id}</small><BudgetAccounts budgets={[row]} compact/><form onSubmit={async event=>{event.preventDefault();setError('');try{await onSave(row,limit);}catch(error){setError(error.message);}}}><label>{t('operator.copy9')}<input aria-label={t('operatorPanel.limitAria',{id:row.id})} inputMode="decimal" value={input} disabled={busy} onChange={event=>setInput(event.target.value)}/></label><button type="submit" disabled={busy||limit===null||limit<minimum||limit===row.limit_microusd}>{t('operator.copy10')}</button></form>{limit!==null&&limit<minimum&&<p role="status">{t('operatorPanel.minimum',{amount:money(minimum)})}</p>}{error&&<p role="alert">{error}</p>}</section>;
}
function PriceEditor({row,busy,onSave}){const {t,locale}=useI18n();
  const [max,setMax]=useState(()=>usdInput(row.max_price_microusd)),[ceiling,setCeiling]=useState(()=>usdInput(row.price_ceiling_microusd)),[error,setError]=useState('');
  useEffect(()=>{setMax(usdInput(row.max_price_microusd));setCeiling(usdInput(row.price_ceiling_microusd));setError('');},[row.id,row.snapshot]);
  const defaultValue=usdMicrousd(max),ceilingValue=usdMicrousd(ceiling),valid=defaultValue!==null&&defaultValue>0&&ceilingValue!==null&&ceilingValue>=defaultValue&&ceilingValue<=9000000000000;
  return <section className="budget-admin-row"><h3>{row.id}</h3><small>{t('operatorPanel.account',{owner:row.owner_id})}</small><form onSubmit={async event=>{event.preventDefault();setError('');try{await onSave(row,defaultValue,ceilingValue);}catch(error){setError(error.message);}}}><label>{t('operator.copy11')}<input aria-label={t('operatorPanel.priceAria',{id:row.id})} inputMode="decimal" disabled={busy} value={max} onChange={event=>setMax(event.target.value)}/></label><label>{t('operator.copy12')}<input aria-label={t('operatorPanel.ceilingAria',{id:row.id})} inputMode="decimal" disabled={busy} value={ceiling} onChange={event=>setCeiling(event.target.value)}/></label><button type="submit" disabled={busy||!valid||defaultValue===row.max_price_microusd&&ceilingValue===row.price_ceiling_microusd}>{t('operator.copy13')}</button></form>{!valid&&<p role="status">{t('operator.copy14')}</p>}{error&&<p role="alert">{error}</p>}</section>;
}
export function AdminBudgetPanel({controller,state,busy}){const {t,locale}=useI18n();
  const [notice,setNotice]=useState('');
  useEffect(()=>{void controller.readBudgetAdmin().catch(()=>{});},[controller,state.account]);
  async function save(fn){const result=await fn();setNotice(result.changed?t('operatorPanel.saved',{id:result.audit_event_id||t('operator.status105')}):t('operator.status106'));await controller.readBudgetAdmin();}
  const budgets=state.adminBudgets,prices=state.adminPrices,audits=state.budgetAudits;
  return <section className="operator-budget" aria-label={t('operator.status124')}><header><div><h3>{t('operator.copy15')}</h3><small>{t('operator.copy16')}</small></div><button type="button" disabled={busy} onClick={()=>void controller.readBudgetAdmin().catch(()=>{})}>{t('operator.copy17')}</button></header>{notice&&<p role="status">{notice}</p>}{budgets?<>{budgets.budgets.map(row=><BudgetEditor key={row.id} row={row} busy={busy} onSave={(row,limit)=>save(()=>controller.updateBudget(row,limit))}/>)}{budgets.next_after_id&&<p>{t('operator.copy18')}</p>}</>:<p role="status">{t('operator.copy19')}</p>}<h3>{t('operator.copy20')}</h3>{prices?prices.profiles.map(row=><PriceEditor key={row.id} row={row} busy={busy} onSave={(row,max,ceiling)=>save(()=>controller.updatePrice(row,max,ceiling))}/>):<p>{t('operator.copy21')}</p>}<details className="budget-audits"><summary>{t('operator.copy22')}</summary>{audits?.events?.length?<ul>{audits.events.map(event=><li key={event.id}>{event.actor?.owner_id||event.actor?.actor_id||t('operator.status107')} · {time(event.created_at,locale)} · {event.account_id}<br/>{money(event.old?.limit_microusd)} → {money(event.new?.limit_microusd)} · {event.id}</li>)}</ul>:<p>{t('operator.copy23')}</p>}{audits?.next_before&&<p>{t('operator.copy24')}</p>}</details><details className="budget-audits"><summary>{t('operator.copy25')}</summary>{prices?.audits?.length?<ul>{prices.audits.map(event=><li key={event.id}>{event.actor_owner||event.actor_id} · {time(event.created_at,locale)} · {event.profile_id}<br/>{t('operatorPanel.priceAudit',{oldDefault:money(event.old?.max_price_microusd),newDefault:money(event.new?.max_price_microusd),oldCeiling:money(event.old?.price_ceiling_microusd),newCeiling:money(event.new?.price_ceiling_microusd)})}</li>)}</ul>:<p>{t('operator.copy26')}</p>}</details></section>;
}
