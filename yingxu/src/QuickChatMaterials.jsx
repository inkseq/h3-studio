import React,{useEffect,useMemo,useRef,useState} from 'react';
import {Image,Video,Music,X,Check,Upload} from 'lucide-react';
import {ChatDialog} from './QuickChatControls.jsx';
import {bindingParticipates,bindingPayload,effectiveLimits,clipLimits,displaySeconds,mediaDuration,hasOriginalAudio,roleSupported} from './quick-chat-model.js';
export function PrivateMedia({client,path,kind,name='',compact=false}){
  const [url,setUrl]=useState(''),[error,setError]=useState(''),player=useRef(null);
  // Freeze the originating account with this media source. A later cookie
  // change must reject this URL, not rebind an old source during a poll render.
  const native=useMemo(()=>{if(!path||compact||!['video','audio'].includes(kind))return null;try{return {url:client.nativeContentURL(path)};}catch(error){return {error:error.message};}},[client,path,kind,compact]);
  useEffect(()=>{let active=true,created='';setUrl('');setError('');if(!path||kind!=='image')return;const aborter=new AbortController();client.download(path,{signal:aborter.signal}).then(blob=>{if(!active)return;created=URL.createObjectURL(blob);setUrl(created);}).catch(()=>{if(active)setError('预览未载入，可刷新或下载原文件。');});return()=>{active=false;aborter.abort();if(created)URL.revokeObjectURL(created);};},[client,path,compact,kind]);
  if(compact&&kind!=='image'){const Icon=kind==='audio'?Music:Video;return <div className="qc-media-placeholder qc-compact" aria-label={kind==='audio'?'音频素材':'视频素材'}><Icon size={22}/></div>;}
  const source=kind==='image'?url:native?.url,issue=native?.error||error;
  if(!source){const Icon={image:Image,video:Video,audio:Music}[kind]||Image;return <div className={'qc-media-placeholder '+(compact?'qc-compact':'')} title={issue||'正在读取私有素材'}><Icon size={22}/>{!compact&&<small>{issue||'正在载入预览…'}</small>}</div>;}
  if(kind==='image')return <img className={compact?'qc-media-thumb':'qc-media-full'} src={source} alt={name}/>;
  const failed=()=>setError('媒体未能载入，请核对登录账户或重新读取原文件。');
  return <>{kind==='video'?<video ref={player} className="qc-media-full" src={source} controls preload="metadata" onError={failed}/>:<audio ref={player} src={source} controls preload="metadata" onError={failed}/>} {issue&&<p className="qc-error" role="alert">{issue}<button onClick={()=>{setError('');player.current?.load();}}>重新读取</button></p>}</>;
}
export function MaterialClip({binding,limits,client,onSave,onClose}){
  const clip=clipLimits(limits,binding.kind),source=mediaDuration(binding.asset),[start,setStart]=useState(binding.source_range?.start??0),[end,setEnd]=useState(binding.source_range?.end??source??0),[error,setError]=useState('');
  return <ChatDialog title="选择本轮使用的片段" onClose={onClose} className="qc-controls-dialog"><p>{binding.asset?.file_name||'素材'} · 原片 {Number.isFinite(source)?`${source.toFixed(2)} 秒`:'时长尚未核验'}</p>{client&&binding.asset?.content_url&&<PrivateMedia client={client} path={binding.asset.content_url} kind={binding.kind} name={binding.asset.file_name}/>}<div className="qc-form-grid"><label className="qc-field">开始时间（秒）<input type="number" min="0" step="0.01" value={start} onChange={e=>setStart(Number(e.target.value))}/></label><label className="qc-field">结束时间（秒）<input type="number" min="0" step="0.01" value={end} onChange={e=>setEnd(Number(e.target.value))}/></label></div><p className="qc-muted">单段 {displaySeconds(clip.minimum)}–{displaySeconds(clip.maximum)} 秒，同类累计上限以当前执行范围为准。保留原件；预检时后台创建合法选段。</p>{error&&<p className="qc-error" role="alert">{error}</p>}<div className="qc-dialog-footer"><button onClick={onClose}>取消</button><button className="qc-primary" onClick={async()=>{if(!Number.isFinite(start)||!Number.isFinite(end)||start<0||!Number.isFinite(source)||end>source||end-start<(clip.minimum??2)||end-start>(clip.maximum??15)){setError('请输入源文件范围内的合法片段。');return;}try{await onSave({start,end});onClose();}catch(err){setError(err.message);}}}>使用这个片段</button></div></ChatDialog>;
}
function AnchorTime({binding,busy,onSave}){
  const [value,setValue]=useState(String(binding.time_seconds??0)),[error,setError]=useState(''),focused=useRef(false),startedVersion=useRef(binding.version);
  useEffect(()=>{if(!focused.current){setValue(String(binding.time_seconds??0));setError('');}},[binding.time_seconds]);
  async function save(){focused.current=false;const next=Number(value);if(value.trim()===''||!Number.isFinite(next)||next<0){setError('请输入非负秒数。');return;}if(next===binding.time_seconds)return;if(startedVersion.current!==binding.version){setError('材料已被更新。输入仍保留，请核对后再次编辑。');return;}try{await onSave(next);setError('');}catch(err){setError(err.message||'时刻尚未保存，输入仍保留。');}}
  return <><label className="qc-anchor-label">锚点时刻<input type="number" min="0" step="0.01" value={value} disabled={busy} onFocus={()=>{focused.current=true;startedVersion.current=binding.version;}} onChange={e=>{setValue(e.target.value);setError('');}} onBlur={save} onKeyDown={e=>{if(e.key==='Enter'){e.preventDefault();e.currentTarget.blur();}}}/><span>秒</span></label>{error&&<small className="qc-error" role="alert">{error}</small>}</>;
}
export default function QuickChatMaterials({bindings,recipe,client,busy,onChange,onUpload,onRemove,onResume,onRoleChange,compact=false,showDisabled=false,availableModes}){
  const limits=effectiveLimits(recipe),[clip,setClip]=useState(null),[error,setError]=useState('');
  const roleUnavailable=(slot,kind)=>{if(roleSupported(recipe,slot,kind))return false;const mode=['first_frame','last_frame'].includes(slot)?'fl':['images','videos','audios'].includes(slot)?'ref':null;return mode&&mode!==recipe?.mode&&availableModes?.[mode]===false;};
  const visible=bindings.filter(binding=>showDisabled||binding.enabled!==false||binding.version===0);
  function change(binding,patch){return onChange(bindings.map(item=>item.binding_id===binding.binding_id?{...bindingPayload(item),...patch}:bindingPayload(item)));}
  async function act(fn){setError('');try{await fn();}catch(err){setError(err.message||'素材修改尚未保存。');}}
  function role(binding,slot){
    const first=['first_frame','last_frame'].includes(slot);
    if(first&&bindings.some(other=>other.binding_id!==binding.binding_id&&other.enabled!==false&&other.slot===slot)){setError('同一个首帧或尾帧只能选择一张，请先调整原素材。');return;}
    const patch={slot,enabled:true,purpose:first?{first_frame:'firstFrame',last_frame:'lastFrame'}[slot]:{images:'reference',videos:'motion',audios:'audio',guides:'reference'}[slot],...(slot==='guides'?{time_seconds:binding.time_seconds??0,use_audio:binding.kind==='video'&&hasOriginalAudio(binding.asset)&&limits.allow_video_audio}:{})};
    act(()=>onRoleChange?onRoleChange(binding,patch):change(binding,patch));
  }
  return <><div className={'qc-materials '+(compact?'qc-materials-compact':'')}>{visible.map(binding=>{
    const asset=binding.asset||{},ready=!asset.status||asset.status==='ready',hasAudio=hasOriginalAudio(asset),active=ready&&bindingParticipates(binding,recipe),slots=binding.kind==='image'?['images','first_frame','last_frame','guides']:binding.kind==='video'?['videos','guides']:['audios','guides'];
    return <article className={'qc-material '+(!active?'qc-material-inactive':'')} key={binding.binding_id}>
      {ready?<PrivateMedia client={client} path={asset.content_url} kind={binding.kind} name={asset.file_name} compact/>:<div className="qc-media-placeholder qc-compact"><Upload size={20}/></div>}
      <div className="qc-material-info"><b title={asset.file_name}>{asset.file_name||'已上传素材'}</b>
        {!ready&&<small className="qc-warning">{({processing:'原文件正在校验',uploading:'原上传收据等待核对',failed:'素材校验失败',unknown:'原上传结果待核对'})[asset.status]||'素材尚未完成校验'}{asset.error_code?` · ${asset.error_code}`:''}</small>}
        {!ready&&onResume&&<button disabled={busy} onClick={()=>act(()=>onResume(binding.asset_id))}>核对并恢复原素材</button>}
        <select aria-label={`${asset.file_name||'素材'}的用途`} disabled={busy||!ready} value={binding.slot} onChange={e=>role(binding,e.target.value)}>{slots.map(slot=><option key={slot} value={slot} disabled={roleUnavailable(slot,binding.kind)||slot==='guides'&&(!limits.guide_kinds.includes(binding.kind)||limits.guide_recipe_ids&&!limits.guide_recipe_ids.includes(recipe?.id))||['first_frame','last_frame'].includes(slot)&&bindings.some(other=>other.binding_id!==binding.binding_id&&other.enabled!==false&&other.slot===slot)}>{({images:'参考图片',videos:'视频 / 动作参考',audios:'声音参考',first_frame:'首帧',last_frame:'尾帧',guides:'时间锚点'})[slot]}</option>)}</select>
        {binding.kind!=='image'&&<button disabled={busy||!ready} className="qc-clip-button" onClick={()=>setClip(binding)}>片段 {Number(binding.source_range?binding.source_range.end-binding.source_range.start:mediaDuration(asset)||0).toFixed(2)} 秒 · 调整</button>}
        {binding.kind==='video'&&<label className="qc-small-check"><input type="checkbox" checked={hasAudio&&(binding.slot==='guides'?binding.use_audio===true:binding.include_audio!==false)} disabled={busy||!ready||!hasAudio||!limits.allow_video_audio} onChange={e=>act(()=>change(binding,{[binding.slot==='guides'?'use_audio':'include_audio']:e.target.checked}))}/>{hasAudio?'使用原声':'原文件无音轨'}</label>}
        {binding.slot==='guides'&&<AnchorTime binding={binding} busy={busy||!ready} onSave={time_seconds=>change(binding,{time_seconds})}/>}
        {!active&&<small>保留 · 本轮不使用</small>}
        {binding.enabled===false&&ready&&<button disabled={busy} className="qc-clip-button" onClick={()=>role(binding,binding.slot)}>加入本轮</button>}
        {(showDisabled||!['first_frame','last_frame','guides'].includes(binding.slot))&&<details className="qc-material-options"><summary>更多用途</summary>{!['first_frame','last_frame','guides'].includes(binding.slot)&&<select aria-label="参考细分用途" value={binding.purpose} disabled={busy||!ready} onChange={e=>act(()=>change(binding,{purpose:e.target.value}))}>{(binding.kind==='image'?['reference','identity']:binding.kind==='video'?['motion','reference']:['audio','reference']).map(role=><option key={role} value={role}>{({reference:'通用参考',identity:'人物身份',motion:'动作参考',audio:'声音参考'})[role]}</option>)}</select>}{showDisabled&&<label className="qc-small-check"><input type="checkbox" checked={binding.enabled!==false} disabled={busy||!ready} onChange={e=>act(()=>change(binding,{enabled:e.target.checked}))}/>本卡使用</label>}</details>}
      </div><button disabled={busy||(!ready&&!onRemove)} className="qc-material-remove" aria-label={`移除本轮引用 ${asset.file_name||''}`} onClick={()=>act(()=>onRemove?onRemove(binding):change(binding,{enabled:false}))}><X size={14}/></button>
    </article>;
  })}</div>{error&&<p className="qc-error" role="alert">{error}</p>}{onUpload&&<button className="qc-add-material" onClick={onUpload} disabled={busy}><Upload size={14}/>添加图片、视频或音频</button>}{clip&&<MaterialClip binding={clip} client={client} limits={limits} onClose={()=>setClip(null)} onSave={range=>change(clip,{source_range:range})}/>}</>;
}
