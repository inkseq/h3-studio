import React,{useEffect,useRef,useState} from 'react';
import {DndContext,DragOverlay,PointerSensor,KeyboardSensor,closestCenter,pointerWithin,useSensor,useSensors} from '@dnd-kit/core';
import {SortableContext,rectSortingStrategy,sortableKeyboardCoordinates} from '@dnd-kit/sortable';
import {StoryboardUIProvider,useStoryboardUI} from './storyboard-ui-context.jsx';
import {AssetLibrary,AssetThumb,AddCard,ProfileEditor,ShotCard,ShotEditor,Screen,Timeline,Transport,usePlayer} from './StoryboardComponents.jsx';
import {StoryboardSplitter,useStoryboardPanelSize} from './StoryboardPanels.jsx';
import {coverOf,fmt,planDrop,totalDuration,variantOf} from './storyboard-ui-model.js';
import './storyboard-workspace.css';

function collision(args){
  const id=String(args.active.id);
  if(id.startsWith('asset:')||id.startsWith('profile:')){
    const hits=pointerWithin(args).filter(hit=>!(id.startsWith('profile:')&&/^(pdrop|vdrop|voicedrop):/.test(String(hit.id))));
    const best=hits.find(hit=>/^(slot|vdrop|voicedrop):/.test(String(hit.id)));return best?[best]:hits;
  }
  return closestCenter({...args,droppableContainers:args.droppableContainers.filter(container=>String(container.id).startsWith('shot:'))});
}
function QueueSummary(){
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
  return <div className="sb-queue" aria-label="当前生成队列">{rows.map(({mode,row,waiting,running})=><span key={mode}>{mode==='FL'?'首尾帧':'全能参考'}：{waiting} 等待{running>0&&` · ${running} 处理中`}{waiting>0&&Number.isFinite(row?.roughWaitSeconds)&&row.roughWaitSeconds>0&&` · 约等 ${Math.ceil(row.roughWaitSeconds/60)} 分钟`}</span>)}</div>;
}
function StoryboardLayoutA({selected,setSelected,pickAsset}){
  const {state,dispatch}=useStoryboardUI(),player=usePlayer(state.shots),[libraryCollapsed,setLibraryCollapsed]=useState(false);
  const library=useStoryboardPanelSize('library',280,140,560),dock=useStoryboardPanelSize('dock',190,110,460),drawer=useStoryboardPanelSize('drawer',440,340,820);
  const count=state.shots.filter(shot=>shot.chosen).length,pending=state.shots.filter(shot=>!shot.versions.length);
  return <div className={'layout A'+(libraryCollapsed?' library-collapsed':'')} data-testid="storyboard-layout-a" style={{gridTemplateColumns:libraryCollapsed?'44px minmax(0,1fr)':`${library.size}px 8px minmax(0,1fr)`,'--libw':(libraryCollapsed?44:library.size)+'px'}}>
    <AssetLibrary onPick={pickAsset} selectedShot={state.shots.find(shot=>shot.id===selected)} collapsed={libraryCollapsed} onToggle={()=>setLibraryCollapsed(value=>!value)}/>
    {!libraryCollapsed&&<StoryboardSplitter panel={library} label="素材库宽度"/>}
    <div className="col"><main className="main"><div className="chapter-head"><div className="chapter-summary"><small>{state.shots.length} 个镜头 · 总长 {fmt(totalDuration(state.shots))} · 已选定 {count}/{state.shots.length} · {state.project.aspect}</small><label className="style-field"><span>全片风格</span><input aria-label="全片风格" value={state.project.style||''} placeholder="例如：电影感，冷色调，胶片颗粒" onChange={event=>dispatch({type:'project',patch:{style:event.target.value}})}/></label></div><div className="chapter-actions"><QueueSummary/><button className="primary" disabled={state.busy||!pending.length} onClick={()=>dispatch({type:'generate',ids:pending.map(shot=>shot.id)})}>预检全部未生成 · {pending.length}</button></div></div>
      <SortableContext items={state.shots.map(shot=>'shot:'+shot.id)} strategy={rectSortingStrategy}><div className="wall">{state.shots.map((shot,index)=><ShotCard key={shot.id} shot={shot} index={index} selected={selected===shot.id} onOpen={setSelected}/>)}<AddCard onAdd={()=>dispatch({type:'add'})}/></div></SortableContext>
    </main><StoryboardSplitter panel={dock} axis="y" sign={-1} label="播放器和时间线高度"/><div className="dock" style={{height:dock.size}}><Screen player={player}/><div className="dock-right"><Transport player={player}/><Timeline player={player} selectedId={selected} onPick={setSelected}/><small className="muted">点击一段时间线，编辑对应镜头</small></div></div></div>
    {selected&&<aside className="drawer" aria-label="镜头编辑器" style={{width:drawer.size}}><StoryboardSplitter panel={drawer} sign={-1} className="edge" label="镜头编辑器宽度"/><ShotEditor shotId={selected} onClose={()=>setSelected(null)}/></aside>}
    <ProfileEditor/>
  </div>;
}
function StoryboardShell({onLegacyGuide,onCanvas}){
  const {state,dispatch,db,clearError}=useStoryboardUI(),[selected,setSelected]=useState(null),[dragging,setDragging]=useState(null),[toast,setToast]=useState('');
  const container=useRef(),sensors=useSensors(useSensor(PointerSensor,{activationConstraint:{distance:6}}),useSensor(KeyboardSensor,{coordinateGetter:sortableKeyboardCoordinates}));
  useEffect(()=>{if(state.lastAdded)setSelected(state.lastAdded);},[state.lastAdded]);
  useEffect(()=>{if(selected&&!state.shots.some(shot=>shot.id===selected))setSelected(null);},[state.shots,selected]);
  useEffect(()=>{if(!toast)return;const timer=setTimeout(()=>setToast(''),4000);return()=>clearTimeout(timer);},[toast]);
  const dropOnShot=async(item,shotId,slot='smart')=>{
    const shot=state.shots.find(s=>s.id===shotId),object=item.profile?{profile:db.profile(item.profile)}:{asset:db.asset(item.asset)};
    if(!shot||!(object.profile||object.asset))return;const plan=planDrop(shot,object,slot,db);if(!plan.patch){setToast(plan.msg);return;}
    const receipt=await dispatch({type:'drop',...item,shot:shotId,slot});if(receipt?.ok!==false)setToast(plan.msg||'已加入镜头');
  };
  const pick=item=>{if(!selected){if(item.profile)return dispatch({type:'openProfile',id:item.profile});setToast('先选中镜头，或把素材直接拖到镜头上');return;}return dropOnShot(item,selected);};
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
        setToast('道具档案不能放声音');return;
      }
      if(asset.kind!=='image'){setToast('动作视频请放进镜头的全能参考素材');return;}
      if(kind==='voicedrop'){setToast('这里接收声音，照片请放到造型照片区');return;}
      const variant=variantOf(profile,vid||profile.def);if(!variant){setToast('先建立造型或场景版本，再添加照片');return;}return dispatch({type:'fileImage',asset:item.asset,pid,vid:variant.id});
    }
  };
  const dragged=dragging&&(dragging.asset?db.asset(dragging.asset):db.profile(dragging.profile)),thumb=dragging?.profile?coverOf(db,dragged,dragged?.def):dragged;
  return <DndContext sensors={sensors} collisionDetection={collision} onDragStart={onDragStart} onDragEnd={onDragEnd} onDragCancel={()=>setDragging(null)}><section ref={container} className="sb-workspace" aria-label="故事分镜工作台">
    {state.error&&<div className="sb-error" role="alert"><span>{state.error}</span><button className="x" aria-label="关闭操作提示" onClick={clearError}>×</button></div>}
    <StoryboardLayoutA selected={selected} setSelected={setSelected} pickAsset={pick}/>
    <DragOverlay dropAnimation={null}>{dragged?<div className="sb-portal achip overlay"><AssetThumb asset={thumb}/><b>{dragged.name}</b></div>:null}</DragOverlay>
    {toast&&<div className="sb-toast" role="status" aria-live="polite">{toast}</div>}
  </section></DndContext>;
}
export function StoryboardWorkspace({adapter,onLegacyGuide,onCanvas}){return <StoryboardUIProvider adapter={adapter}><StoryboardShell onLegacyGuide={onLegacyGuide} onCanvas={onCanvas}/></StoryboardUIProvider>;}
export default StoryboardWorkspace;
