import {useI18n} from './i18n/I18nProvider.jsx';
import React,{useState} from 'react';
import {artifactMedia} from './artifact-media.js';

export default function ArtifactResult({artifact,job,title}){
  const {t:at}=useI18n();
  const [preview,setPreview]=useState(false),[error,setError]=useState(''),[message,setMessage]=useState(''),media=artifactMedia(artifact,job,{title});
  if(!media)return <p role="alert">{at('adjunct.artifactResult.copy32')}</p>;
  const previewLabel=media.type==='audio'?at('adjunct.artifactResult.copy33'):at('adjunct.artifactResult.copy34');
  return <div className="roughcut-result"><div className="cloud-actions"><button onClick={()=>{setPreview(!preview);setError('');}}>{preview?at('adjunct.artifactResult.copy35')+(media.type==='audio'?at('adjunct.artifactResult.copy36'):at('adjunct.artifactResult.copy37')):previewLabel}</button><a className="artifact-download" href={media.downloadURL} download={media.filename} referrerPolicy="no-referrer" onClick={()=>setMessage(at('adjunct.artifactResult.copy38'))}>{at('adjunct.artifactResult.copy39')}{media.type==='audio'?at('adjunct.artifactMix'):at('adjunct.artifactVideo')}</a></div>
    {media.type==='audio'&&<p>{at('adjunct.artifactResult.copy40')}</p>}
    {preview&&(media.type==='audio'?<audio controls preload="metadata" src={media.previewURL} aria-label={at('adjunct.artifactResult.copy41')} onError={()=>setError(at('adjunct.artifactResult.copy42'))}/>:<video controls data-video-cache-play="true" preload="metadata" src={media.previewURL} aria-label={at('adjunct.artifactResult.copy43')} onError={()=>setError(at('adjunct.artifactResult.copy44'))}/>)}
    {error&&<p role="alert" className="cloud-error">{error}</p>}{message&&<p role="status">{message}</p>}
  </div>;
}
