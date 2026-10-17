import React from 'react';
import {generatedTrackState,unlinkGeneratedSound} from './generated-sound-model.js';
import SelectedVideoCuts from './SelectedVideoCuts.jsx';
import {videoCut} from './video-cut-model.js';
import {store} from './store.js';
import {useLocalMedia,LocalRangePreview,RangeFields} from './media-controls.jsx';
import {validateRange,chapterTiming,trackPlacement,trackAtTime,secondsLabel} from './media-timing.js';
import {storyboardUrl} from './storyboard.js';
import './reference-editor.css';
import './sound-studio.css';

const roleNames={dialogue:'对白',music:'音乐',ambience:'环境音',generated:'同次生成声音'};
const show=value=>Number.isFinite(value)?Number(value.toFixed(4)):'未知';
function GeneratedTrackForm({track,asset,onSave,onCancel}){
  const [gain,setGain]=React.useState(String(track.gain??.7)),[error,setError]=React.useState('');
  return <form className="sound-track-form" onSubmit={e=>{e.preventDefault();const value=Number(gain);if(gain===''||!Number.isFinite(value)||value<0||value>1){setError('音量应为0到1。');return;}const {generatedIssue,...saved}=track;onSave({...saved,gain:value});}}><p>声音跟随采用视频，来源 {show(track.start)}–{show(track.end)} 秒。要单独改起点、范围或文件，请先解除关联。</p><label>生成音轨音量（0–1）<input aria-label="生成音轨音量" type="number" min="0" max="1" step="0.05" value={gain} onChange={e=>setGain(e.target.value)}/></label>{asset&&!track.generatedIssue&&<LocalRangePreview entity={asset} start={track.start} end={track.end} duration={track.duration}/>}<div className="media-control-row"><button type="submit">保存生成音轨音量</button><button type="button" onClick={onCancel}>取消</button></div>{error&&<p role="alert">{error}</p>}</form>;
}
function TrackForm({track,assets,timeline,onSave,onCancel}){
  const [assetId,setAssetId]=React.useState(track?.assetId||''),[role,setRole]=React.useState(track?.role||'dialogue'),[shotId,setShotId]=React.useState(track?.shotId||''),[offset,setOffset]=React.useState(String(track?.offset||0)),[start,setStart]=React.useState(String(track?.start||0)),[end,setEnd]=React.useState(track?.end===undefined?'':String(track.end)),[gain,setGain]=React.useState(String(track?.gain??.7)),[muted,setMuted]=React.useState(!!track?.muted),[duration,setDuration]=React.useState(null),[error,setError]=React.useState('');
  const asset=assets.find(a=>a.id===assetId),total=timeline.at(-1)?.end||0;
  const metadata=d=>{setDuration(d);setEnd(v=>v===''?String(Number(d.toFixed(3))):v);};
  const submit=e=>{e.preventDefault();if(!asset){setError('请选择一份音频。');return;}const invalid=validateRange(start,end,duration);if(invalid){setError(invalid);return;}if(offset===''||!Number.isFinite(Number(offset))||Number(offset)<0){setError('开始偏移必须是 0 或正数。');return;}if(gain===''||!Number.isFinite(Number(gain))||Number(gain)<0||Number(gain)>1){setError('音量应为 0 到 1。');return;}
    const next={id:track?.id||`sound-${crypto.randomUUID()}`,assetId,role,shotId,offset:Number(offset),start:Number(start),end:Number(end),gain:Number(gain),muted,duration,fileId:asset.data.fileId},placement=trackPlacement(next,timeline),shot=timeline.find(s=>s.id===shotId);
    if(!placement){setError('原先选定的镜头已被移除，请重新选择声音起点。');return;}if(shot&&Number(offset)>=shot.end-shot.start){setError('偏移超出了所选镜头，请改小偏移或选择后面的镜头。');return;}if(placement.start>=total){setError('声音起点超出了章节时长，请提前起点或先增加镜头。');return;}
    if(onSave(next)!==false)setError('');
  };
  return <form noValidate className="sound-track-form" onSubmit={submit}><div className="sound-fields"><label>音轨用途<select aria-label="音轨用途" value={role} onChange={e=>setRole(e.target.value)}>{Object.entries(roleNames).filter(([key])=>key!=='generated').map(([key,label])=><option key={key} value={key}>{label}</option>)}</select></label><label>音频素材<select aria-label="音频素材" value={assetId} onChange={e=>{setAssetId(e.target.value);setStart('0');setEnd('');setDuration(null);setError('');}}><option value="">选择已上传的音频</option>{assets.map(a=><option key={a.id} value={a.id}>{a.title}</option>)}</select></label></div>
    {asset&&<><LocalRangePreview key={asset.data.fileId||asset.id} entity={asset} start={start} end={end} duration={duration} onDuration={metadata}/><RangeFields start={start} end={end} setStart={setStart} setEnd={setEnd} duration={duration} prefix="声音来源"/></>}
    <div className="sound-fields"><label>放到哪里<select aria-label="声音放到哪里" value={shotId} onChange={e=>setShotId(e.target.value)}><option value="">章节开头</option>{track?.shotId&&!timeline.some(s=>s.id===track.shotId)&&<option value={track.shotId}>原镜头已被移除 · 请重选</option>}{timeline.map(s=><option key={s.id} value={s.id}>{s.title} · {secondsLabel(s.start)}</option>)}</select></label><label>从这个起点延后（秒）<input aria-label="声音开始偏移（秒）" type="number" min="0" step="0.1" value={offset} onChange={e=>setOffset(e.target.value)}/></label><label>音量（0–1）<input aria-label="音轨音量" type="number" min="0" max="1" step="0.05" value={gain} onChange={e=>setGain(e.target.value)}/></label><label className="sound-check"><input type="checkbox" checked={muted} onChange={e=>setMuted(e.target.checked)}/>此音轨静音</label></div>
    <p className="media-control-hint">保留原文件，只播放所选片段。镜头排序变化时，声音跟随选定镜头移动；超过章节结尾的部分只在来源试听中播放。</p>
    {error&&<p role="alert" className="media-control-error">{error}</p>}<div className="media-control-row"><button type="submit" className="sound-primary">{track?'保存音轨修改':'添加这条音轨'}</button><button type="button" onClick={onCancel}>取消</button></div>
  </form>;
}

