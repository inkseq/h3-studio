import {WorkbenchTakes,WorkbenchCopies,WorkbenchDefaults} from './StoryboardWorkbench.jsx';
import {AssistantBadge} from './AssistantConnections.jsx';
import {useI18n} from './i18n/I18nProvider.jsx';
import React, {useEffect, useMemo, useRef, useState} from 'react';
import {createPortal} from 'react-dom';
import {useDraggable, useDroppable} from '@dnd-kit/core';
import {useSortable} from '@dnd-kit/sortable';
import {CSS} from '@dnd-kit/utilities';
import {KIND_LABEL, TYPE_LABEL, VARIANT_LABEL} from './storyboard-ui-model.js';
import {StoryboardDownloads,StoryboardAssetImage,StoryboardVideo,StoryboardPlayButton} from './StoryboardMedia.jsx';
import {chosenVersion,coverOf,expand,fmt,MODE_LABEL,modeOf,QUALITY_LABEL,planDrop,segments,shotProfiles,shotStatus,startFrame,totalDuration,variantOf,queuePositions} from './storyboard-ui-model.js';
import {useStoryboardUI as useStore} from './storyboard-ui-context.jsx';
import {StoryboardNameDialog,StoryboardDeleteDialog} from './StoryboardDialog.jsx';
import {createStoryboardPlayback} from './storyboard-playback.js';


function displayLabels(tr){return {TYPE_LABEL:{character:tr('storyboard.labels.TYPE_LABEL.character'),location:tr('storyboard.labels.TYPE_LABEL.location'),prop:tr('storyboard.labels.TYPE_LABEL.prop')},VARIANT_LABEL:{character:tr('storyboard.labels.VARIANT_LABEL.character'),location:tr('storyboard.labels.VARIANT_LABEL.location'),prop:tr('storyboard.labels.VARIANT_LABEL.prop')},KIND_LABEL:{image:tr('storyboard.labels.KIND_LABEL.image'),video:tr('storyboard.labels.KIND_LABEL.video'),audio:tr('storyboard.labels.KIND_LABEL.audio')},MODE_LABEL:{FL:tr('storyboard.labels.MODE_LABEL.FL'),REF:tr('storyboard.labels.MODE_LABEL.REF')},QUALITY_LABEL:{draft:tr('storyboard.labels.QUALITY_LABEL.draft'),final:tr('storyboard.labels.QUALITY_LABEL.final')}};}
/* ---------- thumbs & draggable tiles ---------- */

export function AssetThumb({asset,size=36}) {const {t:tr,locale}=useI18n();const {TYPE_LABEL,VARIANT_LABEL,KIND_LABEL,MODE_LABEL,QUALITY_LABEL}=displayLabels(tr);
  if(!asset)return <span className="athumb empty" style={{width:size,height:size}}/>;
  if(asset.kind==='audio')return <span className="athumb audio" style={{width:size,height:size}}>♪</span>;
  return <StoryboardAssetImage asset={asset} className="athumb" style={{width:size,height:size}}/>;
}

function Tile({asset, used, onClick, onZoom, dragId, children}) {const {t:tr,locale}=useI18n();const {TYPE_LABEL,VARIANT_LABEL,KIND_LABEL,MODE_LABEL,QUALITY_LABEL}=displayLabels(tr);
  const {attributes, listeners, setNodeRef, isDragging} = useDraggable({id: dragId || 'asset:' + asset.id});
  return (
    <div ref={setNodeRef} {...listeners} {...attributes} className={'tile' + (isDragging ? ' dragging' : '') + (used ? ' used' : '')}
      onClick={onClick} title={tr('storyboard.mixed1',{p0:asset.name})}>
      {asset.kind === 'audio' ? <div className="tile-audio">♪<small>{asset.seconds}s</small></div>
        : <StoryboardAssetImage asset={asset}/>}
      {asset.kind === 'video' && <span className="tile-badge">▶ {asset.seconds}s</span>}
      {asset.kind === 'audio' && <span className="tile-play"><PlayButton asset={asset} /></span>}
      {asset.source === 'ai' && <span className="tile-badge ai">AI</span>}
      {asset.source === 'frame' && <span className="tile-badge">{tr('storyboard.copy37')}</span>}
      {used && <span className="tile-used">✓</span>}
      <span className="tile-name">{asset.name}</span>
      {onZoom && asset.kind !== 'audio' && <button className="tile-zoom" onPointerDown={e => e.stopPropagation()} onClick={e => { e.stopPropagation(); onZoom(asset); }}>⤢</button>}
      {children}
    </div>
  );
}

function ProfileCard({p, used, onOpen, onAdd}) {const {t:tr,locale}=useI18n();const {TYPE_LABEL,VARIANT_LABEL,KIND_LABEL,MODE_LABEL,QUALITY_LABEL}=displayLabels(tr);
  const {db} = useStore();
  const drag = useDraggable({id: 'profile:' + p.id});
  const drop = useDroppable({id: 'pdrop:' + p.id});
  const cover = coverOf(db, p, p.def);
  const count = p.variants.reduce((n, v) => n + v.images.length, 0);
  const dragged = drop.isOver && String(drop.active?.id).startsWith('asset:') ? db.asset(String(drop.active.id).slice(6)) : null;
  const fileOver = !!dragged;
  const hint = !dragged ? '' : dragged.kind === 'image' ? tr('storyboard.mixed2',{p0:p.name})
    : dragged.kind === 'audio' ? (p.type === 'character' ? tr('storyboard.mixed3',{p0:p.name}) : p.type === 'location' ? tr('storyboard.detail1') : tr('storyboard.detail2'))
    : tr('storyboard.detail3');
  return (
    <div ref={n => { drag.setNodeRef(n); drop.setNodeRef(n); }} {...drag.listeners} {...drag.attributes}
      className={'pcard ' + p.type + (drag.isDragging ? ' dragging' : '') + (used ? ' used' : '') + (fileOver ? ' drop-hot' : '')}
      onClick={onOpen} title={tr('storyboard.detail82')}>
      {cover?<AssetThumb asset={cover} size={38}/>:<span className="profile-initial" aria-hidden="true">{Array.from((p.name||tr('storyboard.detail4')).trim())[0]?.toLocaleUpperCase()||tr('storyboard.detail5')}</span>}
      <div className="pcard-body"><b title={p.name}>{p.name}</b>
        <small>{p.type === 'prop' ? tr('storyboard.mixed4',{p0:count}) : tr('storyboard.mixed5',{p0:p.variants.length,p1:VARIANT_LABEL[p.type],p2:count})}{p.voices?.length ? tr('storyboard.mixed6',{p0:p.voices.length}) : ''}{p.ambient ? tr('storyboard.detail6') : ''}</small>
        <small className="muted clip">{p.desc}</small></div>
      {onAdd && <button className="pcard-open" aria-label={used?tr('storyboard.mixed7',{p0:p.name}):tr('storyboard.mixed8',{p0:p.name})} title={used?tr('storyboard.detail7'):tr('storyboard.detail8')} onPointerDown={e => e.stopPropagation()} onClick={e => { e.stopPropagation(); onAdd(); }}>{used ? '✓' : '＋'}</button>}
      {fileOver && <div className={'drop-hint' + (dragged.kind === 'video' || (dragged.kind === 'audio' && p.type === 'prop') ? ' warn' : '')}>{hint}</div>}
    </div>
  );
}

/* ---------- audio playback ---------- */

const PlayButton=StoryboardPlayButton;
// Preserve actual File objects for the normal /v1/assets upload adapter.
export async function readFiles(fileList){return [...fileList];}
const nativeDrop = onFiles => ({
  onDragOver: e => { if ([...e.dataTransfer.types].includes('Files')) { e.preventDefault(); e.currentTarget.classList.add('drop-hot'); } },
  onDragLeave: e => e.currentTarget.classList.remove('drop-hot'),
  onDrop: async e => { if (!e.dataTransfer.files.length) return; e.preventDefault(); e.currentTarget.classList.remove('drop-hot'); onFiles(await readFiles(e.dataTransfer.files)); },
});

