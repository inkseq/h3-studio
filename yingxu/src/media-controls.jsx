import {useI18n} from './i18n/I18nProvider.jsx';
import React from 'react';
import {getFile} from './media.js';
import {validateRange} from './media-timing.js';

export function useLocalMedia(entity){
 const {t:lt}=useI18n();
  const [state,setState]=React.useState({url:'',error:''});
  React.useEffect(()=>{let alive=true,url='';setState({url:'',error:''});if(!entity?.data.fileId){setState({url:'',error:entity?.data.missingFile?lt('media.control1'):lt('media.control2')});return;}
    getFile(entity.data.fileId).then(blob=>{if(!alive)return;if(!blob){setState({url:'',error:lt('media.control3')});return;}url=URL.createObjectURL(blob);setState({url,error:''});}).catch(()=>alive&&setState({url:'',error:lt('media.control4')}));return()=>{alive=false;if(url)URL.revokeObjectURL(url);};
  },[entity?.data.fileId]);return state;
}

export function LocalRangePreview({entity,start=0,end,duration,onDuration,onError,onPosition}){
 const {t:lt,locale}=useI18n();
  const {url,error}=useLocalMedia(entity),ref=React.useRef(null),frame=React.useRef(0),[playing,setPlaying]=React.useState(false),[playError,setPlayError]=React.useState('');
  const stop=React.useCallback(()=>{ref.current?.pause();setPlaying(false);cancelAnimationFrame(frame.current);},[]);
  React.useEffect(()=>{const stopOther=()=>stop();window.addEventListener('yingxu-stop-local-media',stopOther);return()=>{window.removeEventListener('yingxu-stop-local-media',stopOther);ref.current?.pause();cancelAnimationFrame(frame.current);};},[stop]);
  React.useEffect(()=>{stop();setPlayError('');},[url,start,end,stop]);
  React.useEffect(()=>{const element=ref.current;return()=>element?.pause();},[url]);
  React.useEffect(()=>{if(error)onError?.(error);},[error]);
  const checkEnd=()=>{const el=ref.current;if(!el)return;if(Number.isFinite(Number(end))&&el.currentTime>=Number(end)){stop();el.currentTime=Number(start)||0;return;}if(!el.paused)frame.current=requestAnimationFrame(checkEnd);};
  const play=async()=>{if(playing){stop();return;}const invalid=validateRange(start,end,duration,locale);if(invalid){setPlayError(invalid);return;}window.dispatchEvent(new Event('yingxu-stop-local-media'));setPlayError('');const el=ref.current;if(!el)return;el.currentTime=Number(start);try{await el.play();setPlaying(!el.paused);frame.current=requestAnimationFrame(checkEnd);}catch{setPlayError(lt('media.control5'));setPlaying(false);}};
  if(error)return <p className="media-control-error" role="alert">{error}</p>;if(!url)return <p className="media-control-hint">{lt('media.control6')}</p>;
  if(entity.type==='image')return <><img className="reference-image-preview" src={url} alt={entity.title} onError={()=>setPlayError(lt('media.control7'))}/>{playError&&<p role="alert" className="media-control-error">{playError}</p>}</>;
  const props={ref,src:url,preload:'metadata',onTimeUpdate:e=>onPosition?.(e.currentTarget.currentTime),onLoadedMetadata:e=>{const d=e.currentTarget.duration;if(Number.isFinite(d)&&d>0)onDuration?.(d);else setPlayError(lt('media.control8'));},onError:()=>{setPlayError(lt('media.control9'));onError?.(lt('media.control10'));},onEnded:stop,onPause:()=>setPlaying(false)};
  return <div className="local-range-preview">{entity.type==='video'?<video {...props} playsInline controls={!!onPosition}/>:<audio {...props}/>}<button type="button" onClick={play} disabled={!duration}>{playing?lt('media.control11'):lt('media.control12')}</button><span className="media-control-hint">{duration?lt('media.duration',{seconds:duration.toFixed(2)}):lt('media.control13')} · {lt('media.control14')}</span>{playError&&<p className="media-control-error" role="alert">{playError}</p>}</div>;
}

export function RangeFields({start,end,setStart,setEnd,prefix,duration}){const {t:lt}=useI18n();prefix=prefix??lt('media.control15');return <div className="media-range-fields"><label>{lt('media.start',{prefix})}<input aria-label={`${lt('media.start',{prefix})}`} type="number" min="0" step="0.1" value={start} onChange={e=>setStart(e.target.value)}/></label><label>{lt('media.end',{prefix})}<input aria-label={`${lt('media.end',{prefix})}`} type="number" min="0" step="0.1" max={duration||undefined} value={end} onChange={e=>setEnd(e.target.value)}/></label></div>;}
