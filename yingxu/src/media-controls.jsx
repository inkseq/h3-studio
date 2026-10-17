import React from 'react';
import {getFile} from './media.js';
import {validateRange} from './media-timing.js';

export function useLocalMedia(entity){
  const [state,setState]=React.useState({url:'',error:''});
  React.useEffect(()=>{let alive=true,url='';setState({url:'',error:''});if(!entity?.data.fileId){setState({url:'',error:entity?.data.missingFile?'文件缺失，请重新上传或恢复完整备份。':'还没有本地文件，请先上传。'});return;}
    getFile(entity.data.fileId).then(blob=>{if(!alive)return;if(!blob){setState({url:'',error:'此浏览器找不到原文件，请重新上传或恢复完整备份。'});return;}url=URL.createObjectURL(blob);setState({url,error:''});}).catch(()=>alive&&setState({url:'',error:'文件读取失败，请检查浏览器存储后重试。'}));return()=>{alive=false;if(url)URL.revokeObjectURL(url);};
  },[entity?.data.fileId]);return state;
}

export function LocalRangePreview({entity,start=0,end,duration,onDuration,onError,onPosition}){
  const {url,error}=useLocalMedia(entity),ref=React.useRef(null),frame=React.useRef(0),[playing,setPlaying]=React.useState(false),[playError,setPlayError]=React.useState('');
  const stop=React.useCallback(()=>{ref.current?.pause();setPlaying(false);cancelAnimationFrame(frame.current);},[]);
  React.useEffect(()=>{const stopOther=()=>stop();window.addEventListener('yingxu-stop-local-media',stopOther);return()=>{window.removeEventListener('yingxu-stop-local-media',stopOther);ref.current?.pause();cancelAnimationFrame(frame.current);};},[stop]);
  React.useEffect(()=>{stop();setPlayError('');},[url,start,end,stop]);
  React.useEffect(()=>{const element=ref.current;return()=>element?.pause();},[url]);
  React.useEffect(()=>{if(error)onError?.(error);},[error]);
  const checkEnd=()=>{const el=ref.current;if(!el)return;if(Number.isFinite(Number(end))&&el.currentTime>=Number(end)){stop();el.currentTime=Number(start)||0;return;}if(!el.paused)frame.current=requestAnimationFrame(checkEnd);};
  const play=async()=>{if(playing){stop();return;}const invalid=validateRange(start,end,duration);if(invalid){setPlayError(invalid);return;}window.dispatchEvent(new Event('yingxu-stop-local-media'));setPlayError('');const el=ref.current;if(!el)return;el.currentTime=Number(start);try{await el.play();setPlaying(!el.paused);frame.current=requestAnimationFrame(checkEnd);}catch{setPlayError('浏览器未能播放此文件。请再次点击，或换一个支持的文件。');setPlaying(false);}};
  if(error)return <p className="media-control-error" role="alert">{error}</p>;if(!url)return <p className="media-control-hint">正在读取本地文件…</p>;
  if(entity.type==='image')return <><img className="reference-image-preview" src={url} alt={entity.title} onError={()=>setPlayError('图片无法解码，请重新上传。')}/>{playError&&<p role="alert" className="media-control-error">{playError}</p>}</>;
  const props={ref,src:url,preload:'metadata',onTimeUpdate:e=>onPosition?.(e.currentTarget.currentTime),onLoadedMetadata:e=>{const d=e.currentTarget.duration;if(Number.isFinite(d)&&d>0)onDuration?.(d);else setPlayError('无法读取文件时长，请换一个文件。');},onError:()=>{setPlayError('文件无法播放，请换成浏览器支持的格式。');onError?.('文件无法播放');},onEnded:stop,onPause:()=>setPlaying(false)};
  return <div className="local-range-preview">{entity.type==='video'?<video {...props} playsInline controls={!!onPosition}/>:<audio {...props}/>}<button type="button" onClick={play} disabled={!duration}>{playing?'暂停选段':'试听 / 试看选段'}</button><span className="media-control-hint">{duration?`原文件 ${duration.toFixed(2)} 秒`:'正在读取文件时长…'} · 只预览所填选段</span>{playError&&<p className="media-control-error" role="alert">{playError}</p>}</div>;
}

export function RangeFields({start,end,setStart,setEnd,prefix='参考',duration}){return <div className="media-range-fields"><label>{prefix}开始（秒）<input aria-label={`${prefix}开始（秒）`} type="number" min="0" step="0.1" value={start} onChange={e=>setStart(e.target.value)}/></label><label>{prefix}结束（秒）<input aria-label={`${prefix}结束（秒）`} type="number" min="0" step="0.1" max={duration||undefined} value={end} onChange={e=>setEnd(e.target.value)}/></label></div>;}