// Choose files already in the library, or bring new ones from the computer.
function ChooseFiles({kind, exclude, title, onPick, onFiles, onClose}) {const {t:tr,locale}=useI18n();const {TYPE_LABEL,VARIANT_LABEL,KIND_LABEL,MODE_LABEL,QUALITY_LABEL}=displayLabels(tr);
  const {state} = useStore();
  const [q, setQ] = useState('');
  const input = useRef();
  const list = state.assets.filter(a => a.kind === kind && !exclude.has(a.id) && (!q || a.name.includes(q)));
  return createPortal(
    <div className="sb-portal choose-overlay" onClick={onClose} onKeyDown={e => e.key === 'Escape' && onClose()}>
      <div className="choose" onClick={e => e.stopPropagation()} {...nativeDrop(files => { onFiles(files); onClose(); })}>
        <div className="picker-head"><b>{title}</b><button className="x" onClick={onClose}>×</button></div>
        <div className="row tight">
          <input className="lib-search" autoFocus placeholder={tr('storyboard.finish1')} value={q} onChange={e => setQ(e.target.value)} />
          <button className="primary" onClick={() => input.current.click()}>{tr('storyboard.copy38')}</button>
          <input ref={input} type="file" multiple hidden accept={kind === 'image' ? 'image/*' : 'audio/*'}
            onChange={async e => { onFiles(await readFiles(e.target.files)); onClose(); }} />
        </div>
        <div className="picker-note">{tr('storyboard.copy39')}</div>
        {kind === 'image' ? <div className="picker-grid">{list.map(a => (
          <button key={a.id} className="tile" onClick={() => onPick(a.id)} title={a.name}><StoryboardAssetImage asset={a}/><span className="tile-name">{a.name}</span></button>))}</div>
          : <div className="alist">{list.map(a => (
            <div key={a.id} className="arow"><PlayButton asset={a} /><span className="clip">{a.name}</span><small>{a.seconds}s</small>
              <button className="small" onClick={() => onPick(a.id)}>{tr('storyboard.copy40')}</button></div>))}</div>}
        {!list.length && <p className="muted">{tr('storyboard.finish14',{kind:kind==='image'?tr('storyboard.detail9'):tr('storyboard.copy51')})}</p>}
      </div>
    </div>, document.body);
}

/* ---------- library ---------- */


export function AssetLibrary({onPick, selectedShot, compact,collapsed=false,onToggle}) {const {t:tr,locale}=useI18n();const {TYPE_LABEL,VARIANT_LABEL,KIND_LABEL,MODE_LABEL,QUALITY_LABEL}=displayLabels(tr);
const LIB_TABS = [['character', tr('storyboard.detail10')], ['location', tr('storyboard.detail11')], ['prop', tr('storyboard.copy88')], ['image', tr('storyboard.detail9')], ['video', tr('storyboard.detail12')], ['audio', tr('storyboard.copy51')], ['inbox', tr('storyboard.detail13')]];

  const {state, dispatch, db} = useStore();
  const [tab,setTab]=useState('character'),[q,setQ]=useState(''),[wide,setWide]=useState(false),[zoom,setZoom]=useState(null),[newProfileType,setNewProfileType]=useState(null);
  const uploadInput=useRef();
  const inbox = state.assets.filter(a => a.inbox);
  const count = k => k === 'inbox' ? inbox.length : ['character', 'location', 'prop'].includes(k) ? state.profiles.filter(p => p.type === k).length
    : state.assets.filter(a => a.kind === k && !a.inbox).length;
  const match = s => !q || s.includes(q);
  const usedP = new Set(selectedShot ? [...selectedShot.cast.map(c => c.id), selectedShot.place?.id, ...selectedShot.props] : []);
  const profileFiles=selectedShot?[...selectedShot.cast.map(c=>({p:db.profile(c.id),variant:c.variant})),...(selectedShot.place?[{p:db.profile(selectedShot.place.id),variant:selectedShot.place.variant}]:[]),...selectedShot.props.map(id=>({p:db.profile(id)}))].flatMap(({p,variant})=>p?[...(variantOf(p,variant)?.images||[]),p.voice,p.ambient].filter(Boolean):[]):[];
  const usedA=new Set(selectedShot?[...selectedShot.refs,selectedShot.start,selectedShot.end,...profileFiles].filter(Boolean):[]);
  const isProfileTab = ['character', 'location', 'prop'].includes(tab);
  return (
    <aside className={'library' + (wide ? ' wide' : '') + (compact ? ' compact' : '')+(collapsed?' collapsed':'')} aria-label={tr('storyboard.copy41')}>
      <div className="lib-head">{!collapsed&&<><b>{tr('storyboard.copy41')}</b><small>{tr('storyboard.mixed14',{profiles:state.profiles.length,assets:state.assets.length})}</small></>}
        {onToggle?<button className="small ghost library-toggle" aria-label={collapsed?tr('storyboard.detail14'):tr('storyboard.detail15')} aria-expanded={!collapsed} onClick={onToggle} title={collapsed?tr('storyboard.detail14'):tr('storyboard.detail15')}>{collapsed?'›':'‹'}</button>:<button className="small ghost" onClick={() => setWide(!wide)}>{wide ? tr('storyboard.detail16') : tr('storyboard.detail17')}</button>}</div>
      <div className="row tight"><button className="small" onClick={()=>uploadInput.current.click()} disabled={state.busy}>{tr('storyboard.copy42')}</button><input ref={uploadInput} type="file" hidden multiple accept="image/*,video/*,audio/*" onChange={event=>{dispatch({type:'uploadFiles',files:[...event.target.files]});event.target.value='';}}/></div>
      <input className="lib-search" aria-label={tr('storyboard.detail72')} placeholder={tr('storyboard.detail73')} value={q} onChange={e => setQ(e.target.value)} />
      <div className="lib-tabs">{LIB_TABS.map(([k, l], i) => (
        <React.Fragment key={k}>{i === 3 && <span className="lib-sep">{tr('storyboard.copy43')}</span>}
          <button className={(tab === k ? 'on ' : '') + (k === 'inbox' && inbox.length ? 'alert' : '')} onClick={() => setTab(k)}>{l}<small>{count(k)}</small></button>
        </React.Fragment>))}</div>
      {selectedShot && <div className="lib-ctx">{tr('storyboard.mixed15',{title:selectedShot.title})}</div>}

      {isProfileTab && <>
        <div className="plist">{state.profiles.filter(p => p.type === tab && match(p.name + p.desc)).map(p => (
          <ProfileCard key={p.id} p={p} used={usedP.has(p.id)} onOpen={() => dispatch({type: 'openProfile', id: p.id})}
            onAdd={selectedShot ? () => onPick({profile: p.id}) : null} />))}</div>
        <button className="small add-profile" disabled={state.busy} onClick={() => setNewProfileType(tab)}>
          {tr('storyboard.mixed16',{kind:TYPE_LABEL[tab]})}</button>
        <div className="lib-tip">{tab === 'character' ? tr('storyboard.detail18')
          : tab === 'location' ? tr('storyboard.detail19') : tr('storyboard.detail20')}
          {tr('storyboard.mixed20')}</div>
      </>}

      {['image', 'video', 'audio'].includes(tab) && <div className="grid">
        {state.assets.filter(a => a.kind === tab && !a.inbox && match(a.name)).map(a =>
          <Tile key={a.id} asset={a} used={usedA.has(a.id)} onClick={() => onPick?.({asset: a.id})} onZoom={setZoom} />)}</div>}

      {tab === 'inbox' && <div className="inbox">
        <button className="small" onClick={()=>uploadInput.current.click()} disabled={state.busy}>{tr('storyboard.copy44')}</button>
        {!inbox.length && <p className="muted">{tr('storyboard.copy45')}</p>}
        {inbox.map(a => <InboxRow key={a.id} a={a} />)}
      </div>}
      {zoom&&<div className="zoom" role="dialog" aria-label={tr('storyboard.detail74')} onClick={()=>setZoom(null)}>{zoom.kind==='video'?<StoryboardVideo asset={zoom} controls/>:<StoryboardAssetImage asset={zoom}/>}<b>{zoom.name}</b><small>{tr('storyboard.copy46')}</small></div>}
      {newProfileType&&<StoryboardNameDialog title={tr('storyboard.mixed9',{p0:TYPE_LABEL[newProfileType]})} label={tr('storyboard.mixed10',{p0:TYPE_LABEL[newProfileType]})} onSubmit={name=>dispatch({type:'newProfile',ptype:newProfileType,name})} onClose={()=>setNewProfileType(null)}/>}
    </aside>
  );
}

