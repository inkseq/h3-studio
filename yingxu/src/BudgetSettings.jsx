import React,{useEffect,useMemo,useSyncExternalStore} from 'react';
import {useCloud} from './CloudStudio.jsx';
import {cloudController as cloud} from './cloud-controller.js';
import {createP4OperatorController} from './p4-operator-controller.js';
import OperatorBudgetPanel,{AdminBudgetPanel} from './OperatorBudgetPanel.jsx';
import './budget-settings.css';

/** Compatibility entry for the existing audited budget tools. */
export default function BudgetSettings(){
  const account=useCloud().account,controller=useMemo(()=>createP4OperatorController(),[]);
  const state=useSyncExternalStore(controller.subscribe,controller.getState,controller.getState);
  useEffect(()=>{document.title='映序 · 设置';void cloud.initialize().catch(()=>{});return()=>controller.destroy();},[controller]);
  useEffect(()=>{void controller.setAccount(account);},[account,controller]);
  const current=state.account===account,admin=current&&state.snapshot?.operator?.admin===true;
  const adminTab=new URLSearchParams(globalThis.location?.search||'').get('tab')==='admin';
  return <main className="budget-settings-shell">
    <header><div><h1>设置</h1><p>预算与已记录费用</p></div><a href="/operator">返回 GPU 控制台</a></header>
    {!account?<p><a href="/operator">登录后查看预算设置</a></p>:<>
      <nav aria-label="设置分类"><a href="/settings" aria-current={!adminTab?'page':undefined}>我的预算</a>{admin&&<a href="/settings?tab=admin" aria-current={adminTab?'page':undefined}>管理员</a>}</nav>
      {state.error&&current&&<p role="alert">{state.error}</p>}
      {adminTab?(admin?<AdminBudgetPanel controller={controller} state={state} busy={state.busy||!!state.pending}/>:<p role="status">{!current||state.loading?'正在核对管理员权限…':'此账户没有管理员预算权限。'}</p>):<OperatorBudgetPanel account={account}/>}
      {state.pending&&current&&admin&&<section className="operator-budget" role="alert"><p>原管理操作结果待核对，保留原操作标识。</p><button disabled={state.busy} onClick={()=>void controller.recover().catch(()=>{})}>核对原操作</button></section>}
    </>}
  </main>;
}
