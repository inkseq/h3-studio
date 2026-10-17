import {useI18n} from './i18n/I18nProvider.jsx';
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
  const {t:at}=useI18n();
  const ref=useRef(),gate=useRef(createUploadGate()),[busy,setBusy]=useState(false),[error,setError]=useState(''),kind=['character','location'].includes(entity.type)?'image':entity.type;
  const label=entity.type==='character'?at('adjunct.entityMediaUpload.copy68'):entity.type==='location'?at('adjunct.entityMediaUpload.copy69'):entity.data.fileId?at('adjunct.entityMediaUpload.copy70'):at('adjunct.entityMediaUpload.copy71')+({image:at('adjunct.entityMediaUpload.copy72'),video:at('adjunct.entityMediaUpload.copy73'),audio:at('adjunct.entityMediaUpload.copy74')}[kind]);
  async function upload(file){if(!file||gate.current.busy)return;setError('');await gate.current.run(async()=>{setBusy(true);try{if(entity.type==='character')await uploadCharacterMainImage({store,characterId:entity.id,file,upload:uploadFile});else if(entity.type==='location')await uploadLocationImage({store,locationId:entity.id,file,upload:uploadFile});else await replaceEntityMedia({store,entityId:entity.id,file,upload:uploadFile});store.notify(at('adjunct.entityMediaUpload.copy75'));}catch(error){setError(error.message);}finally{setBusy(false);if(ref.current)ref.current.value='';}});}
  return <div className="entity-inline-upload"><input ref={ref} type="file" accept={kind+'/*'} className="visually-hidden" aria-label={`${entity.title} · ${label}`} onChange={event=>upload(event.target.files?.[0])}/><button type="button" disabled={busy} onClick={()=>ref.current.click()}><Upload size={14}/>{busy?at('adjunct.entityMediaUpload.copy76'):label}</button>{error&&<p role="alert" className="cloud-error">{error}</p>}</div>;
}