function InboxRow({a}) {const {t:tr,locale}=useI18n();const {TYPE_LABEL,VARIANT_LABEL,KIND_LABEL,MODE_LABEL,QUALITY_LABEL}=displayLabels(tr);
  const {state, dispatch, db} = useStore();
  const sug = a.suggest && db.profile(a.suggest.pid || a.suggest.id);
  const [target, setTarget] = useState(sug ? `${sug.id}|${a.suggest.vid || sug.def}` : '');
  return (
    <div className="inrow">
      <Tile asset={a} />
      <div className="inrow-body">
        <small className="clip">{a.name}</small>
        {sug ? <small className="sug">{tr('storyboard.finish15',{name:sug.name})}</small> : <small className="muted">{tr('storyboard.copy47')}</small>}
        <select aria-label={a.name+tr('storyboard.detail21')} value={target} onChange={e => setTarget(e.target.value)}>
          <option value="">{tr('storyboard.copy48')}</option>
          {state.profiles.filter(p=>a.kind==='image'||a.kind==='audio'&&['character','location'].includes(p.type)).map(p => <optgroup key={p.id} label={`${TYPE_LABEL[p.type]} · ${p.name}`}>
            {p.variants.map(v => <option key={v.id} value={`${p.id}|${v.id}`}>{p.name} · {v.name}</option>)}</optgroup>)}
        </select>
        <div className="row tight">
          <button className="small primary" disabled={!target||!['image','audio'].includes(a.kind)} onClick={() => { const [pid, vid] = target.split('|'); dispatch(a.kind==='audio'?(db.profile(pid)?.type==='character'?{type:'addVoice',asset:a.id,pid}:{type:'profile',id:pid,patch:{ambient:a.id}}):{type:'fileImage',asset:a.id,pid,vid}); }}>{tr('storyboard.copy49')}</button>
          <button className="small ghost" onClick={() => dispatch({type: 'dismissInbox', asset: a.id})}>{tr('storyboard.copy50')}</button>
        </div>
      </div>
    </div>
  );
}

/* ---------- profile editor ---------- */

function VariantDrop({pid, vid, children, onFiles}) {const {t:tr,locale}=useI18n();const {TYPE_LABEL,VARIANT_LABEL,KIND_LABEL,MODE_LABEL,QUALITY_LABEL}=displayLabels(tr);
  const {setNodeRef, isOver, active} = useDroppable({id: `vdrop:${pid}:${vid}`});
  return <div ref={setNodeRef} {...nativeDrop(onFiles)} className={'vimages' + (isOver && String(active?.id).startsWith('asset:') ? ' drop-hot' : '')}>{children}</div>;
}

function VoiceSection({p}) {const {t:tr,locale}=useI18n();const {TYPE_LABEL,VARIANT_LABEL,KIND_LABEL,MODE_LABEL,QUALITY_LABEL}=displayLabels(tr);
  const {dispatch, db} = useStore();
  const [choose, setChoose] = useState(false);
  const voices = p.voices || [];
  const {setNodeRef, isOver, active} = useDroppable({id: 'voicedrop:' + p.id});
  const hot = isOver && db.asset(String(active?.id).slice(6))?.kind === 'audio';
  return (
    <div ref={setNodeRef} className={'voices' + (hot ? ' drop-hot' : '')} {...nativeDrop(files => dispatch({type: 'uploadFiles', files, target: {kind: 'voice', pid: p.id}}))}>
      <div className="row tight"><b>{tr('storyboard.copy51')}</b><small className="muted">{tr('storyboard.copy52')}</small>
        <button className="small" onClick={() => setChoose(true)}>{tr('storyboard.copy53')}</button></div>
      <div className="drop-line">{tr('storyboard.copy54')}</div>
      {!voices.length && <div className="slot-empty">{tr('storyboard.copy55')}</div>}
      {voices.map(v => { const a = db.asset(v.asset); if (!a) return null; const max=Math.max(0,(Number.isFinite(a.seconds)?a.seconds:3)-3); return (
        <div key={v.asset} className={'voice' + (p.voice === v.asset ? ' main' : '')}>
          <div className="row tight">
            <label className="check"><input type="radio" checked={p.voice === v.asset} onChange={() => dispatch({type: 'profile', id: p.id, patch: {voice: v.asset}})} />{tr('storyboard.copy56')}</label>
            <span className="clip"><b>{a.name}</b> · {Number.isFinite(a.seconds)?a.seconds+tr('storyboard.detail22'):tr('storyboard.detail23')}{a.source === 'upload' ? tr('storyboard.detail24') : ''}</span>
            <PlayButton asset={a} label={tr('storyboard.finish2')} />
            <button className="x" title={tr('storyboard.finish3')} onClick={() => dispatch({type: 'removeVoice', pid: p.id, asset: v.asset})}>×</button>
          </div>
          <div className="row tight">
            <span className="muted">{tr('storyboard.copy57')}</span>
            <input aria-label={a.name+tr('storyboard.detail25')} type="range" min={0} max={max} step={0.5} value={Math.min(v.start, max)} disabled={!max}
              onChange={e => dispatch({type: 'voice', pid: p.id, asset: v.asset, patch: {start: +e.target.value}})} />
            <span className="muted">{tr('storyboard.finish16',{start:v.start,end:v.start+3})}</span>
            <PlayButton asset={a} start={v.start} len={3} label={tr('storyboard.finish4')} />
          </div>
        </div>); })}
      {choose && <ChooseFiles kind="audio" title={tr('storyboard.finish25',{name:p.name})} exclude={new Set(voices.map(v => v.asset))}
        onPick={id => { dispatch({type: 'addVoice', pid: p.id, asset: id}); setChoose(false); }}
        onFiles={files => dispatch({type: 'uploadFiles', files, target: {kind: 'voice', pid: p.id}})} onClose={() => setChoose(false)} />}
    </div>
  );
}

