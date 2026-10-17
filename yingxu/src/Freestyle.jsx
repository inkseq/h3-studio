import React,{useEffect,useRef,useState,useSyncExternalStore} from 'react';
import {Cloud,Upload,ArrowLeft,Image,Video,Music,Film,X,Plus,Bot,Activity} from 'lucide-react';
import {store,ancestors} from './store.js';
import CandidateReview from './CandidateReview.jsx';
import {selectedRecipe} from './cloud-model.js';
import H3Inputs from './H3Inputs.jsx';
import {navigateWorkspace,workspaceHref} from './workspace-navigation.js';
import {agentLinkSearch,freestyleEntityTarget,freestyleLinkState} from './agent-navigation.js';
import AgentLink from './AgentLink.jsx';
import AgentConnect from './AgentConnect.jsx';
import ProjectActivity from './ProjectActivity.jsx';
import {CloudButton,CloudProjects,CloudWorkspaceBar,CloudShotPanel,useCloud} from './CloudStudio.jsx';
import {cloudController as cloud} from './cloud-controller.js';
import './freestyle.css';
import RecipeExecutionNotice from './RecipeExecutionNotice.jsx';
import {ServiceStatus} from './ServiceStatus.jsx';

const run=promise=>promise.catch(()=>{}),names={image:'图片',video:'视频',audio:'音频'},icons={image:Image,video:Video,audio:Music};
function FreestyleDialog({title,onClose,children}){
  const ref=useRef(),opener=useRef(document.activeElement);
  useEffect(()=>{const element=ref.current;element.showModal();element.querySelector('input:not([type=file]),textarea,button')?.focus();return()=>{element.close();opener.current?.isConnected&&opener.current.focus();};},[]);
  return <dialog className="modal wide" ref={ref} aria-label={title} onCancel={e=>{e.preventDefault();onClose();}}><div className="modal-heading"><h2>{title}</h2><button className="icon-button" aria-label="关闭窗口" onClick={onClose}><X size={18}/></button></div>{children}</dialog>;
}