function TimelineAudio({track,entity,register,onProblem}){
  const {url,error}=useLocalMedia(entity),ref=React.useRef(null);
  React.useEffect(()=>{const el=ref.current;if(url&&el)register(track.id,el);return()=>{el?.pause();register(track.id,null);};},[url,track.id]);
  React.useEffect(()=>{if(error&&!track.muted)onProblem(`${entity?.title||'音频'}：${error}`);},[error,track.muted]);
  return url?<audio ref={ref} src={url} preload="auto" onError={()=>onProblem(`${entity?.title||'音频'}无法播放，请重新上传或将此轨静音。`)}/>:null;
}
function TimelineFrame({shot,project,time}){
  const asset=project.entities.find(e=>e.id===shot?.data.selectedAssetId),{url}=useLocalMedia(asset),ref=React.useRef(null),choice=shot?.data.uxReview?.chosen,cut=videoCut(shot,asset);
  React.useEffect(()=>{const el=ref.current;if(el&&Number.isFinite(el.duration))el.currentTime=Math.min(Math.max(0,(cut.sourceStart||0)+time),Math.max(0,el.duration-.01));},[time,url,cut.sourceStart]);
  if(asset?.type==='image'&&url)return <img src={url} alt={`当前镜头：${shot.title}`}/>;
  if(asset?.type==='video'&&shot?.data.selectedVideoRange&&cut.issue)return <div className="sound-storyboard-placeholder"><strong>{shot.title}</strong><p>{cut.issue}</p></div>;
  if(asset?.type==='video'&&url)return <video ref={ref} src={url} muted playsInline preload="auto" onLoadedMetadata={e=>{const el=e.currentTarget;el.currentTime=Math.min(Math.max(0,(cut.sourceStart||0)+time),Math.max(0,el.duration-.01));}}/>;
  if(choice)return <><img src={storyboardUrl(shot,choice)} alt={`示例候选 ${choice}`}/><span>交互示例图，非本次生成</span></>;
  return <div className="sound-storyboard-placeholder"><strong>{shot?.title||'还没有镜头'}</strong><p>{shot?.description||'添加镜头后可按时长预演。'}</p><span>文字分镜占位</span></div>;
}

