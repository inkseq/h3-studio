import {useI18n,LanguageSwitcher} from './i18n/I18nProvider.jsx';
import './i18n/workspace-language.css';
import React,{useEffect,useMemo,useRef,useState} from 'react';
import {Image,Video,Music,X,Check,Upload} from 'lucide-react';
import {ChatDialog} from './QuickChatControls.jsx';
import {bindingParticipates,bindingPayload,effectiveLimits,clipLimits,displaySeconds,mediaDuration,hasOriginalAudio,roleSupported} from './quick-chat-model.js';
export function PrivateMedia({client,path,kind,name='',compact=false}){
  const {t:at}=useI18n();
  const {t,locale}=useI18n();
  const [error,setError]=useState(''),player=useRef(null);
  // Freeze the originating account with this media source. A later cookie
  // change must reject this URL, not rebind an old source during a poll render.
  const native=useMemo(()=>{if(!path||compact&&kind!=='image'||!['image','video','audio'].includes(kind))return null;try{return {url:client.nativeContentURL(path)};}catch(error){return {error:error.message};}},[client,path,kind,compact]);
  useEffect(()=>{setError('');},[client,path,compact,kind]);
  if(compact&&kind!=='image'){const Icon=kind==='audio'?Music:Video;return <div className="qc-media-placeholder qc-compact" aria-label={kind==='audio'?at('adjunct.quickChatMaterials.copy217'):at('adjunct.quickChatMaterials.copy218')}><Icon size={22}/></div>;}
  const source=native?.url,issue=native?.error||error;
  if(!source){const Icon={image:Image,video:Video,audio:Music}[kind]||Image;return <div className={'qc-media-placeholder '+(compact?'qc-compact':'')} title={issue||at('adjunct.quickChatMaterials.copy219')}><Icon size={22}/>{!compact&&<small>{issue||at('adjunct.quickChatMaterials.copy220')}</small>}</div>;}
  const failed=()=>setError(at('adjunct.quickChatMaterials.copy221'));
  return <>{kind==='image'?<img ref={player} className={compact?'qc-media-thumb':'qc-media-full'} src={source} alt={name} referrerPolicy="no-referrer" onError={failed}/>:kind==='video'?<video ref={player} className="qc-media-full" src={source} controls preload="metadata" onError={failed}/>:<audio ref={player} src={source} controls preload="metadata" onError={failed}/>} {issue&&<p className="qc-error" role="alert">{issue}<button onClick={()=>{setError('');if(kind==='image'&&player.current)player.current.src=source;else player.current?.load();}}>{t('workspace.copy60')}</button></p>}</>;
}
export function MaterialClip({binding,limits,client,onSave,onClose}){
  const {t:at}=useI18n();
  const {t,locale}=useI18n();
  const clip=clipLimits(limits,binding.kind),source=mediaDuration(binding.asset),[start,setStart]=useState(binding.source_range?.start??0),[end,setEnd]=useState(binding.source_range?.end??source??0),[error,setError]=useState('');
  return <ChatDialog title={t('workspace.accessible12')} onClose={onClose} className="qc-controls-dialog"><p>{binding.asset?.file_name||at('adjunct.quickChatMaterials.copy222')}{at('adjunct.quickChatMaterials.copy223')}{Number.isFinite(source)?at('adjunct.quickChatMaterials.copy224',{v0:source.toFixed(2)}):at('adjunct.quickChatMaterials.copy225')}</p>{client&&binding.asset?.content_url&&<PrivateMedia client={client} path={binding.asset.content_url} kind={binding.kind} name={binding.asset.file_name}/>}<div className="qc-form-grid"><label className="qc-field">{t('workspace.copy61')}<input type="number" min="0" step="0.01" value={start} onChange={e=>setStart(Number(e.target.value))}/></label><label className="qc-field">{t('workspace.copy62')}<input type="number" min="0" step="0.01" value={end} onChange={e=>setEnd(Number(e.target.value))}/></label></div><p className="qc-muted">{at('adjunct.quickChatMaterials.copy226')}{displaySeconds(clip.minimum)}–{displaySeconds(clip.maximum)}{at('adjunct.quickChatMaterials.copy227')}</p>{error&&<p className="qc-error" role="alert">{error}</p>}<div className="qc-dialog-footer"><button onClick={onClose}>{t('workspace.copy42')}</button><button className="qc-primary" onClick={async()=>{if(!Number.isFinite(start)||!Number.isFinite(end)||start<0||!Number.isFinite(source)||end>source||end-start<(clip.minimum??2)||end-start>(clip.maximum??15)){setError(at('adjunct.quickChatMaterials.copy228'));return;}try{await onSave({start,end});onClose();}catch(err){setError(err.message);}}}>{t('workspace.copy63')}</button></div></ChatDialog>;
}
function AnchorTime({binding,busy,onSave}){
  const {t,locale}=useI18n();
  const [value,setValue]=useState(String(binding.time_seconds??0)),[error,setError]=useState(''),focused=useRef(false),startedVersion=useRef(binding.version);
  useEffect(()=>{if(!focused.current){setValue(String(binding.time_seconds??0));setError('');}},[binding.time_seconds]);
  async function save(){focused.current=false;const next=Number(value);if(value.trim()===''||!Number.isFinite(next)||next<0){setError(t('workspace.status69'));return;}if(next===binding.time_seconds)return;if(startedVersion.current!==binding.version){setError(t('workspace.status70'));return;}try{await onSave(next);setError('');}catch(err){setError(err.message||t('workspace.status71'));}}
  return <><label className="qc-anchor-label">{t('workspace.copy64')}<input type="number" min="0" step="0.01" value={value} disabled={busy} onFocus={()=>{focused.current=true;startedVersion.current=binding.version;}} onChange={e=>{setValue(e.target.value);setError('');}} onBlur={save} onKeyDown={e=>{if(e.key==='Enter'){e.preventDefault();e.currentTarget.blur();}}}/><span>{t('workspace.copy65')}</span></label>{error&&<small className="qc-error" role="alert">{error}</small>}</>;
}
export default function QuickChatMaterials({bindings,recipe,client,busy,onChange,onUpload,onRemove,onResume,onRoleChange,compact=false,showDisabled=false,availableModes}){
  const {t:at}=useI18n();
  const {t,locale}=useI18n();
  const limits=effectiveLimits(recipe),[clip,setClip]=useState(null),[error,setError]=useState('');
  const roleUnavailable=(slot,kind)=>{if(roleSupported(recipe,slot,kind))return false;const mode=['first_frame','last_frame'].includes(slot)?'fl':['images','videos','audios'].includes(slot)?'ref':null;return mode&&mode!==recipe?.mode&&availableModes?.[mode]===false;};
  const visible=bindings.filter(binding=>showDisabled||binding.enabled!==false||binding.version===0);
  function change(binding,patch){return onChange(bindings.map(item=>item.binding_id===binding.binding_id?{...bindingPayload(item),...patch}:bindingPayload(item)));}
  async function act(fn){setError('');try{await fn();}catch(err){setError(err.message||t('workspace.status72'));}}
  function role(binding,slot){
    const first=['first_frame','last_frame'].includes(slot);
    if(first&&bindings.some(other=>other.binding_id!==binding.binding_id&&other.enabled!==false&&other.slot===slot)){setError(at('adjunct.quickChatMaterials.copy229'));return;}
    const patch={slot,enabled:true,purpose:first?{first_frame:'firstFrame',last_frame:'lastFrame'}[slot]:{images:'reference',videos:'motion',audios:'audio',guides:'reference'}[slot],...(slot==='guides'?{time_seconds:binding.time_seconds??0,use_audio:binding.kind==='video'&&hasOriginalAudio(binding.asset)&&limits.allow_video_audio}:{})};
    act(()=>onRoleChange?onRoleChange(binding,patch):change(binding,patch));
  }
  return <><div className={'qc-materials '+(compact?'qc-materials-compact':'')}>{visible.map(binding=>{
    const asset=binding.asset||{},ready=!asset.status||asset.status==='ready',hasAudio=hasOriginalAudio(asset),active=ready&&bindingParticipates(binding,recipe),slots=binding.kind==='image'?['images','first_frame','last_frame','guides']:binding.kind==='video'?['videos','guides']:['audios','guides'];
    return <article className={'qc-material '+(!active?'qc-material-inactive':'')} key={binding.binding_id}>
      {ready?<PrivateMedia client={client} path={asset.content_url} kind={binding.kind} name={asset.file_name} compact/>:<div className="qc-media-placeholder qc-compact"><Upload size={20}/></div>}
      <div className="qc-material-info"><b title={asset.file_name}>{asset.file_name||at('adjunct.quickChatMaterials.copy230')}</b>
        {!ready&&<small className="qc-warning">{({processing:at('adjunct.quickChatMaterials.copy231'),uploading:at('adjunct.quickChatMaterials.copy232'),failed:at('adjunct.quickChatMaterials.copy233'),unknown:at('adjunct.quickChatMaterials.copy234')})[asset.status]||at('adjunct.quickChatMaterials.copy235')}{asset.error_code?` · ${asset.error_code}`:''}</small>}
        {!ready&&onResume&&<button disabled={busy} onClick={()=>act(()=>onResume(binding.asset_id))}>{t('workspace.copy66')}</button>}
        <select aria-label={at('adjunct.quickChatMaterials.copy236',{v0:asset.file_name||at('adjunct.quickChatMaterials.copy237')})} disabled={busy||!ready} value={binding.slot} onChange={e=>role(binding,e.target.value)}>{slots.map(slot=><option key={slot} value={slot} disabled={roleUnavailable(slot,binding.kind)||slot==='guides'&&(!limits.guide_kinds.includes(binding.kind)||limits.guide_recipe_ids&&!limits.guide_recipe_ids.includes(recipe?.id))||['first_frame','last_frame'].includes(slot)&&bindings.some(other=>other.binding_id!==binding.binding_id&&other.enabled!==false&&other.slot===slot)}>{({images:at('adjunct.quickChatMaterials.copy238'),videos:at('adjunct.quickChatMaterials.copy239'),audios:at('adjunct.quickChatMaterials.copy240'),first_frame:at('adjunct.quickChatMaterials.copy241'),last_frame:at('adjunct.quickChatMaterials.copy242'),guides:at('adjunct.quickChatMaterials.copy243')})[slot]}</option>)}</select>
        {binding.kind!=='image'&&<button disabled={busy||!ready} className="qc-clip-button" onClick={()=>setClip(binding)}>{at('adjunct.quickChatMaterials.copy244')}{Number(binding.source_range?binding.source_range.end-binding.source_range.start:mediaDuration(asset)||0).toFixed(2)}{at('adjunct.quickChatMaterials.copy245')}</button>}
        {binding.kind==='video'&&<label className="qc-small-check"><input type="checkbox" checked={hasAudio&&(binding.slot==='guides'?binding.use_audio===true:binding.include_audio!==false)} disabled={busy||!ready||!hasAudio||!limits.allow_video_audio} onChange={e=>act(()=>change(binding,{[binding.slot==='guides'?'use_audio':'include_audio']:e.target.checked}))}/>{hasAudio?at('adjunct.quickChatMaterials.copy246'):at('adjunct.quickChatMaterials.copy247')}</label>}
        {binding.slot==='guides'&&<AnchorTime binding={binding} busy={busy||!ready} onSave={time_seconds=>change(binding,{time_seconds})}/>}
        {!active&&<small>{t('workspace.copy67')}</small>}
        {binding.enabled===false&&ready&&<button disabled={busy} className="qc-clip-button" onClick={()=>role(binding,binding.slot)}>{t('workspace.copy68')}</button>}
        {(showDisabled||!['first_frame','last_frame','guides'].includes(binding.slot))&&<details className="qc-material-options"><summary>{t('workspace.copy69')}</summary>{!['first_frame','last_frame','guides'].includes(binding.slot)&&<select aria-label={t('workspace.accessible13')} value={binding.purpose} disabled={busy||!ready} onChange={e=>act(()=>change(binding,{purpose:e.target.value}))}>{(binding.kind==='image'?['reference','identity']:binding.kind==='video'?['motion','reference']:['audio','reference']).map(role=><option key={role} value={role}>{({reference:at('adjunct.quickChatMaterials.copy248'),identity:at('adjunct.quickChatMaterials.copy249'),motion:at('adjunct.quickChatMaterials.copy250'),audio:at('adjunct.quickChatMaterials.copy240')})[role]}</option>)}</select>}{showDisabled&&<label className="qc-small-check"><input type="checkbox" checked={binding.enabled!==false} disabled={busy||!ready} onChange={e=>act(()=>change(binding,{enabled:e.target.checked}))}/>{t('workspace.copy70')}</label>}</details>}
      </div><button disabled={busy||(!ready&&!onRemove)} className="qc-material-remove" aria-label={at('adjunct.quickChatMaterials.copy252',{v0:asset.file_name||''})} onClick={()=>act(()=>onRemove?onRemove(binding):change(binding,{enabled:false}))}><X size={14}/></button>
    </article>;
  })}</div>{error&&<p className="qc-error" role="alert">{error}</p>}{onUpload&&<button className="qc-add-material" onClick={onUpload} disabled={busy}><Upload size={14}/>{t('workspace.copy71')}</button>}{clip&&<MaterialClip binding={clip} client={client} limits={limits} onClose={()=>setClip(null)} onSave={range=>change(clip,{source_range:range})}/>}</>;
}