export default function Freestyle(){
  const state=useSyncExternalStore(store.subscribe,store.getState,store.getState),c=useCloud(),[dialog,setDialog]=useState(null),[shotId,setShotId]=useState(''),[panel,setPanel]=useState('create'),[linkedEntity,setLinkedEntity]=useState(null),[search,setSearch]=useState(()=>window.location.search),project=state.project,cloudMode=state.workspace.mode==='cloud',shots=cloudMode?project.entities.filter(e=>e.type==='shot'):[],shot=shots.find(e=>e.id===shotId)||shots[0],recipes=c.capabilities?.recipes||[],recipe=selectedRecipe(recipes,shot?.data.h3),mode=recipe?.mode,clipLimits=recipe?.limits?`${recipe.limits.min_clip_duration}–${recipe.limits.max_clip_duration} 秒`: '以服务声明为准';
  const open=()=>setDialog({type:'cloud'}),connect=()=>setDialog({type:'agent'}),showActivity=()=>{setPanel('activity');setDialog(null);};
  const newQuick=()=>{if(!c.account){open();return;}run(cloud.createQuick());};
  const locate=id=>{const target=freestyleEntityTarget(store.getState().project,id);if(!target)return false;if(target.shotId){setShotId(target.shotId);setPanel('create');setLinkedEntity(null);requestAnimationFrame(()=>document.querySelector('.freestyle-controls')?.scrollIntoView({behavior:'smooth'}));}else setLinkedEntity(target.entityId);return true;};
  const review=id=>{if(locate(id))requestAnimationFrame(()=>document.querySelector('.freestyle-results')?.scrollIntoView({behavior:'smooth'}));};
  const workSearch=cloudMode?agentLinkSearch({projectId:project.id,entityId:shot?.id,activity:panel==='activity'}):search,storyHref=workspaceHref('/',workSearch);
  useEffect(()=>{const update=()=>setSearch(window.location.search);window.addEventListener('popstate',update);return()=>window.removeEventListener('popstate',update);},[]);
  useEffect(()=>{const link=freestyleLinkState(store.getState(),search,c.account);if(!link)return;if(link.target?.shotId)setShotId(link.target.shotId);if(link.target&&!link.target.shotId)setLinkedEntity(link.target.entityId);setPanel(link.activity?'activity':'create');},[search,c.account,project.id,state.workspaceEpoch]);
  useEffect(()=>{document.title='映序 · 快速视频创作';},[]);
  useEffect(()=>{if(!state.notice)return;const timer=setTimeout(()=>store.notify(''),5500);return()=>clearTimeout(timer);},[state.notice]);
  return <div className="freestyle-shell"><a className="skip" href="#freestyle-main">跳到创作区</a><header className="freestyle-top"><a href={storyHref} onClick={e=>navigateWorkspace(e,'/',{search:workSearch})} className="freestyle-brand"><span>Ⅱ</span><b>映序</b><small>快速创作</small></a><nav aria-label="快速创作导航"><a href={storyHref} onClick={e=>navigateWorkspace(e,'/',{search:workSearch})}><ArrowLeft size={15}/>短剧工作台</a><button onClick={connect}><Bot size={15}/>连接 AI</button><CloudButton onOpen={open}/><button disabled={c.busy} onClick={newQuick}><Plus size={15}/>新建快速创作</button></nav></header><CloudWorkspaceBar onOpenCloud={open}/><main id="freestyle-main" tabIndex={-1}>
    <AgentLink state={state} onOpenCloud={open} onLocate={locate} onActivity={showActivity}/>
    {cloudMode&&<div className="cloud-actions freestyle-view-switch" aria-label="快速创作视图"><button aria-pressed={panel==='create'} onClick={()=>setPanel('create')}><Film size={15}/>创作与结果</button><button aria-pressed={panel==='activity'} onClick={showActivity}><Activity size={15}/>创作动态</button></div>}
    {linkedEntity&&<aside className="cloud-warning" role="status">这项内容需要在短剧工作台中查看。当前项目和草稿会保留。<a href={workspaceHref('/',agentLinkSearch({projectId:project.id,entityId:linkedEntity}))} onClick={e=>navigateWorkspace(e,'/',{search:agentLinkSearch({projectId:project.id,entityId:linkedEntity})})}>前往对应内容</a><button onClick={()=>setLinkedEntity(null)}>收起</button></aside>}
    {panel==='activity'?<ProjectActivity key={`${c.account}:${project.id}:${state.workspaceEpoch}`} state={state} onLocate={locate} onReview={review} onOpenCloud={open} onConnect={connect}/>:<>
    {!cloudMode||!shot?<section className="freestyle-welcome">{c.account&&<ServiceStatus capabilities={c.capabilities}/>}<span className="eyebrow">ONE IDEA, ONE SHOT</span><h1>把一个想法，拍成一个镜头。</h1><p>写下画面、动作与声音。参考图片、动作视频和音频分别放好，再核对清晰度与时长。</p><div className="freestyle-start-actions"><button className="primary" disabled={c.busy} onClick={newQuick}><Film size={18}/>{c.account?'开始新的快速创作':'登录后开始创作'}</button><button onClick={open}>打开已有云项目</button></div><div className="freestyle-examples"><article><b>只有一句想法</b><p>选择文生视频，写清主体与动作。没有参考也能先预检。</p></article><article><b>让一张图片动起来</b><p>图片放首帧，描述接下来发生什么；尾帧按需添加。</p></article><article><b>人物配上动作与声音</b><p>选择全能参考，分别放角色图、动作片段和音频。</p></article></div><p className="muted">新建只保存项目，不提交模型请求。本机作品保留在短剧工作台。</p>{cloudMode&&!shot&&<p className="cloud-warning">当前项目还没有镜头。可回短剧工作台添加，或新建快速创作项目。</p>}{c.error&&<p className="cloud-error" role="alert">{c.error}</p>}</section>:<><div className="freestyle-heading"><div><span className="eyebrow">QUICK VIDEO</span><h1>{project.title}</h1><p>① 选择方式与素材　② 描述画面与参数　③ 预检并提交　④ 选择结果</p></div>{shots.length>1&&<label>当前镜头<select value={shot.id} onChange={e=>setShotId(e.target.value)}>{shots.map(s=><option key={s.id} value={s.id}>{ancestors(project,s.id).map(e=>e.title).join(' / ')} / {s.title}</option>)}</select></label>}</div>{project.journey?.workspace!=='freestyle'&&<p className="cloud-warning">正在单镜编辑短剧项目。章节结构、其他镜头与已采用版本都会保留。</p>}
    <div className="freestyle-grid"><section className="freestyle-reference-column" aria-label="分别放置参考素材"><H3Inputs project={project} shot={shot} recipes={recipes}/></section><aside className="freestyle-controls" aria-label="提示词与生成设置"><CloudShotPanel key={shot.id} project={project} shot={shot} showRecipeSelector={false}/></aside></div>
    <section className="freestyle-results" aria-label="生成结果与返修"><h2>选择结果，再决定怎么改</h2><p>结果不会自动替换你的选择。先看效果，再明确保留什么、只改什么。</p><CandidateReview project={project} shots={shots} currentShot={shot} onChooseShot={setShotId} onInspect={()=>document.querySelector('.freestyle-controls')?.scrollIntoView({behavior:'smooth'})} onReferences={()=>document.querySelector('.freestyle-reference-column')?.scrollIntoView({behavior:'smooth'})}/></section></>}
  </> }</main>{dialog&&<FreestyleDialog key={dialog.type} title={dialog.type==='agent'?'快速创作 · 连接 AI 助手':'快速创作 · 云项目与账户'} onClose={()=>setDialog(null)}>{dialog.type==='agent'?<AgentConnect account={c.account} workspace={state.workspace} projectId={project.id} projectTitle={project.title} entityId={shot?.id} entityTitle={shot?.title} capabilities={c.capabilities} busy={c.busy} view="/freestyle" onOpenCloud={open} onSaveCloud={()=>cloud.save()} onManageKeys={grant=>setDialog({type:'cloud',grant})} onOpenActivity={showActivity}/>:<CloudProjects quick initialGrant={dialog.grant} onClose={()=>setDialog(null)}/>}</FreestyleDialog>}<div className={'toast '+(state.notice?'show':'')} role="status" aria-live="polite">{state.notice}</div></div>;
}