export default function SoundStudio({project,chapterId,shots=[]}){
  const tracks=(project.journey?.soundTracks?.[chapterId]||[]).map(track=>{const state=generatedTrackState(project,track);return {...state.track,generatedIssue:state.issue};}),assets=project.entities.filter(e=>e.type==='audio'),timeline=chapterTiming(shots),total=timeline.at(-1)?.end||0,silent=project.journey?.sound?.mode==='silent';
  const [editing,setEditing]=React.useState(null),[adding,setAdding]=React.useState(false),[cursor,setCursor]=React.useState(0),[playing,setPlaying]=React.useState(false),[error,setError]=React.useState('');
  const audios=React.useRef(new Map()),frame=React.useRef(0),playRef=React.useRef(false),cursorRef=React.useRef(0),configRef=React.useRef({tracks,timeline,total,silent}),epoch=React.useRef(0),base=React.useRef(0),requests=React.useRef(new Set());configRef.current={tracks,timeline,total,silent};
  const pause=React.useCallback(()=>{playRef.current=false;setPlaying(false);cancelAnimationFrame(frame.current);audios.current.forEach(el=>el.pause());requests.current.clear();},[]);
  const move=React.useCallback(value=>{const next=Math.max(0,Math.min(configRef.current.total,value));cursorRef.current=next;setCursor(next);base.current=next;epoch.current=performance.now();},[]);
  React.useEffect(()=>{pause();move(0);setEditing(null);setAdding(false);setError('');return()=>{playRef.current=false;cancelAnimationFrame(frame.current);audios.current.forEach(el=>el.pause());};},[chapterId]);
  const trackKey=JSON.stringify(tracks),shotKey=JSON.stringify(timeline.map(s=>[s.id,s.start,s.end,s.shot.data.selectedAssetId,s.shot.data.selectedVideoRange]));
  React.useEffect(()=>{pause();setError('');},[trackKey,shotKey,silent]);
  React.useEffect(()=>{const stop=()=>pause();window.addEventListener('yingxu-stop-local-media',stop);return()=>{window.removeEventListener('yingxu-stop-local-media',stop);playRef.current=false;cancelAnimationFrame(frame.current);audios.current.forEach(el=>el.pause());};},[pause]);
  const register=React.useCallback((id,el)=>{if(el)audios.current.set(id,el);else audios.current.delete(id);},[]);
  const problem=React.useCallback(message=>{pause();setError(message);},[pause]);
  const sync=time=>{const {tracks:liveTracks,timeline:liveTimeline}=configRef.current;
    for(const track of liveTracks){const el=audios.current.get(track.id),sourceTime=configRef.current.silent||track.generatedIssue?null:trackAtTime(track,liveTimeline,time);if(!el)continue;if(sourceTime===null){el.pause();continue;}el.volume=track.gain??.7;if(Math.abs(el.currentTime-sourceTime)>.2)el.currentTime=sourceTime;if(el.paused&&!requests.current.has(track.id)){requests.current.add(track.id);el.play().then(()=>{requests.current.delete(track.id);if(!playRef.current)el.pause();}).catch(()=>{requests.current.delete(track.id);problem('浏览器未能开始同步试听。请再次点击播放，或检查 / 静音不能播放的音轨。');});}}
  };
  const tick=()=>{if(!playRef.current)return;const time=Math.min(configRef.current.total,base.current+(performance.now()-epoch.current)/1000);cursorRef.current=time;setCursor(time);sync(time);if(time>=configRef.current.total){pause();return;}frame.current=requestAnimationFrame(tick);};
  const toggle=()=>{if(playing){pause();return;}setError('');window.dispatchEvent(new Event('yingxu-stop-local-media'));for(const track of tracks.filter(t=>!silent&&!t.muted)){if(track.generatedIssue){setError(track.generatedIssue);return;}if(!Number.isFinite(track.gain??.7)||(track.gain??.7)<0||(track.gain??.7)>1){setError('音轨音量不合法，请编辑为 0 到 1。');return;}if(!trackPlacement(track,timeline)){setError('有音轨引用了已删除的镜头。请编辑起点或将该轨静音。');return;}const el=audios.current.get(track.id);if(!el||el.readyState<1){setError('有音轨尚未就绪或原文件缺失。稍等片刻，或检查 / 静音该轨后重试。');return;}const bad=validateRange(track.start,track.end,el.duration);if(bad){setError(`音轨选段需要调整：${bad}`);return;}}
    if(cursorRef.current>=total)move(0);base.current=cursorRef.current;epoch.current=performance.now();playRef.current=true;setPlaying(true);sync(cursorRef.current);frame.current=requestAnimationFrame(tick);
  };
  const changeTracks=mutator=>store.editProject(p=>{p.journey||={};p.journey.soundTracks||={};const current=p.journey.soundTracks[chapterId]||[];p.journey.soundTracks[chapterId]=mutator(current);});
  const save=next=>{const ok=changeTracks(current=>current.some(t=>t.id===next.id)?current.map(t=>t.id===next.id?next:t):[...current,next]);if(ok){setEditing(null);setAdding(false);setError('');}return ok;};
  const jump=time=>{move(time);if(playRef.current)sync(time);};
  const current=timeline.find(s=>cursor>=s.start&&cursor<s.end)||timeline.at(-1);
  if(!chapterId)return <p className="media-control-empty">先添加章节与镜头，再安排声音。</p>;
  return <><SelectedVideoCuts key={chapterId} project={project} shots={shots} onChange={pause}/><section className="sound-studio" aria-label="章节声音工作台"><div className="sound-heading"><div><h3>让镜头与声音一起走一遍</h3><p>上传对白、音乐或环境音，先听选段，再放到章节或镜头上。也可以直接做静音作品。</p></div><button type="button" disabled={!assets.length||!shots.length} onClick={()=>{pause();setAdding(true);setEditing(null);}}>添加声音轨道</button></div>
    {!assets.length&&<p className="media-control-empty">素材库里还没有音频。上传后可在这里选段和试听；静音作品无需添加。</p>}{silent&&!!tracks.length&&<p className="media-control-empty">当前声音方案是「静音先行」，章节试听不会播放这些音轨。音轨已保留，可在上方切换声音方案后试听。</p>}
    <div className="sound-track-list">{tracks.map(track=>{const asset=assets.find(a=>a.id===track.assetId),placement=trackPlacement(track,timeline);return <article key={track.id} className="sound-track-card" data-testid="sound-track-card"><div className="sound-track-title"><strong>{roleNames[track.role]||'声音'} · {asset?.title||'素材已被删除'}</strong><span>{track.muted?'已静音':`音量 ${Math.round((track.gain??.7)*100)}%`}</span></div><p>{placement?`章节 ${secondsLabel(placement.start)} 开始 · 来源 ${show(track.start)}–${show(track.end)} 秒`:'原镜头已被删除，请重新选择起点'}{placement?.end>total+1e-6&&' · 超出章节结尾，试听时会在章节结尾停止'}</p>{track.generatedFrom&&<p className={track.generatedIssue?'cloud-warning':'muted'}>{track.generatedIssue||'与采用视频关联：选段、镜头时长及顺序变化会一起跟随；浏览器试听仅供节奏核对。'}{track.muted&&' 当前已静音，不参与本次粗剪。'}</p>}{(!asset||!asset.data.fileId||asset.data.missingFile)&&<p className="media-control-error">原文件缺失，请重新上传后编辑音轨选择素材，或先静音。</p>}<div className="media-control-row"><button type="button" onClick={()=>{pause();setEditing(track.id);setAdding(false);}}>{track.generatedFrom?'调整生成音轨音量':'编辑这条音轨'}</button>{track.generatedFrom&&<button type="button" onClick={()=>{pause();if(store.editProject(p=>{unlinkGeneratedSound(p,chapterId,track.id);})){setEditing(null);store.notify('已解除视频声音关联，保留原片段为手动音轨。之后不会随视频选段变化，请独立核对来源与位置；可撤销。');}}}>解除关联，改为手动音轨</button>}<button type="button" onClick={()=>changeTracks(current=>current.map(t=>t.id===track.id?{...t,muted:!t.muted}:t))}>{track.muted?'取消静音':'静音此轨'}</button><button type="button" onClick={()=>changeTracks(current=>current.filter(t=>t.id!==track.id))}>移除音轨</button></div>{editing===track.id&&(track.generatedFrom?<GeneratedTrackForm key={`${track.id}-${trackKey}`} track={track} asset={asset} onSave={save} onCancel={()=>setEditing(null)}/>:<TrackForm key={`${track.id}-${trackKey}`} track={track} assets={assets} timeline={timeline} onSave={save} onCancel={()=>setEditing(null)}/>)}<TimelineAudio track={{...track,muted:track.muted||!!track.generatedIssue}} entity={asset} register={register} onProblem={problem}/></article>;})}</div>
    {adding&&<TrackForm assets={assets} timeline={timeline} onSave={save} onCancel={()=>setAdding(false)}/>}
    <div className="sound-preview"><div className="sound-frame"><TimelineFrame shot={current?.shot} project={project} time={cursor-(current?.start||0)}/></div><div className="sound-preview-details"><strong>{current?.title||'章节试听'}</strong><output aria-label="章节试听时间">{secondsLabel(cursor)} / {secondsLabel(total)}</output><input type="range" aria-label="章节试听进度" min="0" max={total||1} step="0.1" value={Math.min(cursor,total)} onChange={e=>jump(Number(e.target.value))} disabled={!total}/><div className="media-control-row"><button type="button" onClick={toggle} disabled={!total}>{playing?'暂停章节试听':'播放章节试听'}</button><button type="button" onClick={()=>{pause();move(0);}}>停止并回到开头</button></div><p className="media-control-hint">{!silent&&tracks.some(t=>!t.muted)?'同时试听已启用的声音轨道。':'当前静音：仅按镜头时长预演。'}这是本地分镜试听，不会导出合成视频；浏览器同步用于节奏核对，不替代精确混音。</p></div></div>
    {!!timeline.length&&<div className="sound-shot-jumps" aria-label="跳到镜头">{timeline.map(s=><button type="button" key={s.id} aria-pressed={current?.id===s.id} onClick={()=>jump(s.start)}>{s.title}<small>{secondsLabel(s.start)}</small></button>)}</div>}
    {error&&<p role="alert" className="media-control-error">{error}</p>}
  </section></>;
}