export function ProfileEditor() {const {t:tr,locale}=useI18n();const {TYPE_LABEL,VARIANT_LABEL,KIND_LABEL,MODE_LABEL,QUALITY_LABEL}=displayLabels(tr);
  const {state, dispatch, db} = useStore();
  const p = state.openProfile && db.profile(state.openProfile);
  const [vid,setVid]=useState(null),[choose,setChoose]=useState(false),[ambientChoose,setAmbientChoose]=useState(false),[newVariant,setNewVariant]=useState(false);
  useEffect(() => { setVid(null); setChoose(false); setAmbientChoose(false); setNewVariant(false); }, [state.openProfile]);
  if(!p)return null;
  const v=variantOf(p,vid||p.def);
  if(!v)return <div className="profile-overlay"><section className="profile"><h3>{p.name}</h3><p>{tr('storyboard.mixed17',{kind:VARIANT_LABEL[p.type]})}</p><button onClick={()=>dispatch({type:'addVariant',pid:p.id,name:'默认'})}>{tr('storyboard.mixed18',{kind:VARIANT_LABEL[p.type]})}</button><button onClick={()=>dispatch({type:'openProfile',id:null})}>{tr('storyboard.copy58')}</button></section></div>;
  const close = () => dispatch({type: 'openProfile', id: null});
  const audios = state.assets.filter(a => a.kind === 'audio');
  const usedIn = state.shots.filter(s => s.cast.some(c => c.id === p.id) || s.place?.id === p.id || s.props.includes(p.id));
  const upload = files => dispatch({type: 'uploadFiles', files, target: {kind: 'image', pid: p.id, vid: v.id}});
  return (
    <div className="profile-overlay" onClick={close}>
      <section className="profile" onClick={e => e.stopPropagation()}>
        <header className="ed-head">
          <div><small>{tr('storyboard.finish17',{kind:TYPE_LABEL[p.type]})}</small><input className="ed-title" aria-label={tr('storyboard.finish5')} value={p.name} onChange={e => dispatch({type: 'profile', id: p.id, patch: {name: e.target.value}})} /></div>
          <button className="x big" onClick={close}>×</button>
        </header>
        <label className="field"><span>{p.type === 'character' ? tr('storyboard.detail26') : p.type === 'location' ? tr('storyboard.detail27') : tr('storyboard.detail28')}{tr('storyboard.finish24')}</span>
          <textarea rows={2} value={p.desc} onChange={e => dispatch({type: 'profile', id: p.id, patch: {desc: e.target.value}})} /></label>

        {p.type !== 'prop' && <div className="vtabs">{p.variants.map(x => (
          <button key={x.id} className={x.id === v.id ? 'on' : ''} onClick={() => setVid(x.id)}>{x.name}{x.id === p.def && <em>{tr('storyboard.copy59')}</em>}<small>{x.images.length}</small></button>))}
          <button className="ghost" disabled={state.busy} onClick={() => setNewVariant(true)}>＋ {VARIANT_LABEL[p.type]}</button>
        </div>}

        <div className="variant">
          {p.type !== 'prop' && <div className="row tight">
            <input aria-label={VARIANT_LABEL[p.type]+tr('storyboard.detail29')} value={v.name} onChange={e => dispatch({type: 'variant', pid: p.id, vid: v.id, patch: {name: e.target.value}})} style={{maxWidth: 160}} />
            <input aria-label={VARIANT_LABEL[p.type]+tr('storyboard.detail30')} value={v.desc} placeholder={p.type === 'character' ? tr('storyboard.detail31') : tr('storyboard.detail32')}
              onChange={e => dispatch({type: 'variant', pid: p.id, vid: v.id, patch: {desc: e.target.value}})} />
            {v.id !== p.def && <button className="small" onClick={() => dispatch({type: 'profile', id: p.id, patch: {def: v.id}})}>{tr('storyboard.copy60')}</button>}
          </div>}
          <div className="row tight"><b>{tr('storyboard.copy61')}</b><small className="muted">{tr('storyboard.copy62')}</small>
            <button className="small" onClick={() => setChoose(true)}>{tr('storyboard.copy63')}</button>
            </div>
          <VariantDrop pid={p.id} vid={v.id} onFiles={upload}>
            {v.images.map((id, i) => { const a = db.asset(id); return a && (
              <div key={id} className="vimg">
                <StoryboardAssetImage asset={a}/>
                {i === 0 ? <em>{tr('storyboard.copy64')}</em> : <button className="small" onClick={() => dispatch({type: 'coverImage', pid: p.id, vid: v.id, asset: id})}>{tr('storyboard.copy65')}</button>}
                {a.source === 'ai' && <span className="tile-badge ai">AI</span>}
                <button className="x" onClick={() => dispatch({type: 'unfileImage', pid: p.id, vid: v.id, asset: id})}>×</button>
                <small className="clip">{a.name}</small>
              </div>); })}
            <div className="vimg add" onClick={() => setChoose(true)}>{tr('storyboard.copy63')}<br /><small>{tr('storyboard.copy66')}</small></div>
          </VariantDrop>
          {p.type === 'character' && !v.images.length && <div className="warn">{tr('storyboard.copy67')}</div>}
          {p.type === 'character' && (p.voices || []).length > 1 && <label className="row tight"><span className="muted">{tr('storyboard.copy68')}</span>
            <select value={v.voice || ''} onChange={e => dispatch({type: 'variant', pid: p.id, vid: v.id, patch: {voice: e.target.value || null}})}>
              <option value="">{tr('storyboard.copy69')}</option>{p.voices.map(x => <option key={x.asset} value={x.asset}>{db.asset(x.asset)?.name}</option>)}</select></label>}
        </div>

        {p.type === 'character' && <VoiceSection p={p} />}
        {p.type === 'location' && <label className="row tight"><b>{tr('storyboard.copy70')}</b>
          <select aria-label={tr('storyboard.finish6')} value={p.ambient || ''} onChange={e => dispatch({type: 'profile', id: p.id, patch: {ambient: e.target.value || null}})}>
            <option value="">{tr('storyboard.copy71')}</option>{audios.map(a => <option key={a.id} value={a.id}>{a.name}（{Number.isFinite(a.seconds)?a.seconds+tr('storyboard.detail22'):tr('storyboard.detail23')}）</option>)}</select>
          {p.ambient&&<PlayButton asset={db.asset(p.ambient)}/>}<button className="small" onClick={()=>setAmbientChoose(true)}>{tr('storyboard.copy72')}</button><small className="muted">{tr('storyboard.copy73')}</small></label>}

        <div className="usedin"><b>{tr('storyboard.finish18',{count:usedIn.length})}</b>{usedIn.map(s => <span key={s.id} className="chip">{s.title}</span>)}
          {usedIn.length > 0 && <small className="muted">{tr('storyboard.copy74')}</small>}</div>
        {ambientChoose&&<ChooseFiles kind="audio" title={tr('storyboard.finish26',{name:p.name})} exclude={new Set()} onPick={asset=>{dispatch({type:'profile',id:p.id,patch:{ambient:asset}});setAmbientChoose(false);}} onFiles={files=>dispatch({type:'uploadFiles',files,target:{kind:'ambient',pid:p.id}})} onClose={()=>setAmbientChoose(false)}/>}
        {choose && <ChooseFiles kind="image" title={tr('storyboard.mixed11',{p0:p.name,p1:v.name})} exclude={new Set(v.images)}
          onPick={id => dispatch({type: 'fileImage', asset: id, pid: p.id, vid: v.id})} onFiles={upload} onClose={() => setChoose(false)} />}
        {newVariant&&<StoryboardNameDialog key={p.id} title={tr('storyboard.mixed12',{p0:VARIANT_LABEL[p.type]})} label={tr('storyboard.mixed13',{p0:VARIANT_LABEL[p.type]})} onSubmit={name=>dispatch({type:'addVariant',pid:p.id,name})} onClose={()=>setNewVariant(false)}/>}
      </section>
    </div>
  );
}

/* ---------- right-click picker ---------- */

const SLOT_ALLOW = {start: ['location', 'prop', 'image'], end: ['location', 'prop', 'image'], refs: ['image', 'video', 'audio'],
  cast: ['character', 'location', 'prop'], smart: ['character', 'location', 'prop', 'image', 'video', 'audio']};

