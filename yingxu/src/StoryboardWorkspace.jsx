import {useI18n} from './i18n/I18nProvider.jsx';
import React,{useEffect,useRef,useState} from 'react';
import {DndContext,DragOverlay,PointerSensor,KeyboardSensor,closestCenter,pointerWithin,useSensor,useSensors} from '@dnd-kit/core';
import {SortableContext,rectSortingStrategy,sortableKeyboardCoordinates} from '@dnd-kit/sortable';
import {StoryboardUIProvider,useStoryboardUI} from './storyboard-ui-context.jsx';
import {AssetLibrary,AssetThumb,AddCard,ProfileEditor,ShotCard,ShotEditor,Screen,Timeline,Transport,usePlayer} from './StoryboardComponents.jsx';
import {StoryboardSplitter,useStoryboardPanelSize} from './StoryboardPanels.jsx';
import {coverOf,fmt,planDrop,totalDuration,variantOf} from './storyboard-ui-model.js';
import './storyboard-workspace.css';
import './i18n/storyboard-language.css';

function collision(args){
  const id=String(args.active.id);
  if(id.startsWith('asset:')||id.startsWith('profile:')){
    const hits=pointerWithin(args).filter(hit=>!(id.startsWith('profile:')&&/^(pdrop|vdrop|voicedrop):/.test(String(hit.id))));
    const best=hits.find(hit=>/^(slot|vdrop|voicedrop):/.test(String(hit.id)));return best?[best]:hits;
  }
  return closestCenter({...args,droppableContainers:args.droppableContainers.filter(container=>String(container.id).startsWith('shot:'))});
}
function QueueSummary(){const {t,locale}=useI18n();
  const {state}=useStoryboardUI(),queues=state.queue;
  const rows=['FL','REF'].map(mode=>{
    const row=Array.isArray(queues)?queues.find(item=>String(item.mode).toUpperCase()===mode):queues?.[mode];
    const versions=state.shots.filter(shot=>shot.mode===mode).flatMap(shot=>shot.versions);
    const waiting=versions.filter(version=>['queued','waiting_capacity','claimed','submitting','submission_unknown'].includes(version.status)).length;
    const running=versions.filter(version=>['generating','running','collecting','checking','cancel_requested'].includes(version.status)).length;
    const observed=row?.waiting??row?.waiting_jobs??row?.queued;
    return {mode,row,waiting:Math.max(Number.isFinite(observed)?observed:0,waiting),running};
  }).filter(row=>row.waiting>0||row.running>0);
  if(!rows.length)return null;
  return <div className="sb-queue" aria-label={t('storyboard.copy14')}>{rows.map(({mode,row,waiting,running})=><span key={mode}>{mode==='FL'?t('storyboard.copy15'):t('storyboard.copy16')}：{t('storyboard.copy34',{count:waiting})}{running>0&&t('storyboard.copy32',{count:running})}{waiting>0&&Number.isFinite(row?.roughWaitSeconds)&&row.roughWaitSeconds>0&&t('storyboard.copy33',{minutes:Math.ceil(row.roughWaitSeconds/60)})}</span>)}</div>;
}
function StoryboardLayoutA({selected,setSelected,pickAsset}){const {t,locale}=useI18n();
  const {state,dispatch}=useStoryboardUI(),player=usePlayer(state.shots),[libraryCollapsed,setLibraryCollapsed]=useState(()=>globalThis.matchMedia?.('(max-width:760px)').matches??false);
  useEffect(()=>{const media=globalThis.matchMedia?.('(max-width:760px)');if(!media)return;const change=()=>setLibraryCollapsed(media.matches);media.addEventListener('change',change);return()=>media.removeEventListener('change',change);},[]);
  useEffect(()=>{if(libraryCollapsed)return;const close=event=>{if(event.key==='Escape')setLibraryCollapsed(true);};window.addEventListener('keydown',close);return()=>window.removeEventListener('keydown',close);},[libraryCollapsed]);
  const library=useStoryboardPanelSize('library',280,140,560),dock=useStoryboardPanelSize('dock',190,110,460),drawer=useStoryboardPanelSize('drawer',440,340,820);
  const count=state.shots.filter(shot=>shot.chosen).length,pending=state.shots.filter(shot=>!shot.versions.length);
  return <div className={'layout A'+(libraryCollapsed?' library-collapsed':'')} data-testid="storyboard-layout-a" style={{gridTemplateColumns:libraryCollapsed?'44px minmax(0,1fr)':`${library.size}px 8px minmax(0,1fr)`,'--libw':(libraryCollapsed?44:library.size)+'px'}}>
    {!libraryCollapsed&&<button type="button" className="sb-library-backdrop" aria-label={t('storyboard.detail15')} onClick={()=>setLibraryCollapsed(true)}/>}
    <AssetLibrary onPick={pickAsset} selectedShot={state.shots.find(shot=>shot.id===selected)} collapsed={libraryCollapsed} onToggle={()=>setLibraryCollapsed(value=>!value)}/>
    {!libraryCollapsed&&<StoryboardSplitter panel={library} label={t('storyboard.copy17')}/>}
    <div className="col"><main className="main"><div className="chapter-head"><div className="chapter-summary"><small>{t('storyboard.copy36',{shots:state.shots.length,duration:fmt(totalDuration(state.shots)),chosen:count,aspect:state.project.aspect})}</small><label className="style-field"><span>{t('storyboard.copy18')}</span><input aria-label={t('storyboard.copy18')} value={state.project.style||''} placeholder={t('storyboard.copy19')} onChange={event=>dispatch({type:'project',patch:{style:event.target.value}})}/></label></div><div className="chapter-actions"><QueueSummary/><button className="primary" disabled={state.busy||!pending.length} onClick={()=>dispatch({type:'generate',ids:pending.map(shot=>shot.id)})}>{t('storyboard.copy35',{count:pending.length})}</button></div></div>
      <SortableContext items={state.shots.map(shot=>'shot:'+shot.id)} strategy={rectSortingStrategy}><div className="wall">{state.shots.map((shot,index)=><ShotCard key={shot.id} shot={shot} index={index} selected={selected===shot.id} onOpen={setSelected}/>)}<AddCard onAdd={()=>dispatch({type:'add'})}/></div></SortableContext>
    </main><StoryboardSplitter panel={dock} axis="y" sign={-1} label={t('storyboard.copy20')}/><div className="dock" style={{height:dock.size}}><Screen player={player}/><div className="dock-right"><Transport player={player}/><Timeline player={player} selectedId={selected} onPick={setSelected}/><small className="muted">{t('storyboard.copy21')}</small></div></div></div>
    {selected&&<aside className="drawer" aria-label={t('storyboard.copy22')} style={{width:drawer.size}}><StoryboardSplitter panel={drawer} sign={-1} className="edge" label={t('storyboard.copy23')}/><ShotEditor shotId={selected} onClose={()=>setSelected(null)}/></aside>}
    <ProfileEditor/>
  </div>;
}
function StoryboardShell({onLegacyGuide,onCanvas}){const {t,locale}=useI18n();
  const {state,dispatch,db,clearError}=useStoryboardUI(),[selected,setSelected]=useState(null),[dragging,setDragging]=useState(null),[toast,setToast]=useState('');
  const container=useRef(),sensors=useSensors(useSensor(PointerSensor,{activationConstraint:{distance:6}}),useSensor(KeyboardSensor,{coordinateGetter:sortableKeyboardCoordinates}));
  useEffect(()=>{if(state.lastAdded)setSelected(state.lastAdded);},[state.lastAdded]);
  useEffect(()=>{if(selected&&!state.shots.some(shot=>shot.id===selected))setSelected(null);},[state.shots,selected]);
  useEffect(()=>{if(!toast)return;const timer=setTimeout(()=>setToast(''),4000);return()=>clearTimeout(timer);},[toast]);
  const dropOnShot=async(item,shotId,slot='smart')=>{
    const shot=state.shots.find(s=>s.id===shotId),object=item.profile?{profile:db.profile(item.profile)}:{asset:db.asset(item.asset)};
    if(!shot||!(object.profile||object.asset))return;const plan=planDrop(shot,object,slot,db);if(!plan.patch){setToast(plan.msg);return;}
    const receipt=await dispatch({type:'drop',...item,shot:shotId,slot});if(receipt?.ok!==false)setToast(plan.msg||t('storyboard.copy24'));
  };
  const pick=item=>{if(!selected){if(item.profile)return dispatch({type:'openProfile',id:item.profile});setToast(t('storyboard.copy25'));return;}return dropOnShot(item,selected);};
  const onDragStart=event=>{const id=String(event.active.id);setDragging(id.startsWith('asset:')?{asset:id.slice(6)}:id.startsWith('profile:')?{profile:id.slice(8)}:null);};
  const onDragEnd=async({active,over})=>{
    setDragging(null);if(!over)return;const a=String(active.id),o=String(over.id);
    if(a.startsWith('shot:')&&o.startsWith('shot:'))return dispatch({type:'reorder',from:a.slice(5),to:o.slice(5)});
    const item=a.startsWith('asset:')?{asset:a.slice(6)}:a.startsWith('profile:')?{profile:a.slice(8)}:null;if(!item)return;
    if(o.startsWith('shot:'))return dropOnShot(item,o.slice(5));
    if(o.startsWith('slot:')){const [,slot,shot]=o.split(':');return dropOnShot(item,shot,slot==='cast'?'smart':slot);}
    if(item.asset&&/^(pdrop|vdrop|voicedrop):/.test(o)){
      const asset=db.asset(item.asset),[kind,pid,vid]=o.split(':'),profile=db.profile(pid);if(!asset||!profile)return;
      if(asset.kind==='audio'){
        if(profile.type==='character')return dispatch({type:'addVoice',pid,asset:asset.id});
        if(profile.type==='location')return dispatch({type:'profile',id:pid,patch:{ambient:asset.id}});
        setToast(t('storyboard.copy26'));return;
      }
      if(asset.kind!=='image'){setToast(t('storyboard.copy27'));return;}
      if(kind==='voicedrop'){setToast(t('storyboard.copy28'));return;}
      const variant=variantOf(profile,vid||profile.def);if(!variant){setToast(t('storyboard.copy29'));return;}return dispatch({type:'fileImage',asset:item.asset,pid,vid:variant.id});
    }
  };
  const dragged=dragging&&(dragging.asset?db.asset(dragging.asset):db.profile(dragging.profile)),thumb=dragging?.profile?coverOf(db,dragged,dragged?.def):dragged;
  return <DndContext sensors={sensors} collisionDetection={collision} onDragStart={onDragStart} onDragEnd={onDragEnd} onDragCancel={()=>setDragging(null)}><section ref={container} className="sb-workspace" aria-label={t('storyboard.copy30')}>
    {state.error&&<div className="sb-error" role="alert"><span>{state.error}</span><button className="x" aria-label={t('storyboard.copy31')} onClick={clearError}>×</button></div>}
    <StoryboardLayoutA selected={selected} setSelected={setSelected} pickAsset={pick}/>
    <DragOverlay dropAnimation={null}>{dragged?<div className="sb-portal achip overlay"><AssetThumb asset={thumb}/><b>{dragged.name}</b></div>:null}</DragOverlay>
    {toast&&<div className="sb-toast" role="status" aria-live="polite">{toast}</div>}
  </section></DndContext>;
}
export function StoryboardWorkspace({adapter,onLegacyGuide,onCanvas}){const {t,locale}=useI18n();return <StoryboardUIProvider adapter={adapter}><StoryboardShell onLegacyGuide={onLegacyGuide} onCanvas={onCanvas}/></StoryboardUIProvider>;}
export default StoryboardWorkspace;
