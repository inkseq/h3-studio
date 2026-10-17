import React,{useRef,useState} from 'react';
import {Upload,Image,Video,Music} from 'lucide-react';
import {store} from './store.js';
import {uploadFile} from './media.js';
import {uploadLibraryFiles} from './media-operations.js';
import {createUploadGate} from './workspace-context.js';
import './media-workspace.css';

const types=[['image','上传图片',Image],['video','上传视频',Video],['audio','上传音频',Music]];
export default function MediaLibraryUpload(){
  const input=useRef(),gate=useRef(createUploadGate()),[kind,setKind]=useState(''),[busy,setBusy]=useState(false),[dragging,setDragging]=useState(false),[error,setError]=useState(''),[message,setMessage]=useState('');
  async function accept(files){if(!files.length)return;if(gate.current.busy){setError('当前上传还未完成，请稍后再添加。');return;}setError('');setMessage('');let completed=0;await gate.current.run(async()=>{setBusy(true);try{for(const file of files){await uploadLibraryFiles({store,files:[file],upload:uploadFile});completed++;}setMessage(`已保存 ${completed} 份素材。可在镜头中选择用途，同一素材可以重复使用。`);}catch(error){setError(`${completed?`前 ${completed} 份已保存。`:''}${error.message}`);}finally{setBusy(false);if(input.current)input.current.value='';}});}
  const choose=type=>{setKind(type);input.current.accept=type+'/*';input.current.click();};
  return <section className={'media-library-upload '+(dragging?'is-dragging':'')} aria-label="上传故事素材" onDragOver={event=>{event.preventDefault();if(!busy)setDragging(true);}} onDragLeave={()=>setDragging(false)} onDrop={event=>{event.preventDefault();setDragging(false);accept([...event.dataTransfer.files]);}}><Upload size={25}/><div><h3>{busy?'正在保存素材…':'把图片、视频、音频拖到这里'}</h3><p>先上传再选择用途，不用先创建文字节点。每份素材保存到当前故事，镜头可以随时复用。</p><div>{types.map(([type,label,Icon])=><button type="button" key={type} disabled={busy} onClick={()=>choose(type)}><Icon size={16}/>{label}</button>)}</div></div><input className="visually-hidden" type="file" ref={input} aria-label={kind?types.find(([type])=>type===kind)?.[1]:'上传图片视频音频'} multiple onChange={event=>accept([...event.target.files])}/>{error&&<p role="alert" className="cloud-error">{error}</p>}{message&&<p role="status" className="media-upload-message">{message}</p>}</section>;
}
