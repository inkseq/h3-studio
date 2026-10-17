import React,{useRef,useState} from 'react';
import {Upload} from 'lucide-react';
import {store} from './store.js';
import {uploadFile} from './media.js';
import {replaceEntityMedia,uploadCharacterMainImage,uploadLocationImage} from './media-operations.js';
import {createUploadGate} from './workspace-context.js';

export function entityCover(project,entity){
  if(entity.type==='character'){const gallery=entity.data.looks?.[0]?.gallery;return project.entities.find(asset=>asset.id===(gallery?.front||Object.values(gallery||{}).find(Boolean))&&asset.type==='image');}
  if(entity.type==='location')return project.entities.find(asset=>(entity.data.referenceAssetIds||[]).includes(asset.id)&&asset.type==='image');
  return ['image','video','audio'].includes(entity.type)?entity:null;
}
export default function EntityMediaUpload({entity}){
  const ref=useRef(),gate=useRef(createUploadGate()),[busy,setBusy]=useState(false),[error,setError]=useState(''),kind=['character','location'].includes(entity.type)?'image':entity.type;
  const label=entity.type==='character'?'上传人物主图':entity.type==='location'?'上传场景图':entity.data.fileId?'替换素材文件':'上传'+({image:'图片',video:'视频',audio:'音频'}[kind]);
  async function upload(file){if(!file||gate.current.busy)return;setError('');await gate.current.run(async()=>{setBusy(true);try{if(entity.type==='character')await uploadCharacterMainImage({store,characterId:entity.id,file,upload:uploadFile});else if(entity.type==='location')await uploadLocationImage({store,locationId:entity.id,file,upload:uploadFile});else await replaceEntityMedia({store,entityId:entity.id,file,upload:uploadFile});store.notify('素材已保存到当前故事；参考可在镜头中复用。');}catch(error){setError(error.message);}finally{setBusy(false);if(ref.current)ref.current.value='';}});}
  return <div className="entity-inline-upload"><input ref={ref} type="file" accept={kind+'/*'} className="visually-hidden" aria-label={`${entity.title} · ${label}`} onChange={event=>upload(event.target.files?.[0])}/><button type="button" disabled={busy} onClick={()=>ref.current.click()}><Upload size={14}/>{busy?'正在保存…':label}</button>{error&&<p role="alert" className="cloud-error">{error}</p>}</div>;
}