export function AssetPicker({shotId, slot, x, y, onClose}) {const {t:tr,locale}=useI18n();const {TYPE_LABEL,VARIANT_LABEL,KIND_LABEL,MODE_LABEL,QUALITY_LABEL}=displayLabels(tr);
const SLOT_TITLE = {start: tr('storyboard.detail33'), end: tr('storyboard.detail34'), refs: tr('storyboard.detail35'), cast: tr('storyboard.detail36'), smart: tr('storyboard.detail37')};

  const {state, dispatch, db} = useStore();
  const shot = state.shots.find(s => s.id === shotId);
  const [q, setQ] = useState(''), [tab, setTab] = useState('all'), [flash, setFlash] = useState('');
  const box = useRef(), input = useRef();
  useEffect(() => {
    input.current?.focus();
    const away = e => box.current && !box.current.contains(e.target) && onClose();
    window.addEventListener('pointerdown', away, true);
    return () => window.removeEventListener('pointerdown', away, true);
  }, [onClose]);
  if(!shot)return null;
  const allow=SLOT_ALLOW[slot]||SLOT_ALLOW.smart;
  const tabs = ['all', ...allow];
  const label = k => ({all: tr('storyboard.detail38'), ...TYPE_LABEL, image: tr('storyboard.detail9'), video: tr('storyboard.detail12'), audio: tr('storyboard.copy51')})[k];
  const ok = k => allow.includes(k) && (tab === 'all' || tab === k);
  const profiles = state.profiles.filter(p => ok(p.type) && (!q || (p.name + p.desc).includes(q)));
  const files = state.assets.filter(a => !a.inbox && ok(a.kind) && (!q || a.name.includes(q)));
  const results = [...profiles.map(p => ({profile: p})), ...files.map(a => ({asset: a}))];
  const multi = slot !== 'start' && slot !== 'end';
  const pick = async item => {
    const s = state.shots.find(x => x.id === shotId);
    const {patch, msg} = planDrop(s, item, slot === 'cast' ? 'smart' : slot, db);
    if(!patch){setFlash(msg);return;}
    const receipt=await dispatch(item.profile ? {type: 'drop', profile: item.profile.id, shot: shotId, slot: slot === 'cast' ? 'smart' : slot}
      : {type: 'drop', asset: item.asset.id, shot: shotId, slot: slot === 'cast' ? 'smart' : slot});
    if(receipt?.ok===false){setFlash(receipt.error);return;}
    if(multi)setFlash(msg);else onClose();
  };
  const usedP = new Set([...shot.cast.map(c => c.id), shot.place?.id, ...shot.props]);
  const usedA = new Set([...shot.refs, shot.start, shot.end]);
  const left=Math.max(8,Math.min(x||8,window.innerWidth-Math.min(400,window.innerWidth-16))),top=Math.max(8,Math.min(y||8,window.innerHeight-Math.min(500,window.innerHeight-16)));
  return createPortal(
    <div ref={box} className="sb-portal picker" style={{left, top}} role="dialog" aria-label={SLOT_TITLE[slot]}
      onContextMenu={e => { e.preventDefault(); e.stopPropagation(); }} onClick={e => e.stopPropagation()} onPointerDown={e => e.stopPropagation()}
      onKeyDown={e => { e.stopPropagation(); if (e.key === 'Escape') onClose(); }}>
      <div className="picker-head"><b>{SLOT_TITLE[slot]}</b><small>「{shot.title}」</small><button className="x" onClick={onClose}>×</button></div>
      <input ref={input} className="lib-search" placeholder={tr('storyboard.finish7')} value={q} onChange={e => setQ(e.target.value)}
        onKeyDown={e => { if (e.key === 'Enter' && results[0]) pick(results[0]); }} />
      <div className="lib-tabs">{tabs.map(k => <button key={k} className={tab === k ? 'on' : ''} onClick={() => setTab(k)}>{label(k)}</button>)}</div>
      {(slot === 'start' || slot === 'end') && <div className="picker-note">{tr('storyboard.copy75')}</div>}
      <div className="picker-grid">
        {profiles.map(p => { const c = coverOf(db, p, p.def); return (
          <button key={p.id} className={'tile prof ' + p.type + (usedP.has(p.id) ? ' used' : '')} onClick={() => pick({profile: p})} title={p.desc}>
            {c ? <StoryboardAssetImage asset={c}/> : <div className="tile-audio">{TYPE_LABEL[p.type]}</div>}
            <span className="tile-badge">{TYPE_LABEL[p.type]}</span>{usedP.has(p.id) && <span className="tile-used">✓</span>}
            <span className="tile-name">{p.name}</span></button>); })}
        {files.map(a => (
          <button key={a.id} className={'tile' + (usedA.has(a.id) ? ' used' : '')} onClick={() => pick({asset: a})} title={a.name}>
            {a.kind === 'audio' ? <div className="tile-audio">♪<small>{a.seconds}s</small></div> : <StoryboardAssetImage asset={a}/>}
            {a.kind === 'video' && <span className="tile-badge">▶ {a.seconds}s</span>}{usedA.has(a.id) && <span className="tile-used">✓</span>}
            <span className="tile-name">{a.name}</span></button>))}
      </div>
      {!results.length && <p className="muted">{tr('storyboard.copy76')}</p>}
      <div className="picker-foot">{flash || (multi ? tr('storyboard.detail39') : tr('storyboard.detail40'))}</div>
    </div>, document.body);
}

/* ---------- shot card ---------- */

export function useShotSortable(id) {
  const s = useSortable({id: 'shot:' + id});
  return {...s, style: {transform: CSS.Transform.toString(s.transform), transition: s.transition, opacity: s.isDragging ? 0.4 : 1}};
}

export function ModeChip({info}) {const {t:tr,locale}=useI18n();const {TYPE_LABEL,VARIANT_LABEL,KIND_LABEL,MODE_LABEL,QUALITY_LABEL}=displayLabels(tr);
  return <span className={'mode ' + info.mode.toLowerCase()} title={info.machine}>{MODE_LABEL[info.mode]||info.kind}</span>;
}
const Pill = ({status}) => <span className={'pill ' + status.tone}>{status.label}</span>;
function Progress({v}) {const {t:tr,locale}=useI18n();const {TYPE_LABEL,VARIANT_LABEL,KIND_LABEL,MODE_LABEL,QUALITY_LABEL}=displayLabels(tr);
  if(!v||!Number.isFinite(v.progress)||!['generating','running','queued'].includes(v.status))return null;
  return <div className="progress"><i style={{width: `${Math.round(v.progress * 100)}%`}} /></div>;
}
export function ShotThumb({shot}){const {t:tr,locale}=useI18n();const {TYPE_LABEL,VARIANT_LABEL,KIND_LABEL,MODE_LABEL,QUALITY_LABEL}=displayLabels(tr);const v=chosenVersion(shot);return v?<StoryboardAssetImage asset={v.asset||v} className="sthumb"/>:<div className="sthumb empty" role="img" aria-label={tr('storyboard.detail75')}><svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="m9 14 3-3 5 5M7 8h.01"/></svg></div>;}

export function WhoChips({shot, small}) {const {t:tr,locale}=useI18n();const {TYPE_LABEL,VARIANT_LABEL,KIND_LABEL,MODE_LABEL,QUALITY_LABEL}=displayLabels(tr);
  const {db} = useStore();
  return (
    <div className={'who' + (small ? ' small' : '')}>
      {shot.cast.map(c => { const p = db.profile(c.id); return p && <span key={c.id} className="chip character">{p.name}<i>{variantOf(p,c.variant)?.name||tr('storyboard.detail41')}</i></span>; })}
      {shot.place && db.profile(shot.place.id) && <span className="chip location">{db.profile(shot.place.id).name.replace('旧车站 · ', '')}</span>}
      {shot.props.map(id => db.profile(id) && <span key={id} className="chip prop">{db.profile(id).name}</span>)}
    </div>
  );
}

const dragItem = (active, db) => {
  const id = String(active?.id || '');
  if (id.startsWith('asset:')) return {asset: db.asset(id.slice(6))};
  if (id.startsWith('profile:')) return {profile: db.profile(id.slice(8))};
  return null;
};

