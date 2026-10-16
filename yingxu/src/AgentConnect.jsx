import {useI18n} from './i18n/I18nProvider.jsx';
import './i18n/workspace-language.css';
import React,{useEffect,useMemo,useRef,useState} from 'react';
import {ArrowUpRight,Bot,Check,Cloud,Copy,Download,Eye,FileText,KeyRound,Link,Save,ShieldCheck} from 'lucide-react';
import {agentAccessProfiles,agentHandoff,agentKeySuggestion} from './agent-connect-model.js';
import {executionStatus} from './service-model.js';
import './agent-connect.css';

/** Callbacks open existing account/key/activity UI; this component makes no API writes. */
export default function AgentConnect({account,workspace={},projectId,projectTitle,entityId,entityTitle,capabilities,busy=false,onOpenCloud,onSaveCloud,onManageKeys,onOpenActivity,view='/',origin=globalThis.location?.origin||''}){const {t,locale}=useI18n();
  const [profile,setProfile]=useState('edit'),[copyState,setCopyState]=useState('idle'),[showBrief,setShowBrief]=useState(false),[message,setMessage]=useState(''),[working,setWorking]=useState(false);
  const textRef=useRef(null),epoch=useRef(0);
  const handoff=useMemo(()=>agentHandoff({origin,account,workspace,projectId,entityId,profile,view,locale}),[origin,account,workspace.mode,workspace.account,workspace.serverVersion,workspace.dirty,projectId,entityId,profile,view,locale]);
  const execution=executionStatus(capabilities,locale),blocked=busy||working,quick=view==='/freestyle';
  useEffect(()=>{epoch.current++;setCopyState('idle');setMessage('');setWorking(false);return()=>{epoch.current++;};},[account,projectId,entityId,workspace.mode,workspace.account,workspace.serverVersion,workspace.dirty,profile,view]);
  useEffect(()=>{if(copyState==='manual'){textRef.current?.focus();textRef.current?.select();}},[copyState]);
  async function act(callback,failure){if(!callback)return;const current=epoch.current;setWorking(true);setMessage('');try{await callback();}catch{if(current===epoch.current)setMessage(failure);}finally{if(current===epoch.current)setWorking(false);}}
  async function copy(){
    const current=epoch.current;setCopyState('pending');setMessage('');
    try{if(!globalThis.navigator?.clipboard?.writeText)throw Error('Clipboard unavailable');await navigator.clipboard.writeText(handoff.brief);if(current===epoch.current){setCopyState('copied');setMessage(handoff.ready?t('account.status18'):t('account.status19'));}}
    catch{if(current===epoch.current){setShowBrief(true);setCopyState('manual');setMessage(t('account.status20'));}}
  }
  const statusText={
    'signed-out':t('account.status21'),
    local:t('account.status22'),
    'account-mismatch':quick?t('account.status23'):t('account.status24'),
    'missing-project':quick?t('account.status25'):t('account.status26'),
    unsaved:t('account.status27'),
    unverified:quick?t('account.status28'):t('account.status29'),
    ready:quick?t('account.status30'):t('account.status31'),
  }[handoff.status];
  return <section className="agent-connect" aria-labelledby="agent-connect-title">
    <header className="agent-connect-heading"><div className="agent-connect-mark" aria-hidden="true"><Bot size={26}/></div><div><span className="eyebrow">{t('account.copy137')}</span><h1 id="agent-connect-title">{t('account.copy138')}</h1><p>{t('account.fragment4')}{quick?t('account.interface13'):t('account.interface14')}</p></div></header>
    <div className="agent-connect-steps">
      <section className={'agent-connect-step '+(handoff.ready?'is-ready':'')} aria-labelledby="agent-step-workspace"><div className="agent-step-index">01 <Cloud size={18} aria-hidden="true"/></div><h2 id="agent-step-workspace">{t('account.copy139')}</h2><p>{statusText}</p>
        <div className="agent-workspace-state"><span className={'agent-state-pill '+(handoff.ready?'ready':'')}>{handoff.ready?<><Check size={13}/>{t('account.fragment3')}{workspace.serverVersion}</>:handoff.status==='unsaved'?t('account.status32'):handoff.status==='signed-out'?t('account.status33'):handoff.status==='local'?t('account.status34'):t('account.status35')}</span><strong>{projectTitle||(quick?t('account.status36'):t('account.status37'))}</strong>{handoff.ready&&entityId&&entityTitle&&<small>{t('account.fragment2')}{entityTitle}</small>}{account&&<small>{t('account.fragment1')}{account}</small>}</div>
        {handoff.status==='unsaved'?<button className="primary" type="button" disabled={blocked||!onSaveCloud} onClick={()=>act(onSaveCloud,t('account.status38'))}><Save size={15}/>{t('account.copy64')}</button>:!handoff.ready?<button type="button" className="primary" disabled={blocked||!onOpenCloud} onClick={()=>act(onOpenCloud,t('account.status39'))}><Cloud size={15}/>{!account?t('account.interface10'):handoff.status==='local'?(quick?t('account.status41'):t('account.status42')):(quick?t('account.status43'):t('account.status44'))}</button>:<button type="button" disabled={blocked||!onOpenCloud} onClick={()=>act(onOpenCloud,t('account.status40'))}>{quick?t('account.status45'):t('account.status46')}</button>}
      </section>
      <section className="agent-connect-step agent-connect-share" aria-labelledby="agent-step-share"><div className="agent-step-index">02 <Link size={18} aria-hidden="true"/></div><h2 id="agent-step-share">{t('account.copy140')}</h2><p>{handoff.ready?(quick?t('account.interface15'):t('account.interface16')):(quick?t('account.interface17'):t('account.interface18'))}</p><div className="agent-link-preview"><Bot size={17} aria-hidden="true"/><span>{handoff.workUrl||handoff.docsUrl||t('account.status47')}</span></div>
        <button className="primary" type="button" disabled={copyState==='pending'||!handoff.brief} onClick={copy}>{copyState==='copied'?<Check size={15}/>:<Copy size={15}/>} {copyState==='copied'?t('account.status48'):copyState==='pending'?t('account.status49'):handoff.ready?t('account.status50'):t('account.status51')}</button><small>{t('account.copy147')}</small>
      </section>
      <section className="agent-connect-step" aria-labelledby="agent-step-access"><div className="agent-step-index">03 <KeyRound size={18} aria-hidden="true"/></div><h2 id="agent-step-access">{t('account.copy141')}</h2><p>{t('account.copy148')}</p><label className="agent-access-label">{t('account.copy142')}<select value={profile} disabled={blocked} onChange={event=>setProfile(event.target.value)}>{Object.entries(agentAccessProfiles).map(([id,item])=><option key={id} value={id}>{t(item.labelKey)}</option>)}</select></label><p className="agent-access-detail">{handoff.access.detail}</p><button type="button" disabled={blocked||!handoff.ready||!onManageKeys} onClick={()=>act(()=>onManageKeys(agentKeySuggestion(handoff.cloudProject,profile)),t('account.interface19'))}><KeyRound size={15}/>{quick?t('account.status52'):t('account.status53')}</button><small>{quick?t('account.status54'):t('account.status55')}</small></section>
    </div>
    <div className="agent-connect-message" aria-live="polite" role="status">{message}</div>
    <details className="agent-brief" open={showBrief} onToggle={event=>setShowBrief(event.currentTarget.open)}><summary>{t('account.copy143')}</summary><label><span className="visually-hidden">{t('account.copy144')}</span><textarea ref={textRef} readOnly value={handoff.brief} rows={11} spellCheck={false}/></label><p>{t('account.copy149')}</p></details>
    <section className="agent-connect-review" aria-labelledby="agent-review-title"><div className="agent-review-icon" aria-hidden="true"><Eye size={23}/></div><div><h2 id="agent-review-title">{t('account.copy145')}</h2><p>{t('account.copy150')}</p></div><button type="button" disabled={!handoff.cloudProject||blocked||!onOpenActivity} onClick={()=>act(onOpenActivity,t('account.status56'))}>{t('account.copy146')}<ArrowUpRight size={15}/></button></section>
    <div className="agent-connect-footer"><div className="agent-connect-capability"><ShieldCheck size={17} aria-hidden="true"/><div><b>{execution.title}</b><p>{execution.detail}</p><p>{quick?t('account.status57'):t('account.status58')}{t('account.fragment5')}</p></div></div><nav className="agent-connect-docs" aria-label={t('account.accessible14')}><a href="/for-agents/guide.md" target="_blank" rel="noreferrer"><FileText size={15}/>{t('account.accessible19')}<ArrowUpRight size={13}/></a><a href="/for-agents/guide.md?download=true" download><Download size={15}/>{t('account.accessible20')}</a><a href="/for-agents/skill.zip" download><Download size={15}/>{t('account.accessible21')}</a><a href="/for-agents/guide.json" target="_blank" rel="noreferrer">{t('account.accessible22')}</a></nav></div>
  </section>;
}
