import {useI18n,LanguageSwitcher} from './i18n/I18nProvider.jsx';
import './i18n/workspace-language.css';
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
  const {t,locale}=useI18n();
  const ref=useRef(),opener=useRef(document.activeElement);
  useEffect(()=>{const element=ref.current;element.showModal();element.querySelector('input:not([type=file]),textarea,button')?.focus();return()=>{element.close();opener.current?.isConnected&&opener.current.focus();};},[]);
  return <dialog className="modal wide" ref={ref} aria-label={title} onCancel={e=>{e.preventDefault();onClose();}}><div className="modal-heading"><h2>{title}</h2><button className="icon-button" aria-label={t('workspace.copy88')} onClick={onClose}><X size={18}/></button></div>{children}</dialog>;
}

export default function Freestyle(){
  const {t,locale}=useI18n();
  const state=useSyncExternalStore(store.subscribe,store.getState,store.getState),c=useCloud(),[dialog,setDialog]=useState(null),[shotId,setShotId]=useState(''),[panel,setPanel]=useState('create'),[linkedEntity,setLinkedEntity]=useState(null),[search,setSearch]=useState(()=>window.location.search),project=state.project,cloudMode=state.workspace.mode==='cloud',shots=cloudMode?project.entities.filter(e=>e.type==='shot'):[],shot=shots.find(e=>e.id===shotId)||shots[0],recipes=c.capabilities?.recipes||[],recipe=selectedRecipe(recipes,shot?.data.h3),mode=recipe?.mode,clipLimits=recipe?.limits?`${recipe.limits.min_clip_duration}–${recipe.limits.max_clip_duration} 秒`: '以服务声明为准';
  const open=()=>setDialog({type:'cloud'}),connect=()=>setDialog({type:'agent'}),showActivity=()=>{setPanel('activity');setDialog(null);};
  const newQuick=()=>{if(!c.account){open();return;}run(cloud.createQuick());};
  const locate=id=>{const target=freestyleEntityTarget(store.getState().project,id);if(!target)return false;if(target.shotId){setShotId(target.shotId);setPanel('create');setLinkedEntity(null);requestAnimationFrame(()=>document.querySelector('.freestyle-controls')?.scrollIntoView({behavior:'smooth'}));}else setLinkedEntity(target.entityId);return true;};
  const review=id=>{if(locate(id))requestAnimationFrame(()=>document.querySelector('.freestyle-results')?.scrollIntoView({behavior:'smooth'}));};
  const workSearch=cloudMode?agentLinkSearch({projectId:project.id,entityId:shot?.id,activity:panel==='activity'}):search,storyHref=workspaceHref('/',workSearch);
  useEffect(()=>{const update=()=>setSearch(window.location.search);window.addEventListener('popstate',update);return()=>window.removeEventListener('popstate',update);},[]);
  useEffect(()=>{const link=freestyleLinkState(store.getState(),search,c.account);if(!link)return;if(link.target?.shotId)setShotId(link.target.shotId);if(link.target&&!link.target.shotId)setLinkedEntity(link.target.entityId);setPanel(link.activity?'activity':'create');},[search,c.account,project.id,state.workspaceEpoch]);
  useEffect(()=>{document.title=t('workspace.copy116');},[locale]);
  useEffect(()=>{if(!state.notice)return;const timer=setTimeout(()=>store.notify(''),5500);return()=>clearTimeout(timer);},[state.notice]);
  return <div className="freestyle-shell"><a className="skip" href="#freestyle-main">{t('workspace.copy91')}</a><header className="freestyle-top"><a href={storyHref} onClick={e=>navigateWorkspace(e,'/',{search:workSearch})} className="freestyle-brand"><span>Ⅱ</span><b>{t('workspace.copy8')}</b><small>{t('workspace.copy92')}</small></a><LanguageSwitcher/><a className="account-settings-link" href="/settings">{t('settings.title')}</a><nav aria-label={t('workspace.copy93')}><a href={storyHref} onClick={e=>navigateWorkspace(e,'/',{search:workSearch})}><ArrowLeft size={15}/>{t('workspace.copy94')}</a><button onClick={connect}><Bot size={15}/>{t('workspace.copy95')}</button><CloudButton onOpen={open}/><button disabled={c.busy} onClick={newQuick}><Plus size={15}/>{t('workspace.copy96')}</button></nav></header><CloudWorkspaceBar onOpenCloud={open}/><main id="freestyle-main" tabIndex={-1}>
    <AgentLink state={state} onOpenCloud={open} onLocate={locate} onActivity={showActivity}/>
    {cloudMode&&<div className="cloud-actions freestyle-view-switch" aria-label={t('workspace.copy97')}><button aria-pressed={panel==='create'} onClick={()=>setPanel('create')}><Film size={15}/>{t('workspace.copy98')}</button><button aria-pressed={panel==='activity'} onClick={showActivity}><Activity size={15}/>{t('workspace.copy99')}</button></div>}
    {linkedEntity&&<aside className="cloud-warning" role="status">{t('workspace.copy129')}<a href={workspaceHref('/',agentLinkSearch({projectId:project.id,entityId:linkedEntity}))} onClick={e=>navigateWorkspace(e,'/',{search:agentLinkSearch({projectId:project.id,entityId:linkedEntity})})}>{t('workspace.copy100')}</a><button onClick={()=>setLinkedEntity(null)}>{t('workspace.copy101')}</button></aside>}
    {panel==='activity'?<ProjectActivity key={`${c.account}:${project.id}:${state.workspaceEpoch}`} state={state} onLocate={locate} onReview={review} onOpenCloud={open} onConnect={connect}/>:<>
    {!cloudMode||!shot?<section className="freestyle-welcome">{c.account&&<ServiceStatus capabilities={c.capabilities}/>}<span className="eyebrow">ONE IDEA, ONE SHOT</span><h1>{t('workspace.copy102')}</h1><p>{t('workspace.copy121')}</p><div className="freestyle-start-actions"><button className="primary" disabled={c.busy} onClick={newQuick}><Film size={18}/>{c.account?t('workspace.copy103'):t('workspace.copy104')}</button><button onClick={open}>{t('workspace.copy105')}</button></div><div className="freestyle-examples"><article><b>{t('workspace.copy106')}</b><p>{t('workspace.copy122')}</p></article><article><b>{t('workspace.copy107')}</b><p>{t('workspace.copy123')}</p></article><article><b>{t('workspace.copy108')}</b><p>{t('workspace.copy124')}</p></article></div><p className="muted">{t('workspace.copy125')}</p>{cloudMode&&!shot&&<p className="cloud-warning">{t('workspace.copy126')}</p>}{c.error&&<p className="cloud-error" role="alert">{c.error}</p>}</section>:<><div className="freestyle-heading"><div><span className="eyebrow">QUICK VIDEO</span><h1>{project.title}</h1><p>{t('workspace.copy130')}</p></div>{shots.length>1&&<label>{t('workspace.copy109')}<select value={shot.id} onChange={e=>setShotId(e.target.value)}>{shots.map(s=><option key={s.id} value={s.id}>{ancestors(project,s.id).map(e=>e.title).join(' / ')} / {s.title}</option>)}</select></label>}</div>{project.journey?.workspace!=='freestyle'&&<p className="cloud-warning">{t('workspace.copy127')}</p>}
    <div className="freestyle-grid"><section className="freestyle-reference-column" aria-label={t('workspace.copy110')}><H3Inputs project={project} shot={shot} recipes={recipes}/></section><aside className="freestyle-controls" aria-label={t('workspace.copy111')}><CloudShotPanel key={shot.id} project={project} shot={shot} showRecipeSelector={false}/></aside></div>
    <section className="freestyle-results" aria-label={t('workspace.copy112')}><h2>{t('workspace.copy113')}</h2><p>{t('workspace.copy128')}</p><CandidateReview project={project} shots={shots} currentShot={shot} onChooseShot={setShotId} onInspect={()=>document.querySelector('.freestyle-controls')?.scrollIntoView({behavior:'smooth'})} onReferences={()=>document.querySelector('.freestyle-reference-column')?.scrollIntoView({behavior:'smooth'})}/></section></>}
  </> }</main>{dialog&&<FreestyleDialog key={dialog.type} title={dialog.type==='agent'?t('workspace.copy114'):t('workspace.copy115')} onClose={()=>setDialog(null)}>{dialog.type==='agent'?<AgentConnect account={c.account} workspace={state.workspace} projectId={project.id} projectTitle={project.title} entityId={shot?.id} entityTitle={shot?.title} capabilities={c.capabilities} busy={c.busy} view="/freestyle" onOpenCloud={open} onSaveCloud={()=>cloud.save()} onManageKeys={grant=>setDialog({type:'cloud',grant})} onOpenActivity={showActivity}/>:<CloudProjects quick initialGrant={dialog.grant} onClose={()=>setDialog(null)}/>}</FreestyleDialog>}<div className={'toast '+(state.notice?'show':'')} role="status" aria-live="polite">{state.notice}</div></div>;
}
