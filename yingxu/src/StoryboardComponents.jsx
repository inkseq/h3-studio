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

/* ---------- thumbs & draggable tiles ---------- */

export function AssetThumb({asset,size=36}) {
  if(!asset)return <span className="athumb empty" style={{width:size,height:size}}/>;
  if(asset.kind==='audio')return <span className="athumb audio" style={{width:size,height:size}}>♪</span>;
  return <StoryboardAssetImage asset={asset} className="athumb" style={{width:size,height:size}}/>;
}

function Tile({asset, used, onClick, onZoom, dragId, children}) {
  const {attributes, listeners, setNodeRef, isDragging} = useDraggable({id: dragId || 'asset:' + asset.id});
  return (
    <div ref={setNodeRef} {...listeners} {...attributes} className={'tile' + (isDragging ? ' dragging' : '') + (used ? ' used' : '')}
      onClick={onClick} title={`${asset.name}\n拖到镜头或档案上，或选中镜头后点击添加`}>
      {asset.kind === 'audio' ? <div className="tile-audio">♪<small>{asset.seconds}s</small></div>
        : <StoryboardAssetImage asset={asset}/>}
      {asset.kind === 'video' && <span className="tile-badge">▶ {asset.seconds}s</span>}
      {asset.kind === 'audio' && <span className="tile-play"><PlayButton asset={asset} /></span>}
      {asset.source === 'ai' && <span className="tile-badge ai">AI</span>}
      {asset.source === 'frame' && <span className="tile-badge">截帧</span>}
      {used && <span className="tile-used">✓</span>}
      <span className="tile-name">{asset.name}</span>
      {onZoom && asset.kind !== 'audio' && <button className="tile-zoom" onPointerDown={e => e.stopPropagation()} onClick={e => { e.stopPropagation(); onZoom(asset); }}>⤢</button>}
      {children}
    </div>
  );
}

