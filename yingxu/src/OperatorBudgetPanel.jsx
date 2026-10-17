import React,{useEffect,useMemo,useRef,useState} from 'react';
import {createP4OperatorClient} from './p4-operator-client.js';
import {money,time} from './p4-operator-model.js';
import {budgetLabel,budgetUsage,usdInput,usdMicrousd} from './operator-budget-model.js';
import './operator-budget.css';

export function BudgetAccounts({budgets=[],compact=false}){
  return <div className={'budget-accounts'+(compact?' compact':'')}>{budgets.map(budget=>{
    const retired=budget.limit_mode==='settings'||budget.legacy_projection_only===true,usage=budgetUsage(budget),pending=budget.invoice_pending;
    const status=retired?'ok':budget.status==='exhausted'||usage?.status==='exhausted'?'exhausted':budget.status==='warning'||usage?.status==='warning'?'warning':'ok';
    const amounts=retired?[['已结算',budget.spent_microusd],['执行中预留',budget.reserved_microusd]]:[['累计额度',budget.limit_microusd],['已结算',budget.spent_microusd],['已预留',budget.reserved_microusd],['剩余额度',budget.remaining_microusd]];
    return <section key={budget.id} className={'budget-account '+status} aria-label={budgetLabel(budget)}>
      <header><b>{budgetLabel(budget)}</b>{status!=='ok'&&<span role="status">{status==='exhausted'?'额度不足':'额度已用或预留达 80%'}</span>}</header>
      {retired&&<p>原固定额度已停用，不再限制启动；支出限制以设置为准。</p>}
      <dl>{amounts.map(([label,value])=><div key={label}><dt>{label}</dt><dd>{money(value)}</dd></div>)}</dl>
      {pending?.reservation_count>0&&<p className="budget-pending">待核账单预估 {money(pending.reserved_estimate_microusd)} · {pending.reservation_count} 项<small>实际费用尚未确认，预估仍计入设置中的使用量。</small></p>}
      {usage&&<progress max="100" value={usage.percent} aria-label={'已结算与预留占额度 '+usage.percent+'%'}/>}<small>预留不是实际账单。</small>
      {!compact&&<details><summary>最近七日已记录费用 · UTC</summary>{budget.daily7.length?<ul>{budget.daily7.map(day=><li key={day.date}><time>{day.date}</time><b>{money(day.cost_microusd)}</b></li>)}</ul>:<p>暂无有结算日期的费用记录。</p>}<p>仅列已有结算日期；没有记录的日期不视为零费用。</p>{budget.unattributed_legacy&&<p>旧记录日期未确认：已结算 {money(budget.unattributed_legacy.cost_microusd)} · {budget.unattributed_legacy.reservation_count??'—'} 项预留记录。</p>}</details>}
    </section>;
  })}</div>;
}

export function BudgetWorkspace({account,controller,state,busy}){
  return <><OperatorBudgetPanel account={account}/>{state.account===account&&state.snapshot?.operator?.admin===true&&<><p><a href="/settings?tab=admin">设置 · 管理员</a></p><AdminBudgetPanel controller={controller} state={state} busy={busy}/></>}</>;
}