export function ShotCard({shot, index, selected, onOpen, checked=false, onSelect}) {const {t:tr,locale}=useI18n();const {TYPE_LABEL,VARIANT_LABEL,KIND_LABEL,MODE_LABEL,QUALITY_LABEL}=displayLabels(tr);
  const {state,db,dispatch}=useStore();
  const [picker, setPicker] = useState(null);
  const {setNodeRef, attributes, listeners, style, isOver, active} = useShotSortable(shot.id);
  const info = expand(shot, db,locale);
  const status = shotStatus(shot, queuePositions(state.shots)[shot.id],locale);
  const latest = shot.versions[shot.versions.length - 1];
  const item = isOver ? dragItem(active, db) : null;
  const plan = item && (item.asset || item.profile) ? planDrop(shot, item, 'smart', db) : null;
  return (
    <div ref={setNodeRef} style={style} {...attributes} {...listeners} className={'card' + (selected ? ' selected' : '') + (plan ? ' drop-hot' : '')}
      {...nativeDrop(files=>dispatch({type:'uploadFiles',files,target:{kind:'shot',shot:shot.id,slot:'smart'}}))} onClick={event=>onOpen(shot.id,event)} onContextMenu={e => { e.preventDefault(); setPicker({slot: 'smart', x: e.clientX, y: e.clientY}); }}>
      <div className="card-media">{onSelect&&<button className="workbench-select" aria-label={tr('workbench.select')} aria-pressed={checked} onPointerDown={e=>e.stopPropagation()} onClick={e=>{e.stopPropagation();onSelect(e);}}>{checked?'●':'○'}</button>}
        <ShotThumb shot={shot} />
        <span className="card-no">{String(index + 1).padStart(2, '0')}</span>
        <span className="card-dur">{fmt(info.sec)}</span>
        <Progress v={latest} />
      </div>
      <div className="card-body">
        <div className="card-title" title={shot.title}>{shot.title}<AssistantBadge createdBy={shot.created_by}/></div>
        <div className="card-meta"><ModeChip info={info} /><span className="q">{QUALITY_LABEL[shot.quality]}</span>
          {info.warnings.length > 0 && <span className="warnbadge" title={info.warnings.join('\n')}>!</span>}</div>
        <WhoChips shot={shot} small />
        <Pill status={status} />
      </div>
      {plan && <div className={'drop-hint' + (plan.warn ? ' warn' : '')}>{plan.hint || plan.msg}</div>}
      {picker && <AssetPicker shotId={shot.id} {...picker} onClose={() => setPicker(null)} />}
    </div>
  );
}

export function AddCard({onAdd, label}) {const {t:tr,locale}=useI18n();const {TYPE_LABEL,VARIANT_LABEL,KIND_LABEL,MODE_LABEL,QUALITY_LABEL}=displayLabels(tr);
  if(label===undefined)label=tr('storyboard.detail42');
  return <button className="card add" onClick={onAdd}>{label}</button>;
}

/* ---------- shot editor ---------- */

function Slot({id,label,hint,children,onClear,onPick,onFiles,className=''}) {const {t:tr,locale}=useI18n();const {TYPE_LABEL,VARIANT_LABEL,KIND_LABEL,MODE_LABEL,QUALITY_LABEL}=displayLabels(tr);
  const {setNodeRef, isOver, active} = useDroppable({id});
  const hot = isOver && /^(asset|profile):/.test(String(active?.id));
  const open = e => { e.preventDefault(); onPick?.(e); };
  return (
    <div ref={setNodeRef} {...(onFiles?nativeDrop(onFiles):{})} className={'slot '+className + (hot ? ' drop-hot' : '')} onContextMenu={open} title={tr('storyboard.finish8')}>
      <div className="slot-label">{label}<span>{onPick && <button className="x" title={tr('storyboard.finish9')} onClick={open}>⌕</button>}
        {onClear && <button className="x" onClick={onClear}>×</button>}</span></div>
      {children || <div className="slot-empty clickable" onClick={open}>{hint}<br /><small>{tr('storyboard.copy77')}</small></div>}
    </div>
  );
}

function SaveFrame({shot,version,onDone}){const {t:tr,locale}=useI18n();const {TYPE_LABEL,VARIANT_LABEL,KIND_LABEL,MODE_LABEL,QUALITY_LABEL}=displayLabels(tr);
  const {state,dispatch,db}=useStore(),sugg=shotProfiles(shot).map(x=>({...x,p:db.profile(x.id)})).filter(x=>x.p);
  const [t,setT]=useState(0),[target,setTarget]=useState(sugg[0]?`${sugg[0].id}|${sugg[0].variant||sugg[0].p.def}`:''),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const max=version.duration||shot.duration;
  async function save(){setBusy(true);setError('');try{const [pid,vid]=target?target.split('|'):[null,null];const receipt=await dispatch({type:'saveFrame',shot:shot.id,version:version.id,artifactId:version.artifactId,t,pid,vid});if(!receipt||receipt?.ok===false){setError(receipt?.error||tr('storyboard.detail43'));return;}onDone(pid?tr('storyboard.detail44'):tr('storyboard.detail45'));}finally{setBusy(false);}}
  return <div className="saveframe"><b>{tr('storyboard.copy78')}</b><StoryboardVideo asset={version.asset||version} controls onTimeUpdate={event=>setT(Number(event.currentTarget.currentTime.toFixed(3)))}/><label className="field"><span>{tr('storyboard.copy79')}</span><input type="number" min={0} max={max} step={0.001} value={t} onChange={event=>setT(Number(event.target.value))}/></label><select aria-label={tr('storyboard.detail76')} value={target} onChange={event=>setTarget(event.target.value)}><option value="">{tr('storyboard.copy80')}</option>{sugg.map(x=><option key={x.id} value={`${x.id}|${x.variant||x.p.def}`}>{tr('storyboard.mixed19',{profile:x.p.name,version:variantOf(x.p,x.variant)?.name})}</option>)}{state.profiles.map(p=><optgroup key={p.id} label={TYPE_LABEL[p.type]+' · '+p.name}>{p.variants.map(v=><option key={v.id} value={`${p.id}|${v.id}`}>{p.name} · {v.name}</option>)}</optgroup>)}</select>{error&&<p role="alert" className="warn">{error}</p>}<button className="primary small" disabled={busy||!version.artifactId||!Number.isFinite(t)||t<0||t>max} onClick={save}>{busy?tr('storyboard.detail46'):tr('storyboard.detail47')}</button></div>;
}

