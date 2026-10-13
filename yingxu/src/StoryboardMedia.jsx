import React,{useEffect,useRef,useState} from 'react';
import {getFile} from './media.js';
import {artifactMedia} from './artifact-media.js';

export function StoryboardDownloads({version,title}){
  const video=version.asset?.entity,audio=version.audio,items=[video?{id:version.artifactId,kind:'video',mime:video.data.mime,content_url:video.data.cloudContentPath}:null,audio].map(artifact=>artifactMedia(artifact,{id:version.jobId},{title})).filter(Boolean);
  return <span className="row tight">{items.map(media=><a key={media.id} href={media.downloadURL} download={media.filename.replace('粗剪','镜头')} referrerPolicy="no-referrer">{media.type==='video'?'下载视频':'下载独立音频'}</a>)}</span>;
}

// Reuse the existing owner-scoped media reader; Blob URLs live only as long as
// their mounted preview. No hardcoded demo clips, public buckets or provider URLs.
export function useStoryboardMediaUrl(asset,{thumbnail=false}={}){
  const [value,setValue]=useState({url:'',error:''});
  const direct=thumbnail?(asset?.thumb||asset?.src):(asset?.src||asset?.thumb),fileId=asset?.fileId||asset?.entity?.data?.fileId;
  useEffect(()=>{let alive=true,owned='';setValue({url:'',error:''});
    if(direct){setValue({url:direct,error:''});return;}
    if(!fileId)return;
    getFile(fileId).then(blob=>{if(!alive)return;if(!blob){setValue({url:'',error:'素材文件暂不可读取'});return;}owned=URL.createObjectURL(blob);setValue({url:owned,error:''});}).catch(()=>{if(alive)setValue({url:'',error:'素材读取失败，请检查连接'});});
    return()=>{alive=false;if(owned)URL.revokeObjectURL(owned);};
  },[direct,fileId]);return value;
}
export function StoryboardAssetImage({asset,className='',style,alt=''}){
  const {url,error}=useStoryboardMediaUrl(asset,{thumbnail:true});
  if(!url)return <span className={className+' media-empty'} style={style} title={error||'尚未读取素材'}>{error?'!':'◇'}</span>;
  return asset?.kind==='video'&&!asset?.thumb?<video className={className} style={style} src={url} muted preload="metadata" playsInline aria-label={asset.name||'视频预览'}/>:<img className={className} style={style} src={url} alt={alt} draggable={false}/>;
}
export function StoryboardVideo({asset,className='',onTimeUpdate,onLoadedMetadata,videoRef,...props}){
  const {url,error}=useStoryboardMediaUrl(asset);
  return url?<video ref={videoRef} className={className} src={url} playsInline preload="metadata" onTimeUpdate={onTimeUpdate} onLoadedMetadata={onLoadedMetadata} {...props}/>:<div className="media-empty">{error||'正在读取已保存视频…'}</div>;
}
let currentAudio=null;
export function StoryboardPlayButton({asset,start=0,len=null,label}){
  const {url,error}=useStoryboardMediaUrl(asset),[playing,setPlaying]=useState(false),own=useRef(null);
  useEffect(()=>()=>{if(own.current){own.current.pause();if(currentAudio?.audio===own.current)currentAudio=null;}},[url]);
  const toggle=event=>{event.stopPropagation();if(currentAudio){const same=currentAudio.audio===own.current;currentAudio.audio.pause();currentAudio.stop();currentAudio=null;if(same)return;}
    if(!url)return;const audio=new Audio(url);own.current=audio;const stop=()=>{audio.pause();setPlaying(false);if(currentAudio?.audio===audio)currentAudio=null;};
    audio.addEventListener('loadedmetadata',()=>{audio.currentTime=Math.max(0,start);audio.play().then(()=>setPlaying(true)).catch(stop);},{once:true});
    audio.addEventListener('ended',stop);audio.addEventListener('error',stop);if(Number.isFinite(len))audio.addEventListener('timeupdate',()=>audio.currentTime>=start+len&&stop());currentAudio={audio,stop};};
  return <button type="button" className={'play'+(playing?' on':'')} disabled={!url} onPointerDown={event=>event.stopPropagation()} onClick={toggle} title={error||label||'试听'}>{playing?'■':'▶'}{label&&' '+label}</button>;
}