// Each mounted view owns an account-bound read client. No budget state is saved
// in the project, draft or generation command journal.
export default function OperatorBudgetPanel({account,compact=false}){
  const client=useMemo(()=>createP4OperatorClient(),[]),generation=useRef(0),[state,setState]=useState({account:null,value:null,loading:false,error:''});
  async function read(after_id){
    if(!account)return;const current=++generation.current;
    setState(previous=>({account,value:previous.account===account?previous.value:null,loading:true,error:''}));
    try{const value=await client.budgets({after_id});if(value.account!==account)throw Error('登录账户已改变，原预算未展示。');if(generation.current===current)setState(previous=>({account,value:after_id&&previous.account===account&&previous.value?{...value,budgets:[...new Map([...previous.value.budgets,...value.budgets].map(budget=>[budget.id,budget])).values()]}:value,loading:false,error:''}));}
    catch(error){if(generation.current===current)setState({account,value:null,loading:false,error:error.message});}
  }
  useEffect(()=>{client.setAccount(account);generation.current++;setState({account,value:null,loading:false,error:''});if(account)void read();const timer=account?setInterval(()=>{if(globalThis.document?.visibilityState!=='hidden')void read();},30000):null;return()=>{clearInterval(timer);generation.current++;client.reset();};},[account,client]);
  const value=state.account===account?state.value:null;
  if(!account)return null;
  return <section className={'operator-budget'+(compact?' compact':'')} aria-label="我的累计预算"><header><div><h3>我的累计预算</h3><small>USD · 不按月自动重置</small></div><button type="button" disabled={state.loading} onClick={()=>void read()}>{state.loading?'读取中…':'刷新额度'}</button></header>{value?<><BudgetAccounts budgets={value.budgets} compact={compact}/><small>读取于 {time(value.as_of)}；提交时仍由服务核对原额度。</small>{!value.budgets.length&&<p>尚未配置账户额度；不能把它视为无限额度。</p>}{value.next_after_id&&<button type="button" disabled={state.loading} onClick={()=>void read(value.next_after_id)}>读取更多账户额度</button>}</>:<p role={state.error?'alert':'status'}>{state.error||'正在读取原账户额度…'}</p>}</section>;
}

