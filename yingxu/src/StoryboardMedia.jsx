import {useI18n} from './i18n/I18nProvider.jsx';
import React,{useEffect,useRef,useState} from 'react';
import {getFile,download} from './media.js';
import {artifactMedia} from './artifact-media.js';

export function StoryboardDownloads({version,title}){
 const {t}=useI18n();
  const video=version.asset?.entity,audio=version.audio,items=[video?{id:version.artifactId,kind:'video',mime:video.data.mime,content_url:video.data.cloudContentPath}:null,audio].map(artifact=>artifactMedia(artifact,{id:version.jobId},{title})).filter(Boolean);
  return <span className="row tight">{items.map(media=><StoryboardDownload key={media.id} media={media}/>)}</span>;
}
function StoryboardDownload({media}){
 const {t}=useI18n();
  const [loading,setLoading]=useState(false),[error,setError]=useState(''),pending=useRef(false),alive=useRef(true);
  useEffect(()=>{alive.current=true;return()=>{alive.current=false;};},[]);
  const save=async()=>{
    if(pending.current)return;pending.current=true;setLoading(true);setError('');
    try{
      const blob=await getFile('cloud_artifact_'+media.id);
      if(!alive.current)return;
      if(!blob?.size)throw Error(t('legacy.visible35'));
      download(blob,media.filename.replace('粗剪','镜头'));
    }catch(error){if(alive.current)setError(t('legacy.downloadError',{message:error.message}));}
    finally{pending.current=false;if(alive.current)setLoading(false);}
  };
  return <span><button type="button" disabled={loading} onClick={save}>{loading?t('legacy.visible36'):media.type==='video'?t('legacy.visible37'):t('legacy.visible38')}</button>{error&&<small role="alert">{error}</small>}</span>;
}

// Reuse the existing owner-scoped media reader; Blob URLs live only as long as
// their mounted preview. No hardcoded demo clips, public buckets or provider URLs.
export function useStoryboardMediaUrl(asset,{thumbnail=false}={}){
 const {t}=useI18n();
  const [value,setValue]=useState({url:'',error:''});
  const direct=thumbnail?(asset?.thumb||asset?.src):(asset?.src||asset?.thumb),fileId=asset?.fileId||asset?.entity?.data?.fileId;
  useEffect(()=>{let alive=true,owned='';setValue({url:'',error:''});
    if(direct){setValue({url:direct,error:''});return;}
    if(!fileId)return;
    getFile(fileId).then(blob=>{if(!alive)return;if(!blob){setValue({url:'',error:t('legacy.visible39')});return;}owned=URL.createObjectURL(blob);setValue({url:owned,error:''});}).catch(()=>{if(alive)setValue({url:'',error:t('legacy.visible40')});});
    return()=>{alive=false;if(owned)URL.revokeObjectURL(owned);};
  },[direct,fileId]);return value;
}
export function StoryboardAssetImage({asset,className='',style,alt=''}){
 const {t}=useI18n();
  const {url,error}=useStoryboardMediaUrl(asset,{thumbnail:true});
  if(!url)return <span className={className+' media-empty'} style={style} title={error||t('legacy.visible41')}>{error?'!':'◇'}</span>;
  return asset?.kind==='video'&&!asset?.thumb?<video className={className} style={style} src={url} muted preload="metadata" playsInline aria-label={asset.name||t('legacy.visible42')}/>:<img className={className} style={style} src={url} alt={alt} draggable={false}/>;
}
export function StoryboardVideo({asset,className='',onTimeUpdate,onLoadedMetadata,videoRef,...props}){
 const {t}=useI18n();
  const {url,error}=useStoryboardMediaUrl(asset);
  return url?<video ref={videoRef} className={className} src={url} playsInline preload="metadata" onTimeUpdate={onTimeUpdate} onLoadedMetadata={onLoadedMetadata} {...props}/>:<div className="media-empty">{error||t('legacy.visible43')}</div>;
}
let currentAudio=null;
export function StoryboardPlayButton({asset,start=0,len=null,label}){
 const {t}=useI18n();
  const {url,error}=useStoryboardMediaUrl(asset),[playing,setPlaying]=useState(false),own=useRef(null);
  useEffect(()=>()=>{if(own.current){own.current.pause();if(currentAudio?.audio===own.current)currentAudio=null;}},[url]);
  const toggle=event=>{event.stopPropagation();if(currentAudio){const same=currentAudio.audio===own.current;currentAudio.audio.pause();currentAudio.stop();currentAudio=null;if(same)return;}
    if(!url)return;const audio=new Audio(url);own.current=audio;const stop=()=>{audio.pause();setPlaying(false);if(currentAudio?.audio===audio)currentAudio=null;};
    audio.addEventListener('loadedmetadata',()=>{audio.currentTime=Math.max(0,start);audio.play().then(()=>setPlaying(true)).catch(stop);},{once:true});
    audio.addEventListener('ended',stop);audio.addEventListener('error',stop);if(Number.isFinite(len))audio.addEventListener('timeupdate',()=>audio.currentTime>=start+len&&stop());currentAudio={audio,stop};};
  return <button type="button" className={'play'+(playing?' on':'')} disabled={!url} onPointerDown={event=>event.stopPropagation()} onClick={toggle} title={error||label||t('legacy.visible44')}>{playing?'■':'▶'}{label&&' '+label}</button>;
}
