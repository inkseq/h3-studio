import {useI18n} from './i18n/I18nProvider.jsx';
import React,{useEffect,useMemo,useRef,useState} from 'react';
import {getFile,nativeMediaSource} from './media.js';
import {artifactMedia} from './artifact-media.js';

export function StoryboardDownloads({version,title}){
 const {t}=useI18n();
  const video=version.asset?.entity,audio=version.audio,items=[video?{id:version.artifactId,kind:'video',mime:video.data.mime,content_url:video.data.cloudContentPath}:null,audio].map(artifact=>artifactMedia(artifact,{id:version.jobId},{title})).filter(Boolean);
  return <span className="row tight">{items.map(media=><StoryboardDownload key={media.id} media={media}/>)}</span>;
}
function StoryboardDownload({media}){
 const {t}=useI18n();
  const [error,setError]=useState('');
  const message=error=>t(error?.message?.startsWith('client.')?error.message:'client.accountChanged');
  const source=useMemo(()=>{try{return {target:nativeMediaSource('cloud_artifact_'+media.id,{download:true})};}catch(error){return {error};}},[media.id]);
  let issue=source.error;try{source.target?.assertCurrent();}catch(error){issue=error;}
  const follow=event=>{try{if(source.error)throw source.error;source.target.assertCurrent();setError('');}catch(error){event.preventDefault();setError(message(error));}};
  return <span><a className="btn" href={issue?undefined:source.target?.href} download={media.filename.replace('粗剪','镜头')} target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer" aria-disabled={!!issue} tabIndex={issue?-1:undefined} onClick={follow}>{media.type==='video'?t('legacy.visible37'):t('legacy.visible38')}</a>{(error||issue)&&<small role="alert">{t('legacy.downloadError',{message:error||message(issue)})}</small>}</span>;
}

// Cloud views stream through the existing owner-fenced native route. Local
// Blob URLs still live only as long as their mounted preview.
export function useStoryboardMediaUrl(asset,{thumbnail=false}={}){
 const {t}=useI18n();
  const [value,setValue]=useState({url:'',error:''}),[failure,setFailure]=useState(null);
  const direct=thumbnail?(asset?.thumb||asset?.src):(asset?.src||asset?.thumb),fileId=asset?.fileId||asset?.entity?.data?.fileId;
  const native=useMemo(()=>{try{const target=nativeMediaSource(fileId);return target?{target}:null;}catch(error){return {error};}},[fileId]);
  useEffect(()=>{let alive=true,owned='';setValue({url:'',error:''});
    if(native)return()=>{alive=false;};
    if(direct){setValue({url:direct,error:''});return;}
    if(!fileId)return;
    getFile(fileId).then(blob=>{if(!alive)return;if(!blob){setValue({url:'',error:t('legacy.visible39')});return;}owned=URL.createObjectURL(blob);setValue({url:owned,error:''});}).catch(()=>{if(alive)setValue({url:'',error:t('legacy.visible40')});});
    return()=>{alive=false;if(owned)URL.revokeObjectURL(owned);};
  },[direct,fileId,native]);
  const onError=()=>{if(native)setFailure({source:native,message:t('legacy.visible40')});else setValue({url:'',error:t('legacy.visible40')});};
  if(native){let error=native.error?t(native.error.message?.startsWith('client.')?native.error.message:'client.accountChanged'):'';try{native.target?.assertCurrent();}catch{error=t('client.accountChanged');}
    if(failure?.source===native)error=failure.message;
    return {url:error?'':native.target?.href||'',error,onError,assertCurrent:()=>native.target?.assertCurrent()};}
  return {...value,onError,assertCurrent:()=>{}};
}
export function StoryboardAssetImage({asset,className='',style,alt=''}){
 const {t}=useI18n();
  const {url,error,onError}=useStoryboardMediaUrl(asset,{thumbnail:true});
  if(!url)return <span className={className+' media-empty'} style={style} role={error?'alert':undefined} title={error||t('legacy.visible41')}>{error?t('library.previewUnavailable'):'◇'}</span>;
  return asset?.kind==='video'&&!asset?.thumb?<video className={className} style={style} src={url} muted preload="metadata" playsInline aria-label={asset.name||t('legacy.visible42')} onError={onError}/>:<img className={className} style={style} src={url} alt={alt} draggable={false} onError={onError}/>;
}
export function StoryboardVideo({asset,className='',onTimeUpdate,onLoadedMetadata,videoRef,...props}){
 const {t}=useI18n();
  const {url,error,onError}=useStoryboardMediaUrl(asset);
  return url?<video ref={videoRef} className={className} src={url} playsInline preload="metadata" onTimeUpdate={onTimeUpdate} onLoadedMetadata={onLoadedMetadata} {...props} onError={event=>{onError();props.onError?.(event);}}/>:<div className="media-empty" role={error?'alert':undefined}>{error||t('legacy.visible43')}</div>;
}
let currentAudio=null;
export function StoryboardPlayButton({asset,start=0,len=null,label}){
 const {t}=useI18n();
  const {url,error,onError,assertCurrent}=useStoryboardMediaUrl(asset),[playing,setPlaying]=useState(false),own=useRef(null);
  useEffect(()=>()=>{if(own.current){own.current.pause();if(currentAudio?.audio===own.current)currentAudio=null;}},[url]);
  const toggle=event=>{event.stopPropagation();if(currentAudio){const same=currentAudio.audio===own.current;currentAudio.audio.pause();currentAudio.stop();currentAudio=null;if(same)return;}
    if(!url)return;try{assertCurrent();}catch{onError();return;}const audio=new Audio(url);own.current=audio;const stop=()=>{audio.pause();setPlaying(false);if(currentAudio?.audio===audio)currentAudio=null;};
    audio.addEventListener('loadedmetadata',()=>{try{assertCurrent();audio.currentTime=Math.max(0,start);audio.play().then(()=>setPlaying(true)).catch(()=>{stop();onError();});}catch{stop();onError();}},{once:true});
    audio.addEventListener('ended',stop);audio.addEventListener('error',()=>{stop();onError();});if(Number.isFinite(len))audio.addEventListener('timeupdate',()=>audio.currentTime>=start+len&&stop());currentAudio={audio,stop};};
  return <button type="button" className={'play'+(playing?' on':'')} disabled={!url} onPointerDown={event=>event.stopPropagation()} onClick={toggle} title={error||label||t('legacy.visible44')}>{playing?'■':'▶'}{label&&' '+label}</button>;
}
