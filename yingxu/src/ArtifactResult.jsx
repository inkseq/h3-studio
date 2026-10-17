import React,{useState} from 'react';
import {artifactMedia} from './artifact-media.js';

export default function ArtifactResult({artifact,job,title}){
  const [preview,setPreview]=useState(false),[error,setError]=useState(''),[message,setMessage]=useState(''),media=artifactMedia(artifact,job,{title});
  if(!media)return <p role="alert">结果格式或下载地址尚未支持，请刷新任务后核对；不会打开未知外部地址。</p>;
  const previewLabel=media.type==='audio'?'试听独立混音':'播放这版粗剪';
  return <div className="roughcut-result"><div className="cloud-actions"><button onClick={()=>{setPreview(!preview);setError('');}}>{preview?'收起'+(media.type==='audio'?'混音':'成片'):previewLabel}</button><a className="artifact-download" href={media.downloadURL} download={media.filename} referrerPolicy="no-referrer" onClick={()=>setMessage('已请求浏览器下载；请在下载列表查看进度、完成状态或重试。')}>下载{media.label}</a></div>
    {media.type==='audio'&&<p>独立混音与这版成片使用同一时间线，包含开头和结尾的静音；视频原声未参与混音。</p>}
    {preview&&(media.type==='audio'?<audio controls preload="metadata" src={media.previewURL} aria-label="章节独立混音预览" onError={()=>setError('混音暂时无法播放。可以尝试下载 FLAC，或刷新任务检查登录状态。')}/>:<video controls preload="metadata" src={media.previewURL} aria-label="章节粗剪视频预览" onError={()=>setError('成片暂时无法播放，请刷新任务检查登录状态后重试。')}/>)}
    {error&&<p role="alert" className="cloud-error">{error}</p>}{message&&<p role="status">{message}</p>}
  </div>;
}