export function ShotEditor({shotId, onClose, inline}) {const {t:tr,locale}=useI18n();const {TYPE_LABEL,VARIANT_LABEL,KIND_LABEL,MODE_LABEL,QUALITY_LABEL}=displayLabels(tr);
  const {state, dispatch, db} = useStore();
  const shot = state.shots.find(s => s.id === shotId);
  const [copies,setCopies]=useState('1');
  const [preview, setPreview] = useState(null), [picker, setPicker] = useState(null), [saving, setSaving] = useState(false), [msg, setMsg] = useState(''),[deleting,setDeleting]=useState(false);
  useEffect(() => { setPreview(null); setPicker(null); setSaving(false); setMsg(''); setDeleting(false); setCopies('1'); }, [shotId,state.contextId]);
  if (!shot) return null;
  const index = state.shots.indexOf(shot);
  const info = expand(shot, db,locale);
  const upd = patch => dispatch({type: 'update', id: shot.id, patch});
  const openPicker = slot => e => setPicker({slot, x: e.clientX, y: e.clientY});
  const sf = startFrame(shot, db,locale), endA = shot.end && db.asset(shot.end);
  const viewing = shot.versions.find(v => v.id === preview) || chosenVersion(shot);
  const busy=state.busy||shot.versions.some(v=>['queued','generating','waiting_capacity','claimed','submitting','running','collecting','checking'].includes(v.status));
  const restoredExact=!!shot.entity?.data.h3?.explicitReferences&&!shot.entity?.data.storyboard;
  const chosen=chosenVersion(shot),qualityLabel=QUALITY_LABEL[shot.quality]||tr('storyboard.detail48'),durationRange=info.durationRange;

  return (
    <section className={'editor' + (inline ? ' inline' : '')}>
      <header className="ed-head">
        <div><small>{tr('storyboard.finish19',{number:String(index+1).padStart(2,'0')})}</small><input className="ed-title" aria-label={tr('storyboard.finish10')} value={shot.title} onChange={e => upd({title: e.target.value})} /></div>
        {onClose && <button className="x big" aria-label={tr('storyboard.closeEditor')} onClick={onClose}>×</button>}
      </header>

      <div className="ed-preview">
        {['done','succeeded'].includes(viewing?.status)?<StoryboardVideo key={viewing.id} asset={viewing.asset||viewing} controls />
          : <div className="ed-preview-empty">{busy ? tr('storyboard.detail49') : tr('storyboard.detail50')}</div>}
        {viewing && viewing.id !== shot.chosen && ['done','succeeded'].includes(viewing.status) &&
          <button className="primary use" onClick={() => dispatch({type: 'choose', shot: shot.id, version: viewing.id})}>{tr('storyboard.finish20',{number:chosen?.n})}</button>}
      </div>
      {['done','succeeded'].includes(viewing?.status)&& (saving ? <SaveFrame shot={shot} version={viewing} onDone={t => { setSaving(false); setMsg(t); }} />
        : <div className="row tight"><StoryboardDownloads version={viewing} title={shot.title}/><button className="small" onClick={() => { setSaving(true); setMsg(''); }}>{tr('storyboard.copy81')}</button>{msg && <small className="okmsg">{msg}</small>}</div>)}

      <WorkbenchTakes shot={shot}/><WorkbenchDefaults shotId={shot.id}/>
      <label className="field"><span>{tr('storyboard.copy84')}</span><textarea rows={2} value={shot.script} onChange={e => upd({script: e.target.value})} /></label>
      <label className="field"><span>{tr('storyboard.copy85')}</span>
        <textarea rows={2} value={shot.prompt} onChange={e => upd({prompt: e.target.value})} placeholder={tr('storyboard.finish11')} /></label>

      {restoredExact?<p>{tr('workbench.restoredBindings')}</p>:<Slot onFiles={files=>dispatch({type:'uploadFiles',files,target:{kind:'shot',shot:shot.id,slot:'smart'}})} id={'slot:cast:'+shot.id} className="castslot" label={tr('storyboard.detail77')} hint={tr('storyboard.detail78')} onPick={openPicker('cast')}>
        {(shot.cast.length || shot.place || shot.props.length) ? <div className="castlist">
          {shot.cast.map(c => { const p = db.profile(c.id); if (!p) return null; const a=db.asset(variantOf(p,c.variant)?.images[0]); return (
            <div key={c.id} className="castitem character"><AssetThumb asset={a} size={34} /><div><button className="link" title={tr('storyboard.finish12')} onClick={() => dispatch({type: 'openProfile', id: p.id})}>{p.name}</button>
              <select aria-label={p.name+tr('storyboard.detail57')} value={variantOf(p,c.variant)?.id||''} onChange={e => dispatch({type: 'castVariant', shot: shot.id, id: c.id, variant: e.target.value})}>
                {!p.variants.length&&<option value="">{tr('storyboard.copy86')}</option>}
                {p.variants.map(v => <option key={v.id} value={v.id}>{v.name}{v.images.length ? '' : tr('storyboard.detail58')}</option>)}</select></div>
              <button className="x" onClick={() => dispatch({type: 'uncast', shot: shot.id, id: c.id})}>×</button></div>); })}
          {shot.place && (() => { const p = db.profile(shot.place.id); if (!p) return null; return (
            <div className="castitem location"><AssetThumb asset={coverOf(db, p, shot.place.variant)} size={34} /><div><button className="link" title={tr('storyboard.finish12')} onClick={() => dispatch({type: 'openProfile', id: p.id})}>{p.name}</button>
              <select aria-label={p.name+tr('storyboard.detail59')} value={variantOf(p,shot.place.variant)?.id||''} onChange={e => dispatch({type: 'placeVariant', shot: shot.id, variant: e.target.value})}>
                {!p.variants.length&&<option value="">{tr('storyboard.copy87')}</option>}
                {p.variants.map(v => <option key={v.id} value={v.id}>{v.name}</option>)}</select></div>
              <button className="x" onClick={() => dispatch({type: 'unplace', shot: shot.id})}>×</button></div>); })()}
          {shot.props.map(id => { const p = db.profile(id); return p && (
            <div key={id} className="castitem prop"><AssetThumb asset={coverOf(db, p)} size={34} /><div><button className="link" title={tr('storyboard.finish12')} onClick={() => dispatch({type: 'openProfile', id: p.id})}>{p.name}</button><small>{tr('storyboard.copy88')}</small></div>
              <button className="x" onClick={() => dispatch({type: 'unprop', shot: shot.id, id})}>×</button></div>); })}
        </div> : null}
      </Slot>}

      <div className="modetabs" role="tablist" aria-label={tr('storyboard.finish13')}>
        {['FL', 'REF'].map(k => (
          <button key={k} role="tab" aria-selected={info.mode === k} className={'mt ' + k.toLowerCase() + (info.mode === k ? ' on' : '')} onClick={() => upd({mode: k})}>
            <b>{MODE_LABEL[k]}</b><small>{k === 'FL' ? tr('storyboard.detail60') : tr('storyboard.detail61')}</small></button>))}
      </div>
      <div className={'modepanel ' + info.mode.toLowerCase()}>
        {info.frameInputs && <>
          <div className="slots">
            <Slot onFiles={files=>dispatch({type:'uploadFiles',files,target:{kind:'shot',shot:shot.id,slot:'start'}})} id={'slot:start:'+shot.id} label={tr('storyboard.detail79')} hint={tr('storyboard.detail81')} onPick={openPicker('start')}
              onClear={shot.start || shot.continuePrev ? () => upd(restoredExact?{start:null}:{start:null,continuePrev:false}) : null}>
              {sf&&(sf.asset?<figure><StoryboardAssetImage asset={sf.asset}/><figcaption>{sf.label}</figcaption></figure>:<div className="slot-empty">{sf.label}</div>)}
            </Slot>
            <Slot onFiles={files=>dispatch({type:'uploadFiles',files,target:{kind:'shot',shot:shot.id,slot:'end'}})} id={'slot:end:'+shot.id} label={tr('storyboard.detail80')} hint={tr('storyboard.detail81')} onPick={openPicker('end')} onClear={shot.end ? () => upd({end: null}) : null}>
              {endA && <figure><StoryboardAssetImage asset={endA}/><figcaption>{endA.name}</figcaption></figure>}
            </Slot>
          </div>
          {index > 0 && !restoredExact && <label className="check"><input type="checkbox" checked={shot.continuePrev}
            onChange={e => upd({continuePrev: e.target.checked, start: e.target.checked ? null : shot.start})} />
            {tr('storyboard.finish21',{title:state.shots[index-1].title})}</label>}
          <label className="field"><span>{tr('storyboard.finish22',{seconds:shot.duration})}</span><input type="number" min={durationRange?.minimum} max={durationRange?.maximum} step={durationRange?.step||'any'} value={shot.duration} disabled={!durationRange} onChange={event=>{const n=Number(event.target.value);if(Number.isFinite(n)&&n>0)upd({duration:n});}}/>{!durationRange&&<small>{tr('storyboard.copy89')}</small>}</label>
        </>}
        {(info.mode==='REF'||info.controlInputs)&&<>
          <Slot onFiles={files=>dispatch({type:'uploadFiles',files,target:{kind:'shot',shot:shot.id,slot:'refs'}})} id={'slot:refs:'+shot.id} label={info.mode==='FL'?tr('storyboard.detail62'):tr('storyboard.detail63')} hint={info.mode==='FL'?tr('storyboard.detail64'):tr('storyboard.detail65')} onPick={openPicker('refs')}>
            {shot.refs.length > 0 && <div className="refs">{shot.refs.map(id => db.asset(id)).filter(Boolean).map(a => (
              <span key={a.id} className="ref"><AssetThumb asset={a} size={28} />{a.name}
                <button className="x" onClick={() => dispatch({type: 'unref', shot: shot.id, asset: a.id})}>×</button></span>))}</div>}
          </Slot>
          {info.limitsLabel&&<div className="limits">{info.limitsLabel}</div>}

        </>}
      </div>

      <div className="sent">
        <div className="sent-head"><b>{tr('storyboard.copy90')}</b><ModeChip info={info} /><span className="muted">{info.machine} · {info.res} · {info.sec} {tr('storyboard.secondsUnit')}</span></div>
        <div className="sent-prompt">{info.parts.map((x, i) => <div key={i}><em>{x.label}</em>{x.text}</div>)}</div>
        {info.items.length > 0 && <div className="sent-items">{info.items.map((x, i) => (
          <div key={i} className={'sent-item' + (x.sent ? '' : ' off')} title={x.reason}>
            <AssetThumb asset={x.a} size={40}/>
            <span><b>{x.kind}</b><small>{x.why}</small>{!x.sent && <small className="warn">{tr('storyboard.finish23',{reason:x.reason})}</small>}</span></div>))}</div>}
        {info.warnings.map(w => <div key={w} className="warn">⚠ {w}</div>)}
        {info.notes.map(w => <div key={w} className="note">· {w}</div>)}
      </div>

      <div className="row">
        <div className="seg">{['draft', 'final'].map(q => (
          <button key={q} className={shot.quality === q ? 'on' : ''} onClick={() => upd({quality: q})}>{QUALITY_LABEL[q]}<small>{state.modelLabelKeys?.[q]?tr(state.modelLabelKeys[q]):typeof state.models?.[q]==='string'?state.models[q]:state.models?.[q]?.label||tr('storyboard.detail66')}</small></button>))}</div>
      </div>
      {shot.versions.length > 0 && <label className="check"><input type="checkbox" checked={shot.seedLock} onChange={e => upd({seedLock: e.target.checked})} />
        {tr('storyboard.mixed21')}</label>}
      <WorkbenchCopies value={copies} onChange={setCopies} max={state.copiesMax}/><div className="ed-actions">
        <button className="primary" disabled={busy||!Number.isInteger(Number(copies))||Number(copies)<1||Number(copies)>state.copiesMax} onClick={()=>dispatch({type:'workbenchGenerate',ids:[shot.id],copies:Number(copies)})}>
          {shot.versions.length?tr('storyboard.detail67'):tr('storyboard.detail68')} · {qualityLabel}</button>

      </div>
      {picker && <AssetPicker shotId={shot.id} {...picker} onClose={() => setPicker(null)} />}
      {deleting&&<StoryboardDeleteDialog onConfirm={async()=>{const receipt=await dispatch({type:'remove',id:shot.id});if(receipt?.ok!==false)onClose?.();return receipt;}} onClose={()=>setDeleting(false)}/>}
    </section>
  );
}