function BudgetEditor({row,busy,onSave}){
  const [input,setInput]=useState(()=>usdInput(row.limit_microusd)),[error,setError]=useState('');
  useEffect(()=>{setInput(usdInput(row.limit_microusd));setError('');},[row.id,row.snapshot]);
  const limit=usdMicrousd(input),minimum=row.spent_microusd+row.reserved_microusd;
  const retired=row.limit_mode==='settings'||row.legacy_projection_only===true;
  return <section className="budget-admin-row"><h3>{budgetLabel(row)}</h3><details><summary>账户标识</summary><small>{row.id}</small></details><BudgetAccounts budgets={[row]} compact/><form onSubmit={async event=>{event.preventDefault();setError('');try{await onSave(row,limit);}catch(error){setError(error.message);}}}><label>{retired?'历史累计额度 · USD（不限制启动）':'累计额度 · USD'}<input aria-label={row.id+' 累计额度 USD'} inputMode="decimal" value={input} disabled={busy} onChange={event=>setInput(event.target.value)}/></label><button type="submit" disabled={busy||limit===null||limit<minimum||limit===row.limit_microusd}>保存额度</button></form>{limit!==null&&limit<minimum&&<p role="status">额度不能低于已结算与预留合计 {money(minimum)}。</p>}{error&&<p role="alert">{error}</p>}</section>;
}
function PriceEditor({row,busy,onSave}){
  const [max,setMax]=useState(()=>usdInput(row.max_price_microusd)),[ceiling,setCeiling]=useState(()=>usdInput(row.price_ceiling_microusd)),[error,setError]=useState('');
  useEffect(()=>{setMax(usdInput(row.max_price_microusd));setCeiling(usdInput(row.price_ceiling_microusd));setError('');},[row.id,row.snapshot]);
  const defaultValue=usdMicrousd(max),ceilingValue=usdMicrousd(ceiling),valid=defaultValue!==null&&defaultValue>0&&ceilingValue!==null&&ceilingValue>=defaultValue&&ceilingValue<=9000000000000;
  return <section className="budget-admin-row"><h3>{row.id}</h3><small>账户 {row.owner_id}</small><form onSubmit={async event=>{event.preventDefault();setError('');try{await onSave(row,defaultValue,ceilingValue);}catch(error){setError(error.message);}}}><label>默认每 GPU 上限 · USD/小时<input aria-label={row.id+' 默认每 GPU 价格上限'} inputMode="decimal" disabled={busy} value={max} onChange={event=>setMax(event.target.value)}/></label><label>允许的最高价格 · USD/小时<input aria-label={row.id+' 管理员价格上限'} inputMode="decimal" disabled={busy} value={ceiling} onChange={event=>setCeiling(event.target.value)}/></label><button type="submit" disabled={busy||!valid||defaultValue===row.max_price_microusd&&ceilingValue===row.price_ceiling_microusd}>保存价格策略</button></form>{!valid&&<p role="status">默认价格须大于零，并且不超过管理员最高价格。</p>}{error&&<p role="alert">{error}</p>}</section>;
}
export function AdminBudgetPanel({controller,state,busy}){
  const [notice,setNotice]=useState(''),[reading,setReading]=useState(false);
  const authorized=state.snapshot?.operator?.admin===true;
  async function read(after_id){setReading(true);try{await controller.readBudgetAdmin({after_id});}catch{}finally{setReading(false);}}
  useEffect(()=>{if(authorized)void read();},[controller,state.account,authorized]);
  async function save(fn){const result=await fn();setNotice(result.changed?'修改已保存 · 审核编号 '+(result.audit_event_id||'待核对'):'数值未改变，原余额保留。');await controller.readBudgetAdmin();}
  const budgets=state.adminBudgets,prices=state.adminPrices,audits=state.budgetAudits;
  if(!authorized)return null;
  return <section className="operator-budget" aria-label="管理员预算与价格"><header><div><h3>预算与价格管理</h3><small>所有账户，包含共享 GPU 与共享生成预算。保存仍由服务核对原余额和审核记录。</small></div><button type="button" disabled={busy||reading} onClick={()=>void read()}>{reading?'读取中…':'刷新管理记录'}</button></header>{notice&&<p role="status">{notice}</p>}{budgets?<>{budgets.budgets.map(row=><BudgetEditor key={row.id} row={row} busy={busy||reading} onSave={(row,limit)=>save(()=>controller.updateBudget(row,limit))}/>)}<p>已读取 {budgets.budgets.length} 个预算账户{budgets.next_after_id?'，还有更多账户。':'，全部账户已读取。'}</p>{budgets.next_after_id&&<button type="button" disabled={busy||reading} onClick={()=>void read(budgets.next_after_id)}>读取更多预算账户</button>}</>:<p role="status">管理员预算尚未读取，请刷新。</p>}<h3>每 GPU 价格策略</h3>{prices?prices.profiles.map(row=><PriceEditor key={row.id} row={row} busy={busy||reading} onSave={(row,max,ceiling)=>save(()=>controller.updatePrice(row,max,ceiling))}/>):<p>价格策略尚未读取。</p>}<details className="budget-audits"><summary>已读取的额度审核记录</summary>{audits?.events?.length?<ul>{audits.events.map(event=><li key={event.id}>{event.actor?.owner_id||event.actor?.actor_id||'管理员'} · {new Date(event.created_at*1000).toLocaleString()} · {event.account_id}<br/>{money(event.old?.limit_microusd)} → {money(event.new?.limit_microusd)} · {event.id}</li>)}</ul>:<p>暂无已读取的额度修改记录。</p>}{audits?.next_before&&<p>这里只列最近读取的审核记录，不代表全部历史。</p>}</details><details className="budget-audits"><summary>已读取的价格审核记录</summary>{prices?.audits?.length?<ul>{prices.audits.map(event=><li key={event.id}>{event.actor_owner||event.actor_id} · {new Date(event.created_at*1000).toLocaleString()} · {event.profile_id}<br/>默认 {money(event.old?.max_price_microusd)} → {money(event.new?.max_price_microusd)}；最高 {money(event.old?.price_ceiling_microusd)} → {money(event.new?.price_ceiling_microusd)} /GPU/小时</li>)}</ul>:<p>暂无已读取的价格修改记录。</p>}</details></section>;
}