function ProfileCard({p, used, onOpen, onAdd}) {
  const {db} = useStore();
  const drag = useDraggable({id: 'profile:' + p.id});
  const drop = useDroppable({id: 'pdrop:' + p.id});
  const cover = coverOf(db, p, p.def);
  const count = p.variants.reduce((n, v) => n + v.images.length, 0);
  const dragged = drop.isOver && String(drop.active?.id).startsWith('asset:') ? db.asset(String(drop.active.id).slice(6)) : null;
  const fileOver = !!dragged;
  const hint = !dragged ? '' : dragged.kind === 'image' ? `松开：照片存进 ${p.name}`
    : dragged.kind === 'audio' ? (p.type === 'character' ? `松开：加为 ${p.name} 的声音` : p.type === 'location' ? '松开：设为环境声' : '道具不能放声音')
    : '动作视频请拖到镜头上';
  return (
    <div ref={n => { drag.setNodeRef(n); drop.setNodeRef(n); }} {...drag.listeners} {...drag.attributes}
      className={'pcard ' + p.type + (drag.isDragging ? ' dragging' : '') + (used ? ' used' : '') + (fileOver ? ' drop-hot' : '')}
      onClick={onOpen} title="点击：打开档案编辑 · 拖到镜头上：加进镜头">
      {cover?<AssetThumb asset={cover} size={38}/>:<span className="profile-initial" aria-hidden="true">{Array.from((p.name||'档案').trim())[0]?.toLocaleUpperCase()||'档'}</span>}
      <div className="pcard-body"><b title={p.name}>{p.name}</b>
        <small>{p.type === 'prop' ? `${count} 张图` : `${p.variants.length} 个${VARIANT_LABEL[p.type]} · ${count} 张图`}{p.voices?.length ? ` · ${p.voices.length} 段声音` : ''}{p.ambient ? ' · 有环境声' : ''}</small>
        <small className="muted clip">{p.desc}</small></div>
      {onAdd && <button className="pcard-open" aria-label={used?`${p.name}已在当前镜头`:`将${p.name}加入当前镜头`} title={used?'已在当前镜头':'加到当前选中的镜头'} onPointerDown={e => e.stopPropagation()} onClick={e => { e.stopPropagation(); onAdd(); }}>{used ? '✓' : '＋'}</button>}
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
function ChooseFiles({kind, exclude, title, onPick, onFiles, onClose}) {
  const {state} = useStore();
  const [q, setQ] = useState('');
  const input = useRef();
  const list = state.assets.filter(a => a.kind === kind && !exclude.has(a.id) && (!q || a.name.includes(q)));
  return createPortal(
    <div className="sb-portal choose-overlay" onClick={onClose} onKeyDown={e => e.key === 'Escape' && onClose()}>
      <div className="choose" onClick={e => e.stopPropagation()} {...nativeDrop(files => { onFiles(files); onClose(); })}>
        <div className="picker-head"><b>{title}</b><button className="x" onClick={onClose}>×</button></div>
        <div className="row tight">
          <input className="lib-search" autoFocus placeholder="在素材库里搜索…" value={q} onChange={e => setQ(e.target.value)} />
          <button className="primary" onClick={() => input.current.click()}>从电脑选择…</button>
          <input ref={input} type="file" multiple hidden accept={kind === 'image' ? 'image/*' : 'audio/*'}
            onChange={async e => { onFiles(await readFiles(e.target.files)); onClose(); }} />
        </div>
        <div className="picker-note">也可以把文件从电脑桌面直接拖进这个窗口</div>
        {kind === 'image' ? <div className="picker-grid">{list.map(a => (
          <button key={a.id} className="tile" onClick={() => onPick(a.id)} title={a.name}><StoryboardAssetImage asset={a}/><span className="tile-name">{a.name}</span></button>))}</div>
          : <div className="alist">{list.map(a => (
            <div key={a.id} className="arow"><PlayButton asset={a} /><span className="clip">{a.name}</span><small>{a.seconds}s</small>
              <button className="small" onClick={() => onPick(a.id)}>添加</button></div>))}</div>}
        {!list.length && <p className="muted">素材库里没有可添加的{kind === 'image' ? '图片' : '声音'}了，从电脑选择吧</p>}
      </div>
    </div>, document.body);
}

/* ---------- library ---------- */

const LIB_TABS = [['character', '人物'], ['location', '场景'], ['prop', '道具'], ['image', '图片'], ['video', '动作'], ['audio', '声音'], ['inbox', '待整理']];

export function AssetLibrary({onPick, selectedShot, compact,collapsed=false,onToggle}) {
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
    <aside className={'library' + (wide ? ' wide' : '') + (compact ? ' compact' : '')+(collapsed?' collapsed':'')} aria-label="素材库">
      <div className="lib-head">{!collapsed&&<><b>素材库</b><small>{state.profiles.length} 个档案 · {state.assets.length} 个文件</small></>}
        {onToggle?<button className="small ghost library-toggle" aria-label={collapsed?'展开素材库':'收起素材库'} aria-expanded={!collapsed} onClick={onToggle} title={collapsed?'展开素材库':'收起素材库'}>{collapsed?'›':'‹'}</button>:<button className="small ghost" onClick={() => setWide(!wide)}>{wide ? '收起' : '展开'}</button>}</div>
      <div className="row tight"><button className="small" onClick={()=>uploadInput.current.click()} disabled={state.busy}>＋ 上传素材</button><input ref={uploadInput} type="file" hidden multiple accept="image/*,video/*,audio/*" onChange={event=>{dispatch({type:'uploadFiles',files:[...event.target.files]});event.target.value='';}}/></div>
      <input className="lib-search" aria-label="搜索档案和文件" placeholder="搜索档案、文件…" value={q} onChange={e => setQ(e.target.value)} />
      <div className="lib-tabs">{LIB_TABS.map(([k, l], i) => (
        <React.Fragment key={k}>{i === 3 && <span className="lib-sep">文件</span>}
          <button className={(tab === k ? 'on ' : '') + (k === 'inbox' && inbox.length ? 'alert' : '')} onClick={() => setTab(k)}>{l}<small>{count(k)}</small></button>
        </React.Fragment>))}</div>
      {selectedShot && <div className="lib-ctx">当前镜头「{selectedShot.title}」· ✓ 已用 · 点击即添加</div>}

      {isProfileTab && <>
        <div className="plist">{state.profiles.filter(p => p.type === tab && match(p.name + p.desc)).map(p => (
          <ProfileCard key={p.id} p={p} used={usedP.has(p.id)} onOpen={() => dispatch({type: 'openProfile', id: p.id})}
            onAdd={selectedShot ? () => onPick({profile: p.id}) : null} />))}</div>
        <button className="small add-profile" disabled={state.busy} onClick={() => setNewProfileType(tab)}>
          ＋ 新建{TYPE_LABEL[tab]}</button>
        <div className="lib-tip">{tab === 'character' ? '人物档案 = 描述 + 多套造型照片 + 声音。拖到镜头上，自动带上照片、声音和描述。'
          : tab === 'location' ? '场景档案 = 布局描述 + 白天/夜晚等版本的图 + 环境声。' : '道具档案 = 描述 + 几张图。'}
          点「档案」打开后，可以从左边“图片”里把照片直接拖进去；上传的新图在“待整理”里一键归档。</div>
      </>}

      {['image', 'video', 'audio'].includes(tab) && <div className="grid">
        {state.assets.filter(a => a.kind === tab && !a.inbox && match(a.name)).map(a =>
          <Tile key={a.id} asset={a} used={usedA.has(a.id)} onClick={() => onPick?.({asset: a.id})} onZoom={setZoom} />)}</div>}

      {tab === 'inbox' && <div className="inbox">
        <button className="small" onClick={()=>uploadInput.current.click()} disabled={state.busy}>＋ 上传文件</button>
        {!inbox.length && <p className="muted">没有待整理的文件。上传的、截帧没选档案的会出现在这里。</p>}
        {inbox.map(a => <InboxRow key={a.id} a={a} />)}
      </div>}
      {zoom&&<div className="zoom" role="dialog" aria-label="素材预览" onClick={()=>setZoom(null)}>{zoom.kind==='video'?<StoryboardVideo asset={zoom} controls/>:<StoryboardAssetImage asset={zoom}/>}<b>{zoom.name}</b><small>点击关闭</small></div>}
      {newProfileType&&<StoryboardNameDialog title={`新建${TYPE_LABEL[newProfileType]}`} label={`${TYPE_LABEL[newProfileType]}名称`} onSubmit={name=>dispatch({type:'newProfile',ptype:newProfileType,name})} onClose={()=>setNewProfileType(null)}/>}
    </aside>
  );
}

function InboxRow({a}) {
  const {state, dispatch, db} = useStore();
  const sug = a.suggest && db.profile(a.suggest.pid || a.suggest.id);
  const [target, setTarget] = useState(sug ? `${sug.id}|${a.suggest.vid || sug.def}` : '');
  return (
    <div className="inrow">
      <Tile asset={a} />
      <div className="inrow-body">
        <small className="clip">{a.name}</small>
        {sug ? <small className="sug">看起来是「{sug.name}」</small> : <small className="muted">没认出来，手动选</small>}
        <select aria-label={a.name+'的归档位置'} value={target} onChange={e => setTarget(e.target.value)}>
          <option value="">选择档案…</option>
          {state.profiles.filter(p=>a.kind==='image'||a.kind==='audio'&&['character','location'].includes(p.type)).map(p => <optgroup key={p.id} label={`${TYPE_LABEL[p.type]} · ${p.name}`}>
            {p.variants.map(v => <option key={v.id} value={`${p.id}|${v.id}`}>{p.name} · {v.name}</option>)}</optgroup>)}
        </select>
        <div className="row tight">
          <button className="small primary" disabled={!target||!['image','audio'].includes(a.kind)} onClick={() => { const [pid, vid] = target.split('|'); dispatch(a.kind==='audio'?(db.profile(pid)?.type==='character'?{type:'addVoice',asset:a.id,pid}:{type:'profile',id:pid,patch:{ambient:a.id}}):{type:'fileImage',asset:a.id,pid,vid}); }}>归档</button>
          <button className="small ghost" onClick={() => dispatch({type: 'dismissInbox', asset: a.id})}>留在文件里</button>
        </div>
      </div>
    </div>
  );
}

/* ---------- profile editor ---------- */

function VariantDrop({pid, vid, children, onFiles}) {
  const {setNodeRef, isOver, active} = useDroppable({id: `vdrop:${pid}:${vid}`});
  return <div ref={setNodeRef} {...nativeDrop(onFiles)} className={'vimages' + (isOver && String(active?.id).startsWith('asset:') ? ' drop-hot' : '')}>{children}</div>;
}

function VoiceSection({p}) {
  const {dispatch, db} = useStore();
  const [choose, setChoose] = useState(false);
  const voices = p.voices || [];
  const {setNodeRef, isOver, active} = useDroppable({id: 'voicedrop:' + p.id});
  const hot = isOver && db.asset(String(active?.id).slice(6))?.kind === 'audio';
  return (
    <div ref={setNodeRef} className={'voices' + (hot ? ' drop-hot' : '')} {...nativeDrop(files => dispatch({type: 'uploadFiles', files, target: {kind: 'voice', pid: p.id}}))}>
      <div className="row tight"><b>声音</b><small className="muted">说话样本：全能参考生成时截其中 3 秒，让人物的声音保持一致</small>
        <button className="small" onClick={() => setChoose(true)}>＋ 添加声音</button></div>
      <div className="drop-line">把声音从左边素材库「声音」或电脑桌面拖到这里</div>
      {!voices.length && <div className="slot-empty">还没有声音样本。没有声音时，模型会自己配声音。</div>}
      {voices.map(v => { const a = db.asset(v.asset); if (!a) return null; const max=Math.max(0,(Number.isFinite(a.seconds)?a.seconds:3)-3); return (
        <div key={v.asset} className={'voice' + (p.voice === v.asset ? ' main' : '')}>
          <div className="row tight">
            <label className="check"><input type="radio" checked={p.voice === v.asset} onChange={() => dispatch({type: 'profile', id: p.id, patch: {voice: v.asset}})} />主声音</label>
            <span className="clip"><b>{a.name}</b> · {Number.isFinite(a.seconds)?a.seconds+' 秒':'时长待读取'}{a.source === 'upload' ? ' · 刚上传' : ''}</span>
            <PlayButton asset={a} label="整段" />
            <button className="x" title="移除" onClick={() => dispatch({type: 'removeVoice', pid: p.id, asset: v.asset})}>×</button>
          </div>
          <div className="row tight">
            <span className="muted">生成用的 3 秒：</span>
            <input aria-label={a.name+'的三秒片段起点'} type="range" min={0} max={max} step={0.5} value={Math.min(v.start, max)} disabled={!max}
              onChange={e => dispatch({type: 'voice', pid: p.id, asset: v.asset, patch: {start: +e.target.value}})} />
            <span className="muted">第 {v.start}–{v.start + 3} 秒</span>
            <PlayButton asset={a} start={v.start} len={3} label="试听这 3 秒" />
          </div>
        </div>); })}
      {choose && <ChooseFiles kind="audio" title={`给${p.name}添加声音`} exclude={new Set(voices.map(v => v.asset))}
        onPick={id => { dispatch({type: 'addVoice', pid: p.id, asset: id}); setChoose(false); }}
        onFiles={files => dispatch({type: 'uploadFiles', files, target: {kind: 'voice', pid: p.id}})} onClose={() => setChoose(false)} />}
    </div>
  );
}

export function ProfileEditor() {
  const {state, dispatch, db} = useStore();
  const p = state.openProfile && db.profile(state.openProfile);
  const [vid,setVid]=useState(null),[choose,setChoose]=useState(false),[ambientChoose,setAmbientChoose]=useState(false),[newVariant,setNewVariant]=useState(false);
  useEffect(() => { setVid(null); setChoose(false); setAmbientChoose(false); setNewVariant(false); }, [state.openProfile]);
  if(!p)return null;
  const v=variantOf(p,vid||p.def);
  if(!v)return <div className="profile-overlay"><section className="profile"><h3>{p.name}</h3><p>先添加{VARIANT_LABEL[p.type]}，再放照片。</p><button onClick={()=>dispatch({type:'addVariant',pid:p.id,name:'默认'})}>＋ 添加{VARIANT_LABEL[p.type]}</button><button onClick={()=>dispatch({type:'openProfile',id:null})}>关闭</button></section></div>;
  const close = () => dispatch({type: 'openProfile', id: null});
  const audios = state.assets.filter(a => a.kind === 'audio');
  const usedIn = state.shots.filter(s => s.cast.some(c => c.id === p.id) || s.place?.id === p.id || s.props.includes(p.id));
  const upload = files => dispatch({type: 'uploadFiles', files, target: {kind: 'image', pid: p.id, vid: v.id}});
  return (
    <div className="profile-overlay" onClick={close}>
      <section className="profile" onClick={e => e.stopPropagation()}>
        <header className="ed-head">
          <div><small>{TYPE_LABEL[p.type]}档案</small><input className="ed-title" aria-label="档案名称" value={p.name} onChange={e => dispatch({type: 'profile', id: p.id, patch: {name: e.target.value}})} /></div>
          <button className="x big" onClick={close}>×</button>
        </header>
        <label className="field"><span>{p.type === 'character' ? '人物描述（长相、年龄、性格）' : p.type === 'location' ? '场景描述（布局、空间关系）' : '道具描述'} · 会自动拼进提示词</span>
          <textarea rows={2} value={p.desc} onChange={e => dispatch({type: 'profile', id: p.id, patch: {desc: e.target.value}})} /></label>

        {p.type !== 'prop' && <div className="vtabs">{p.variants.map(x => (
          <button key={x.id} className={x.id === v.id ? 'on' : ''} onClick={() => setVid(x.id)}>{x.name}{x.id === p.def && <em>默认</em>}<small>{x.images.length}</small></button>))}
          <button className="ghost" disabled={state.busy} onClick={() => setNewVariant(true)}>＋ {VARIANT_LABEL[p.type]}</button>
        </div>}

        <div className="variant">
          {p.type !== 'prop' && <div className="row tight">
            <input aria-label={VARIANT_LABEL[p.type]+'名称'} value={v.name} onChange={e => dispatch({type: 'variant', pid: p.id, vid: v.id, patch: {name: e.target.value}})} style={{maxWidth: 160}} />
            <input aria-label={VARIANT_LABEL[p.type]+'描述'} value={v.desc} placeholder={p.type === 'character' ? '这套造型的衣服、发型…' : '光线、天气、时间…'}
              onChange={e => dispatch({type: 'variant', pid: p.id, vid: v.id, patch: {desc: e.target.value}})} />
            {v.id !== p.def && <button className="small" onClick={() => dispatch({type: 'profile', id: p.id, patch: {def: v.id}})}>设为默认</button>}
          </div>}
          <div className="row tight"><b>照片</b><small className="muted">第一张是主图，生成时优先用</small>
            <button className="small" onClick={() => setChoose(true)}>＋ 添加照片</button>
            </div>
          <VariantDrop pid={p.id} vid={v.id} onFiles={upload}>
            {v.images.map((id, i) => { const a = db.asset(id); return a && (
              <div key={id} className="vimg">
                <StoryboardAssetImage asset={a}/>
                {i === 0 ? <em>主图 · 生成时优先用</em> : <button className="small" onClick={() => dispatch({type: 'coverImage', pid: p.id, vid: v.id, asset: id})}>设为主图</button>}
                {a.source === 'ai' && <span className="tile-badge ai">AI</span>}
                <button className="x" onClick={() => dispatch({type: 'unfileImage', pid: p.id, vid: v.id, asset: id})}>×</button>
                <small className="clip">{a.name}</small>
              </div>); })}
            <div className="vimg add" onClick={() => setChoose(true)}>＋ 添加照片<br /><small>点这里选择，或把图片从左边素材库 / 电脑桌面拖进来</small></div>
          </VariantDrop>
          {p.type === 'character' && !v.images.length && <div className="warn">这套造型还没有照片：生成时只能靠文字描述，人物容易不一致</div>}
          {p.type === 'character' && (p.voices || []).length > 1 && <label className="row tight"><span className="muted">这套造型用的声音：</span>
            <select value={v.voice || ''} onChange={e => dispatch({type: 'variant', pid: p.id, vid: v.id, patch: {voice: e.target.value || null}})}>
              <option value="">跟随主声音</option>{p.voices.map(x => <option key={x.asset} value={x.asset}>{db.asset(x.asset)?.name}</option>)}</select></label>}
        </div>

        {p.type === 'character' && <VoiceSection p={p} />}
        {p.type === 'location' && <label className="row tight"><b>环境声</b>
          <select aria-label="场景环境声" value={p.ambient || ''} onChange={e => dispatch({type: 'profile', id: p.id, patch: {ambient: e.target.value || null}})}>
            <option value="">无</option>{audios.map(a => <option key={a.id} value={a.id}>{a.name}（{Number.isFinite(a.seconds)?a.seconds+' 秒':'时长待读取'}）</option>)}</select>
          {p.ambient&&<PlayButton asset={db.asset(p.ambient)}/>}<button className="small" onClick={()=>setAmbientChoose(true)}>＋ 环境声</button><small className="muted">全能参考时作为声音参考（优先级最低）</small></label>}

        <div className="usedin"><b>用在 {usedIn.length} 个镜头</b>{usedIn.map(s => <span key={s.id} className="chip">{s.title}</span>)}
          {usedIn.length > 0 && <small className="muted">改了档案后，这些镜头下次生成会自动用新内容（不会自动重新生成）</small>}</div>
        {ambientChoose&&<ChooseFiles kind="audio" title={`给${p.name}添加环境声`} exclude={new Set()} onPick={asset=>{dispatch({type:'profile',id:p.id,patch:{ambient:asset}});setAmbientChoose(false);}} onFiles={files=>dispatch({type:'uploadFiles',files,target:{kind:'ambient',pid:p.id}})} onClose={()=>setAmbientChoose(false)}/>}
        {choose && <ChooseFiles kind="image" title={`给「${p.name} · ${v.name}」添加照片`} exclude={new Set(v.images)}
          onPick={id => dispatch({type: 'fileImage', asset: id, pid: p.id, vid: v.id})} onFiles={upload} onClose={() => setChoose(false)} />}
        {newVariant&&<StoryboardNameDialog key={p.id} title={`新建${VARIANT_LABEL[p.type]}`} label={`${VARIANT_LABEL[p.type]}名称`} onSubmit={name=>dispatch({type:'addVariant',pid:p.id,name})} onClose={()=>setNewVariant(false)}/>}
      </section>
    </div>
  );
}

/* ---------- right-click picker ---------- */

const SLOT_TITLE = {start: '选开头画面', end: '选结尾画面', refs: '添加附加素材', cast: '这一镜里有谁 / 在哪 / 什么道具', smart: '给这个镜头加东西'};
const SLOT_ALLOW = {start: ['location', 'prop', 'image'], end: ['location', 'prop', 'image'], refs: ['image', 'video', 'audio'],
  cast: ['character', 'location', 'prop'], smart: ['character', 'location', 'prop', 'image', 'video', 'audio']};

export function AssetPicker({shotId, slot, x, y, onClose}) {
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
  const label = k => ({all: '全部', ...TYPE_LABEL, image: '图片', video: '动作', audio: '声音'})[k];
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
      <input ref={input} className="lib-search" placeholder="搜索… 回车选第一个" value={q} onChange={e => setQ(e.target.value)}
        onKeyDown={e => { if (e.key === 'Enter' && results[0]) pick(results[0]); }} />
      <div className="lib-tabs">{tabs.map(k => <button key={k} className={tab === k ? 'on' : ''} onClick={() => setTab(k)}>{label(k)}</button>)}</div>
      {(slot === 'start' || slot === 'end') && <div className="picker-note">开头/结尾画面：用场景、道具档案的主图，或任意图片</div>}
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
      {!results.length && <p className="muted">没有匹配的内容</p>}
      <div className="picker-foot">{flash || (multi ? '可以连续点选 · Esc 关闭' : '点一下就放进去 · Esc 关闭')}</div>
    </div>, document.body);
}