/* ---------- player + timeline ---------- */

export function usePlayer(shots) {
  const segs = useMemo(() => segments(shots), [shots]);
  const total = segs.reduce((n, s) => n + s.dur, 0);
  const [t, setT] = useState(0), [playing, setPlaying] = useState(false);
  useEffect(() => {
    if (!playing) return;
    let last = performance.now(), raf;
    const step = now => {
      const dt = (now - last) / 1000; last = now;
      setT(x => { const nx = x + dt; if (nx >= total) { setPlaying(false); return 0; } return nx; });
      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [playing, total]);
  const idx = Math.max(0, segs.findIndex(s => t >= s.start && t < s.start + s.dur));
  return {segs, total, t, setT, playing, setPlaying, seg: segs[idx], idx};
}

export function Screen({player,big,style}){const {t:tr,locale}=useI18n();const {TYPE_LABEL,VARIANT_LABEL,KIND_LABEL,MODE_LABEL,QUALITY_LABEL}=displayLabels(tr);
  const {seg,t,playing}=player,ref=useRef(),playback=useRef(null),local=seg?t-seg.start:0;
  if(!playback.current)playback.current=createStoryboardPlayback();
  const sync=()=>playback.current.sync({video:ref.current,time:local,playing,onFailure:()=>player.setPlaying(false)});
  useEffect(sync,[seg?.version?.id,seg?.shot.id,playing,local]);
  useEffect(()=>()=>playback.current.reset(),[]);
  if(!seg)return <div className="screen" style={style}><div className="screen-empty">{tr('storyboard.copy93')}</div></div>;
  return <div className={'screen'+(big?' big':'')} style={style}>{seg.version?<StoryboardVideo key={seg.shot.id+':'+seg.version.id} asset={seg.version.asset||seg.version} videoRef={ref} onLoadedMetadata={sync} onCanPlay={sync}/>:<div className="screen-empty"><b>{seg.shot.title}</b><span>{tr('storyboard.copy94')}</span></div>}<div className="screen-cap">{seg.shot.title}<small>{seg.shot.script}</small></div></div>;
}

export function Transport({player}) {const {t:tr,locale}=useI18n();const {TYPE_LABEL,VARIANT_LABEL,KIND_LABEL,MODE_LABEL,QUALITY_LABEL}=displayLabels(tr);
  const {playing, setPlaying, t, total, setT} = player;
  return (
    <div className="transport">
      <button className="primary round" aria-label={playing?tr('storyboard.detail69'):tr('storyboard.detail70')} disabled={!total} onClick={() => setPlaying(!playing)}>{playing ? '❚❚' : '▶'}</button>
      <button className="ghost" onClick={() => { setPlaying(false); setT(0); }}>⏮</button>
      <span className="time">{fmt(t)} / {fmt(total)}</span>
    </div>
  );
}

function Segment({seg, selected, onPick}) {const {t:tr,locale}=useI18n();const {TYPE_LABEL,VARIANT_LABEL,KIND_LABEL,MODE_LABEL,QUALITY_LABEL}=displayLabels(tr);
  const {state} = useStore();
  const status = shotStatus(seg.shot, queuePositions(state.shots)[seg.shot.id],locale);
  return (
    <button type="button" aria-label={tr('storyboard.detail71')+seg.shot.title} style={{flexGrow: seg.dur, flexBasis: 0}}
      className={'tseg' + (selected ? ' selected' : '')} onClick={() => onPick(seg)} title={status.label}>
      {seg.version?<StoryboardAssetImage asset={seg.version.asset||seg.version}/>:<div className="tseg-empty"/>}
      <span className="tseg-title">{seg.shot.title}</span>
      <span className={'tseg-dot ' + status.tone} />
    </button>
  );
}

export function Timeline({player, selectedId, onPick}) {const {t:tr,locale}=useI18n();const {TYPE_LABEL,VARIANT_LABEL,KIND_LABEL,MODE_LABEL,QUALITY_LABEL}=displayLabels(tr);
  const {segs, total, t, setT} = player;
  const pick = seg => { setT(seg.start); onPick?.(seg.shot.id); };
  return (
    <div className="timeline">
      <div className="track video">
        {segs.map(seg => <Segment key={seg.shot.id} seg={seg} selected={seg.shot.id === selectedId} onPick={pick} />)}
        <div className="playhead" style={{left: `${total ? (t / total) * 100 : 0}%`}} />
      </div>
      <div className="track audio"><span>{tr('storyboard.copy95')}</span></div>
      <div className="ticks">{segs.map(s => <span key={s.shot.id} style={{flexGrow: s.dur, flexBasis: 0}}>{fmt(s.start)}</span>)}</div>
    </div>
  );
}
