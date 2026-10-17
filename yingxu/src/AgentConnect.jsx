import React,{useEffect,useMemo,useRef,useState} from 'react';
import {ArrowUpRight,Bot,Check,Cloud,Copy,Download,Eye,FileText,KeyRound,Link,Save,ShieldCheck} from 'lucide-react';
import {agentAccessProfiles,agentHandoff,agentKeySuggestion} from './agent-connect-model.js';
import {executionStatus} from './service-model.js';
import './agent-connect.css';

/** Callbacks open existing account/key/activity UI; this component makes no API writes. */
export default function AgentConnect({account,workspace={},projectId,projectTitle,entityId,entityTitle,capabilities,busy=false,onOpenCloud,onSaveCloud,onManageKeys,onOpenActivity,view='/',origin=globalThis.location?.origin||''}){
  const [profile,setProfile]=useState('edit'),[copyState,setCopyState]=useState('idle'),[showBrief,setShowBrief]=useState(false),[message,setMessage]=useState(''),[working,setWorking]=useState(false);
  const textRef=useRef(null),epoch=useRef(0);
  const handoff=useMemo(()=>agentHandoff({origin,account,workspace,projectId,entityId,profile,view}),[origin,account,workspace.mode,workspace.account,workspace.serverVersion,workspace.dirty,projectId,entityId,profile,view]);
  const execution=executionStatus(capabilities),blocked=busy||working,quick=view==='/freestyle';
  useEffect(()=>{epoch.current++;setCopyState('idle');setMessage('');setWorking(false);return()=>{epoch.current++;};},[account,projectId,entityId,workspace.mode,workspace.account,workspace.serverVersion,workspace.dirty,profile,view]);
  useEffect(()=>{if(copyState==='manual'){textRef.current?.focus();textRef.current?.select();}},[copyState]);
  async function act(callback,failure){if(!callback)return;const current=epoch.current;setWorking(true);setMessage('');try{await callback();}catch{if(current===epoch.current)setMessage(failure);}finally{if(current===epoch.current)setWorking(false);}}
  async function copy(){
    const current=epoch.current;setCopyState('pending');setMessage('');
    try{if(!globalThis.navigator?.clipboard?.writeText)throw Error('Clipboard unavailable');await navigator.clipboard.writeText(handoff.brief);if(current===epoch.current){setCopyState('copied');setMessage(handoff.ready?'已复制工作说明。粘贴给 AI 后，在本地为它配置授权。':'已复制入门说明；其中没有本机作品内容或工作位置。');}}
    catch{if(current===epoch.current){setShowBrief(true);setCopyState('manual');setMessage('浏览器未允许自动复制。下方说明已选中，请按 Ctrl+C 或 ⌘C 手动复制。');}}
  }
  const statusText={
    'signed-out':'先登录，让你和 AI 使用同一份云作品。',
    local:'这份作品只保存在本机。先复制到云端，AI 才能读取和保存结果。',
    'account-mismatch':quick?'当前作品与登录账户不一致。请重新打开属于这个账户的快速创作。':'当前作品与登录账户不一致。请重新打开属于这个账户的云故事。',
    'missing-project':quick?'当前作品位置尚未确认，请重新打开快速创作。':'当前云故事位置尚未确认，请重新打开故事。',
    unsaved:'有修改还没同步。先保存云版本，再把最新工作位置交给 AI。',
    unverified:quick?'尚未确认云版本，请重新打开快速创作后继续。':'尚未确认云版本，请重新打开云故事后继续。',
    ready:quick?'你和 AI 将使用同一份云作品。每次保存的提示词、参考素材与结果，都可以回到这里查看。':'你和 AI 将使用同一个云故事。每次保存的修改，都可以回到这里查看。',
  }[handoff.status];
  return <section className="agent-connect" aria-labelledby="agent-connect-title">
    <header className="agent-connect-heading"><div className="agent-connect-mark" aria-hidden="true"><Bot size={26}/></div><div><span className="eyebrow">AI 创作伙伴</span><h1 id="agent-connect-title">让 AI 创作，你来掌镜。</h1><p>把工作链接交给 Codex 或其他 Agent。{quick?'它通过 API 准备这个视频的提示词和参考素材，你在这里看进展、选结果，再继续调整。':'它通过 API 写入故事，你在映序里看进展、选结果，再继续调整。'}</p></div></header>
    <div className="agent-connect-steps">
      <section className={'agent-connect-step '+(handoff.ready?'is-ready':'')} aria-labelledby="agent-step-workspace"><div className="agent-step-index">01 <Cloud size={18} aria-hidden="true"/></div><h2 id="agent-step-workspace">准备共同的作品</h2><p>{statusText}</p>
        <div className="agent-workspace-state"><span className={'agent-state-pill '+(handoff.ready?'ready':'')}>{handoff.ready?<><Check size={13}/>云版本 {workspace.serverVersion}</>:handoff.status==='unsaved'?'待保存云版本':handoff.status==='signed-out'?'尚未登录':handoff.status==='local'?'本机作品':'需核对账户'}</span><strong>{projectTitle||(quick?'尚未选择快速创作':'尚未选择故事')}</strong>{handoff.ready&&entityId&&entityTitle&&<small>定位到：{entityTitle}</small>}{account&&<small>当前账户：{account}</small>}</div>
        {handoff.status==='unsaved'?<button className="primary" type="button" disabled={blocked||!onSaveCloud} onClick={()=>act(onSaveCloud,'云版本尚未保存，请检查页面上的同步或冲突提示。')}><Save size={15}/>保存云版本</button>:!handoff.ready?<button type="button" className="primary" disabled={blocked||!onOpenCloud} onClick={()=>act(onOpenCloud,'云工作室暂未打开，请从右上角登录入口继续。')}><Cloud size={15}/>{!account?'登录云工作室':handoff.status==='local'?(quick?'打开 / 创建快速创作':'打开 / 创建云故事'):(quick?'重新选择快速创作':'重新选择云故事')}</button>:<button type="button" disabled={blocked||!onOpenCloud} onClick={()=>act(onOpenCloud,'故事列表暂未打开，请从云工作室入口继续。')}>{quick?'换一份快速创作':'换一个云故事'}</button>}
      </section>
      <section className="agent-connect-step agent-connect-share" aria-labelledby="agent-step-share"><div className="agent-step-index">02 <Link size={18} aria-hidden="true"/></div><h2 id="agent-step-share">把工作位置交给 AI</h2><p>{handoff.ready?(quick?'说明中有使用方法和当前视频的位置。告诉 AI 你想要的画面、动作和声音，让它准备提示词与参考素材。':'说明中有使用方法和当前工作位置。接下来，直接告诉 AI 你想改哪一段、想看到什么效果。'):(quick?'现在也可以让 AI 先了解映序。当前复制的只是公开入门说明；保存快速创作后再复制工作位置。':'现在也可以让 AI 先了解映序。当前复制的只是公开入门说明；保存云故事后再复制工作位置。')}</p><div className="agent-link-preview"><Bot size={17} aria-hidden="true"/><span>{handoff.workUrl||handoff.docsUrl||'当前网站地址无法用于分享'}</span></div>
        <button className="primary" type="button" disabled={copyState==='pending'||!handoff.brief} onClick={copy}>{copyState==='copied'?<Check size={15}/>:<Copy size={15}/>} {copyState==='copied'?'已复制说明':copyState==='pending'?'正在复制…':handoff.ready?'复制给 AI 的工作说明':'复制给 AI 的入门说明'}</button><small>只包含公开文档链接、工作位置和使用约定。没有密钥、故事正文或素材。</small>
      </section>
      <section className="agent-connect-step" aria-labelledby="agent-step-access"><div className="agent-step-index">03 <KeyRound size={18} aria-hidden="true"/></div><h2 id="agent-step-access">决定它能做什么</h2><p>链接提供位置，API Key 提供权限。为这个 Agent 单独创建一把 Key，之后可随时撤销。</p><label className="agent-access-label">建议的权限范围<select value={profile} disabled={blocked} onChange={event=>setProfile(event.target.value)}>{Object.entries(agentAccessProfiles).map(([id,item])=><option key={id} value={id}>{item.label}</option>)}</select></label><p className="agent-access-detail">{handoff.access.detail}</p><button type="button" disabled={blocked||!handoff.ready||!onManageKeys} onClick={()=>act(()=>onManageKeys(agentKeySuggestion(handoff.cloudProject,profile)),'Key 管理暂未打开。请到云工作室中的 Agent API Key 管理继续。')}><KeyRound size={15}/>{quick?'为这份作品配置 Key':'为这个故事配置 Key'}</button><small>{quick?'建议仅授权这份作品、7 天有效。创建前可核对和修改，不会自动授权。':'建议仅授权当前故事、7 天有效。创建前可核对和修改，不会自动授权。'}</small></section>
    </div>
    <div className="agent-connect-message" aria-live="polite" role="status">{message}</div>
    <details className="agent-brief" open={showBrief} onToggle={event=>setShowBrief(event.currentTarget.open)}><summary>查看将交给 AI 的说明</summary><label><span className="visually-hidden">给 AI 的工作说明，可手动复制</span><textarea ref={textRef} readOnly value={handoff.brief} rows={11} spellCheck={false}/></label><p>复制动作由你触发。API Key 请放入 Agent 运行环境的安全凭据配置中，不要粘贴到聊天、工作链接或故事里。</p></details>
    <section className="agent-connect-review" aria-labelledby="agent-review-title"><div className="agent-review-icon" aria-hidden="true"><Eye size={23}/></div><div><h2 id="agent-review-title">创作过程留在这里，决定权也在这里。</h2><p>任务会回到原来的镜头。打开对应位置可检查提示词、参考素材和候选结果；调整后单独重做。云端有新版时，先比较、再载入，正在编辑的草稿会保留。</p></div><button type="button" disabled={!handoff.cloudProject||blocked||!onOpenActivity} onClick={()=>act(onOpenActivity,'创作动态暂未打开，请从作品中的任务入口查看。')}>查看创作动态 <ArrowUpRight size={15}/></button></section>
    <div className="agent-connect-footer"><div className="agent-connect-capability"><ShieldCheck size={17} aria-hidden="true"/><div><b>{execution.title}</b><p>{execution.detail}</p><p>{quick?'Agent 可以用自己的 AI 准备提示词与参考素材，再通过 API 保存。':'Agent 可以用自己的 AI 编写剧本，再通过 API 保存。'}映序当前不提供内置 AI 编剧、图像 / 音乐生成或多人实时协作。</p></div></div><nav className="agent-connect-docs" aria-label="Agent 使用资料"><a href="/for-agents/guide.md" target="_blank" rel="noreferrer"><FileText size={15}/>阅读 Agent 文本指南 <ArrowUpRight size={13}/></a><a href="/for-agents/guide.md?download=true" download><Download size={15}/>下载 Markdown</a><a href="/for-agents/skill.zip" download><Download size={15}/>下载 Skill 与工具包</a><a href="/for-agents/guide.json" target="_blank" rel="noreferrer">API 流程与能力</a></nav></div>
  </section>;
}