/* ---------- shot card ---------- */

export function useShotSortable(id) {
  const s = useSortable({id: 'shot:' + id});
  return {...s, style: {transform: CSS.Transform.toString(s.transform), transition: s.transition, opacity: s.isDragging ? 0.4 : 1}};
}

export function ModeChip({info}) {
  return <span className={'mode ' + info.mode.toLowerCase()} title={info.machine}>{info.kind}</span>;
}
const Pill = ({status}) => <span className={'pill ' + status.tone}>{status.label}</span>;
function Progress({v}) {
  if(!v||!Number.isFinite(v.progress)||!['generating','running','queued'].includes(v.status))return null;
  return <div className="progress"><i style={{width: `${Math.round(v.progress * 100)}%`}} /></div>;
}
export function ShotThumb({shot}){const v=chosenVersion(shot);return v?<StoryboardAssetImage asset={v.asset||v} className="sthumb"/>:<div className="sthumb empty" role="img" aria-label="尚未生成镜头预览"><svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="m9 14 3-3 5 5M7 8h.01"/></svg></div>;}

export function WhoChips({shot, small}) {
  const {db} = useStore();
  return (
    <div className={'who' + (small ? ' small' : '')}>
      {shot.cast.map(c => { const p = db.profile(c.id); return p && <span key={c.id} className="chip character">{p.name}<i>{variantOf(p,c.variant)?.name||'未选造型'}</i></span>; })}
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

export function ShotCard({shot, index, selected, onOpen}) {
  const {state,db,dispatch}=useStore();
  const [picker, setPicker] = useState(null);
  const {setNodeRef, attributes, listeners, style, isOver, active} = useShotSortable(shot.id);
  const info = expand(shot, db);
  const status = shotStatus(shot, queuePositions(state.shots)[shot.id]);
  const latest = shot.versions[shot.versions.length - 1];
  const item = isOver ? dragItem(active, db) : null;
  const plan = item && (item.asset || item.profile) ? planDrop(shot, item, 'smart', db) : null;
  return (
    <div ref={setNodeRef} style={style} {...attributes} {...listeners} className={'card' + (selected ? ' selected' : '') + (plan ? ' drop-hot' : '')}
      {...nativeDrop(files=>dispatch({type:'uploadFiles',files,target:{kind:'shot',shot:shot.id,slot:'smart'}}))} onClick={()=>onOpen(shot.id)} onContextMenu={e => { e.preventDefault(); setPicker({slot: 'smart', x: e.clientX, y: e.clientY}); }}>
      <div className="card-media">
        <ShotThumb shot={shot} />
        <span className="card-no">{String(index + 1).padStart(2, '0')}</span>
        <span className="card-dur">{fmt(info.sec)}</span>
        <Progress v={latest} />
      </div>
      <div className="card-body">
        <div className="card-title" title={shot.title}>{shot.title}</div>
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

export function AddCard({onAdd, label = '＋ 新镜头'}) {
  return <button className="card add" onClick={onAdd}>{label}</button>;
}

/* ---------- shot editor ---------- */

function Slot({id,label,hint,children,onClear,onPick,onFiles,className=''}) {
  const {setNodeRef, isOver, active} = useDroppable({id});
  const hot = isOver && /^(asset|profile):/.test(String(active?.id));
  const open = e => { e.preventDefault(); onPick?.(e); };
  return (
    <div ref={setNodeRef} {...(onFiles?nativeDrop(onFiles):{})} className={'slot '+className + (hot ? ' drop-hot' : '')} onContextMenu={open} title="右键：搜索">
      <div className="slot-label">{label}<span>{onPick && <button className="x" title="搜索" onClick={open}>⌕</button>}
        {onClear && <button className="x" onClick={onClear}>×</button>}</span></div>
      {children || <div className="slot-empty clickable" onClick={open}>{hint}<br /><small>拖进来，或点这里 / 右键搜索</small></div>}
    </div>
  );
}

function SaveFrame({shot,version,onDone}){
  const {state,dispatch,db}=useStore(),sugg=shotProfiles(shot).map(x=>({...x,p:db.profile(x.id)})).filter(x=>x.p);
  const [t,setT]=useState(0),[target,setTarget]=useState(sugg[0]?`${sugg[0].id}|${sugg[0].variant||sugg[0].p.def}`:''),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const max=version.duration||shot.duration;
  async function save(){setBusy(true);setError('');try{const [pid,vid]=target?target.split('|'):[null,null];const receipt=await dispatch({type:'saveFrame',shot:shot.id,version:version.id,artifactId:version.artifactId,t,pid,vid});if(!receipt||receipt?.ok===false){setError(receipt?.error||'保存尚未确认，请核对原记录后再试。');return;}onDone(pid?'已保存为档案中的参考图片':'已保存到待整理');}finally{setBusy(false);}}
  return <div className="saveframe"><b>截这一帧存为参考</b><StoryboardVideo asset={version.asset||version} controls onTimeUpdate={event=>setT(Number(event.currentTarget.currentTime.toFixed(3)))}/><label className="field"><span>取帧时间（秒）</span><input type="number" min={0} max={max} step={0.001} value={t} onChange={event=>setT(Number(event.target.value))}/></label><select aria-label="截帧归档位置" value={target} onChange={event=>setTarget(event.target.value)}><option value="">放进待整理，稍后归档</option>{sugg.map(x=><option key={x.id} value={`${x.id}|${x.variant||x.p.def}`}>建议：{x.p.name} · {variantOf(x.p,x.variant)?.name}（这一镜里有）</option>)}{state.profiles.map(p=><optgroup key={p.id} label={TYPE_LABEL[p.type]+' · '+p.name}>{p.variants.map(v=><option key={v.id} value={`${p.id}|${v.id}`}>{p.name} · {v.name}</option>)}</optgroup>)}</select>{error&&<p role="alert" className="warn">{error}</p>}<button className="primary small" disabled={busy||!version.artifactId||!Number.isFinite(t)||t<0||t>max} onClick={save}>{busy?'保存图片中…':'保存这一帧'}</button></div>;
}

export function ShotEditor({shotId, onClose, inline}) {
  const {state, dispatch, db} = useStore();
  const shot = state.shots.find(s => s.id === shotId);
  const [preview, setPreview] = useState(null), [picker, setPicker] = useState(null), [saving, setSaving] = useState(false), [msg, setMsg] = useState(''),[deleting,setDeleting]=useState(false);
  useEffect(() => { setPreview(null); setPicker(null); setSaving(false); setMsg(''); setDeleting(false); }, [shotId]);
  if (!shot) return null;
  const index = state.shots.indexOf(shot);
  const info = expand(shot, db);
  const upd = patch => dispatch({type: 'update', id: shot.id, patch});
  const openPicker = slot => e => setPicker({slot, x: e.clientX, y: e.clientY});
  const sf = startFrame(shot, db), endA = shot.end && db.asset(shot.end);
  const viewing = shot.versions.find(v => v.id === preview) || chosenVersion(shot);
  const busy=state.busy||shot.versions.some(v=>['queued','generating','waiting_capacity','claimed','submitting','running','collecting','checking'].includes(v.status));
  const chosen=chosenVersion(shot),qualityLabel=QUALITY_LABEL[shot.quality]||'草稿',durationRange=info.durationRange;

  return (
    <section className={'editor' + (inline ? ' inline' : '')}>
      <header className="ed-head">
        <div><small>镜头 {String(index + 1).padStart(2, '0')}</small><input className="ed-title" aria-label="镜头名称" value={shot.title} onChange={e => upd({title: e.target.value})} /></div>
        {onClose && <button className="x big" onClick={onClose}>×</button>}
      </header>

      <div className="ed-preview">
        {['done','succeeded'].includes(viewing?.status)?<StoryboardVideo key={viewing.id} asset={viewing.asset||viewing} controls />
          : <div className="ed-preview-empty">{busy ? '生成中…' : '还没有版本，写好下面的内容后点生成'}</div>}
        {viewing && viewing.id !== shot.chosen && ['done','succeeded'].includes(viewing.status) &&
          <button className="primary use" onClick={() => dispatch({type: 'choose', shot: shot.id, version: viewing.id})}>用这个版本（替换时间线上的 v{chosen?.n}）</button>}
      </div>
      {['done','succeeded'].includes(viewing?.status)&& (saving ? <SaveFrame shot={shot} version={viewing} onDone={t => { setSaving(false); setMsg(t); }} />
        : <div className="row tight"><StoryboardDownloads version={viewing} title={shot.title}/><button className="small" onClick={() => { setSaving(true); setMsg(''); }}>📌 截一帧存为参考</button>{msg && <small className="okmsg">{msg}</small>}</div>)}

      {shot.versions.length > 0 && <div className="versions">
        {shot.versions.map(v => (
          <button key={v.id} className={'ver' + (v.id === shot.chosen ? ' chosen' : '') + (v.id === viewing?.id ? ' viewing' : '')} onClick={() => ['done','succeeded'].includes(v.status)&&setPreview(v.id)}>
            {['done','succeeded'].includes(v.status)?<StoryboardAssetImage asset={v.asset||v}/>:<div className="ver-wait">{v.status==='queued'?'排队':Number.isFinite(v.progress)?`${Math.round(v.progress*100)}%`:v.message||({running:'生成中',collecting:'收集结果'}[v.status]||'状态待更新')}</div>}
            <span>v{v.n} · {QUALITY_LABEL[v.quality]||'视频'}{v.sameSeedAs ? ' · 同种子' : ''}</span>
            {v.id === shot.chosen && <em>使用中</em>}
            {v.status === 'queued' && <i className="x" onClick={e => { e.stopPropagation(); dispatch({type:'cancel',shot:shot.id,version:v.id,jobId:v.jobId}); }}>取消</i>}
          </button>))}
      </div>}

      <label className="field"><span>剧本（这一镜发生什么）</span><textarea rows={2} value={shot.script} onChange={e => upd({script: e.target.value})} /></label>
      <label className="field"><span>动作描述 · 只写动作，人物和场景的样子来自档案</span>
        <textarea rows={2} value={shot.prompt} onChange={e => upd({prompt: e.target.value})} placeholder="例如：她拆开信，表情从疑惑变成震惊" /></label>

      <Slot onFiles={files=>dispatch({type:'uploadFiles',files,target:{kind:'shot',shot:shot.id,slot:'smart'}})} id={'slot:cast:'+shot.id} className="castslot" label="这一镜里（人物 / 场景 / 道具）· 两种模式都用" hint="拖入人物、场景或道具档案" onPick={openPicker('cast')}>
        {(shot.cast.length || shot.place || shot.props.length) ? <div className="castlist">
          {shot.cast.map(c => { const p = db.profile(c.id); if (!p) return null; const a=db.asset(variantOf(p,c.variant)?.images[0]); return (
            <div key={c.id} className="castitem character"><AssetThumb asset={a} size={34} /><div><button className="link" title="打开档案" onClick={() => dispatch({type: 'openProfile', id: p.id})}>{p.name}</button>
              <select aria-label={p.name+'的造型'} value={variantOf(p,c.variant)?.id||''} onChange={e => dispatch({type: 'castVariant', shot: shot.id, id: c.id, variant: e.target.value})}>
                {!p.variants.length&&<option value="">尚无造型</option>}
                {p.variants.map(v => <option key={v.id} value={v.id}>{v.name}{v.images.length ? '' : '（无照片）'}</option>)}</select></div>
              <button className="x" onClick={() => dispatch({type: 'uncast', shot: shot.id, id: c.id})}>×</button></div>); })}
          {shot.place && (() => { const p = db.profile(shot.place.id); if (!p) return null; return (
            <div className="castitem location"><AssetThumb asset={coverOf(db, p, shot.place.variant)} size={34} /><div><button className="link" title="打开档案" onClick={() => dispatch({type: 'openProfile', id: p.id})}>{p.name}</button>
              <select aria-label={p.name+'的版本'} value={variantOf(p,shot.place.variant)?.id||''} onChange={e => dispatch({type: 'placeVariant', shot: shot.id, variant: e.target.value})}>
                {!p.variants.length&&<option value="">尚无版本</option>}
                {p.variants.map(v => <option key={v.id} value={v.id}>{v.name}</option>)}</select></div>
              <button className="x" onClick={() => dispatch({type: 'unplace', shot: shot.id})}>×</button></div>); })()}
          {shot.props.map(id => { const p = db.profile(id); return p && (
            <div key={id} className="castitem prop"><AssetThumb asset={coverOf(db, p)} size={34} /><div><button className="link" title="打开档案" onClick={() => dispatch({type: 'openProfile', id: p.id})}>{p.name}</button><small>道具</small></div>
              <button className="x" onClick={() => dispatch({type: 'unprop', shot: shot.id, id})}>×</button></div>); })}
        </div> : null}
      </Slot>

      <div className="modetabs" role="tablist" aria-label="生成模式（二选一）">
        {['FL', 'REF'].map(k => (
          <button key={k} role="tab" aria-selected={info.mode === k} className={'mt ' + k.toLowerCase() + (info.mode === k ? ' on' : '')} onClick={() => upd({mode: k})}>
            <b>{MODE_LABEL[k]}</b><small>{k === 'FL' ? '人物场景只用文字 · 可设开头 / 结尾画面' : '人物场景用照片和声音 · 可加动作视频'}</small></button>))}
      </div>
      <div className={'modepanel ' + info.mode.toLowerCase()}>
        {info.frameInputs && <>
          <div className="slots">
            <Slot onFiles={files=>dispatch({type:'uploadFiles',files,target:{kind:'shot',shot:shot.id,slot:'start'}})} id={'slot:start:'+shot.id} label="开头画面" hint="可留空" onPick={openPicker('start')}
              onClear={shot.start || shot.continuePrev ? () => upd({start: null, continuePrev: false}) : null}>
              {sf&&(sf.asset?<figure><StoryboardAssetImage asset={sf.asset}/><figcaption>{sf.label}</figcaption></figure>:<div className="slot-empty">{sf.label}</div>)}
            </Slot>
            <Slot onFiles={files=>dispatch({type:'uploadFiles',files,target:{kind:'shot',shot:shot.id,slot:'end'}})} id={'slot:end:'+shot.id} label="结尾画面" hint="可留空" onPick={openPicker('end')} onClear={shot.end ? () => upd({end: null}) : null}>
              {endA && <figure><StoryboardAssetImage asset={endA}/><figcaption>{endA.name}</figcaption></figure>}
            </Slot>
          </div>
          {index > 0 && <label className="check"><input type="checkbox" checked={shot.continuePrev}
            onChange={e => upd({continuePrev: e.target.checked, start: e.target.checked ? null : shot.start})} />
            接上一镜：用「{state.shots[index - 1].title}」选定版本的最后一帧作为开头</label>}
          <label className="field"><span>时长 {shot.duration} 秒</span><input type="number" min={durationRange?.minimum} max={durationRange?.maximum} step={durationRange?.step||'any'} value={shot.duration} disabled={!durationRange} onChange={event=>{const n=Number(event.target.value);if(Number.isFinite(n)&&n>0)upd({duration:n});}}/>{!durationRange&&<small>时长范围等待服务能力确认。</small>}</label>
        </>}
        {(info.mode==='REF'||info.controlInputs)&&<>
          <Slot onFiles={files=>dispatch({type:'uploadFiles',files,target:{kind:'shot',shot:shot.id,slot:'refs'}})} id={'slot:refs:'+shot.id} label={info.mode==='FL'?'控制视频 / 保留的声音':'附加素材（动作视频、额外的图或声音）'} hint={info.mode==='FL'?'在全部控制中明确视频和声音用途':'可留空 · 人物和场景已经在上面了'} onPick={openPicker('refs')}>
            {shot.refs.length > 0 && <div className="refs">{shot.refs.map(id => db.asset(id)).filter(Boolean).map(a => (
              <span key={a.id} className="ref"><AssetThumb asset={a} size={28} />{a.name}
                <button className="x" onClick={() => dispatch({type: 'unref', shot: shot.id, asset: a.id})}>×</button></span>))}</div>}
          </Slot>
          {info.limitsLabel&&<div className="limits">{info.limitsLabel}</div>}

        </>}
      </div>

      <div className="sent">
        <div className="sent-head"><b>实际发给模型</b><ModeChip info={info} /><span className="muted">{info.machine} · {info.res} · {info.sec} 秒</span></div>
        <div className="sent-prompt">{info.parts.map((x, i) => <div key={i}><em>{x.label}</em>{x.text}</div>)}</div>
        {info.items.length > 0 && <div className="sent-items">{info.items.map((x, i) => (
          <div key={i} className={'sent-item' + (x.sent ? '' : ' off')} title={x.reason}>
            <AssetThumb asset={x.a} size={40}/>
            <span><b>{x.kind}</b><small>{x.why}</small>{!x.sent && <small className="warn">未发送：{x.reason}</small>}</span></div>))}</div>}
        {info.warnings.map(w => <div key={w} className="warn">⚠ {w}</div>)}
        {info.notes.map(w => <div key={w} className="note">· {w}</div>)}
      </div>

      <div className="row">
        <div className="seg">{['draft', 'final'].map(q => (
          <button key={q} className={shot.quality === q ? 'on' : ''} onClick={() => upd({quality: q})}>{QUALITY_LABEL[q]}<small>{typeof state.models?.[q]==='string'?state.models[q]:state.models?.[q]?.label||'以预检模型为准'}</small></button>))}</div>
      </div>
      {shot.versions.length > 0 && <label className="check"><input type="checkbox" checked={shot.seedLock} onChange={e => upd({seedLock: e.target.checked})} />
        固定随机种子：只改提示词或素材，其余尽量和当前版本一致</label>}
      <div className="ed-actions">
        <button className="primary" disabled={busy} onClick={()=>dispatch({type:'generate',ids:[shot.id]})}>
          {shot.versions.length?'预检并重新生成':'预检并生成'} · {qualityLabel}</button>
        {chosen&&chosen.quality==='draft'&&<button disabled={busy} onClick={() => dispatch({type:'generate',ids:[shot.id],quality:'final',sourceVersion:chosen.id})}>用精修重做选定版本</button>}
        <button className="ghost danger" onClick={() => setDeleting(true)}>删除镜头</button>
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

export function Screen({player,big,style}){
  const {seg,t,playing}=player,ref=useRef(),playback=useRef(null),local=seg?t-seg.start:0;
  if(!playback.current)playback.current=createStoryboardPlayback();
  const sync=()=>playback.current.sync({video:ref.current,time:local,playing,onFailure:()=>player.setPlaying(false)});
  useEffect(sync,[seg?.version?.id,seg?.shot.id,playing,local]);
  useEffect(()=>()=>playback.current.reset(),[]);
  if(!seg)return <div className="screen" style={style}><div className="screen-empty">还没有镜头</div></div>;
  return <div className={'screen'+(big?' big':'')} style={style}>{seg.version?<StoryboardVideo key={seg.shot.id+':'+seg.version.id} asset={seg.version.asset||seg.version} videoRef={ref} onLoadedMetadata={sync} onCanPlay={sync}/>:<div className="screen-empty"><b>{seg.shot.title}</b><span>尚未选定视频 · 文字占位</span></div>}<div className="screen-cap">{seg.shot.title}<small>{seg.shot.script}</small></div></div>;
}

export function Transport({player}) {
  const {playing, setPlaying, t, total, setT} = player;
  return (
    <div className="transport">
      <button className="primary round" aria-label={playing?'暂停时间线':'播放时间线'} disabled={!total} onClick={() => setPlaying(!playing)}>{playing ? '❚❚' : '▶'}</button>
      <button className="ghost" onClick={() => { setPlaying(false); setT(0); }}>⏮</button>
      <span className="time">{fmt(t)} / {fmt(total)}</span>
    </div>
  );
}

function Segment({seg, selected, onPick}) {
  const {state} = useStore();
  const status = shotStatus(seg.shot, queuePositions(state.shots)[seg.shot.id]);
  return (
    <button type="button" aria-label={'打开镜头 '+seg.shot.title} style={{flexGrow: seg.dur, flexBasis: 0}}
      className={'tseg' + (selected ? ' selected' : '')} onClick={() => onPick(seg)} title={status.label}>
      {seg.version?<StoryboardAssetImage asset={seg.version.asset||seg.version}/>:<div className="tseg-empty"/>}
      <span className="tseg-title">{seg.shot.title}</span>
      <span className={'tseg-dot ' + status.tone} />
    </button>
  );
}

export function Timeline({player, selectedId, onPick}) {
  const {segs, total, t, setT} = player;
  const pick = seg => { setT(seg.start); onPick?.(seg.shot.id); };
  return (
    <div className="timeline">
      <div className="track video">
        {segs.map(seg => <Segment key={seg.shot.id} seg={seg} selected={seg.shot.id === selectedId} onPick={pick} />)}
        <div className="playhead" style={{left: `${total ? (t / total) * 100 : 0}%`}} />
      </div>
      <div className="track audio"><span>选定视频的原声 · 章节混音仍在原交付工作台</span></div>
      <div className="ticks">{segs.map(s => <span key={s.shot.id} style={{flexGrow: s.dur, flexBasis: 0}}>{fmt(s.start)}</span>)}</div>
    </div>
  );
}
