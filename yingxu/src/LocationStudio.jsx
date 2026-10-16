import {useI18n} from './i18n/I18nProvider.jsx';
import React,{useEffect,useRef,useState} from 'react';
import {ImagePlus,MapPin,Plus,Upload,Trash2} from 'lucide-react';
import {store} from './store.js';
import {uploadFile} from './media.js';
import {uploadLocationImage} from './media-operations.js';
import {setLocationReferences,bindLocation,locationShots} from './location-model.js';
import {createUploadGate} from './workspace-context.js';
import AssetPreview from './AssetPreview.jsx';
import './media-workspace.css';

export function LocationReferenceEditor({project,location}){
  const {t:at}=useI18n();
 const {t:lt}=useI18n();
  const input=useRef(),gate=useRef(createUploadGate()),[busy,setBusy]=useState(false),[error,setError]=useState(''),[dragging,setDragging]=useState(false),[chosen,setChosen]=useState('');
  const ids=location.data.referenceAssetIds||[],assets=ids.map(id=>project.entities.find(entity=>entity.id===id)).filter(Boolean),images=project.entities.filter(entity=>entity.type==='image'&&!ids.includes(entity.id));
  const edit=next=>{let result;const ok=store.editProject(project=>{result=setLocationReferences(project,location.id,next);return result.ok;});if(!ok)setError(result?.error||store.getState().notice);};
  async function accept(files){if(!files.length)return;if(gate.current.busy)return;setError('');await gate.current.run(async()=>{setBusy(true);try{for(const file of files)await uploadLocationImage({store,locationId:location.id,file,upload:uploadFile});}catch(error){setError(error.message);}finally{setBusy(false);if(input.current)input.current.value='';}});}
  return <section className="location-reference-editor" aria-label={at('adjunct.locationStudio.copy300',{v0:location.title})} onDragOver={event=>{event.preventDefault();if(!busy)setDragging(true);}} onDragLeave={()=>setDragging(false)} onDrop={event=>{event.preventDefault();setDragging(false);accept([...event.dataTransfer.files]);}}><h4><ImagePlus size={17}/>{lt('legacy.route54')}</h4><p>{lt('legacy.explain8')}</p><input ref={input} type="file" multiple accept="image/png,image/jpeg,image/webp,image/gif" aria-label={at('adjunct.locationStudio.copy301',{v0:location.title})} className="visually-hidden" onChange={event=>accept([...event.target.files])}/><button type="button" disabled={busy} className={'h3-drop-button '+(dragging?'is-dragging':'')} onClick={()=>input.current.click()}><Upload size={18}/>{busy?at('adjunct.locationStudio.copy302'):at('adjunct.locationStudio.copy303')}</button>{!!assets.length&&<div className="h3-media-grid">{assets.map(asset=><article key={asset.id}><div className="h3-media-preview"><AssetPreview entity={asset}/></div><div className="h3-media-caption"><b>{asset.title}</b><button type="button" disabled={busy} onClick={()=>edit(ids.filter(id=>id!==asset.id))}><Trash2 size={13}/>{lt('legacy.route55')}</button></div></article>)}</div>}{!!images.length&&<div className="location-reuse"><label>{lt('legacy.route56')}<select aria-label={at('adjunct.locationStudio.copy304',{v0:location.title})} value={chosen} onChange={event=>setChosen(event.target.value)}><option value="">{lt('legacy.route57')}</option>{images.map(image=><option key={image.id} value={image.id}>{image.title}</option>)}</select></label><button type="button" disabled={!chosen||busy} onClick={()=>{edit([...ids,chosen]);setChosen('');}}>{lt('legacy.route58')}</button></div>}{error&&<p role="alert" className="cloud-error">{error}</p>}</section>;
}

export function SceneLocationPicker({project,scene}){
  const {t:at}=useI18n();
 const {t:lt}=useI18n();
  const locations=project.entities.filter(entity=>entity.type==='location');
  return !!locations.length&&<label className="scene-location-picker">{lt('legacy.route59')}<select aria-label={at('adjunct.locationStudio.copy305',{v0:scene.title})} value={scene.data.locationId||''} onChange={event=>{let result;store.editProject(project=>{result=bindLocation(project,scene.id,event.target.value);return result.ok;});if(result?.ok===false)store.notify(result.error);}}><option value="">{lt('legacy.route60')}</option>{locations.map(location=><option key={location.id} value={location.id}>{location.title}</option>)}</select><small>{lt('legacy.explain11')}</small></label>;
}

export default function LocationStudio({project,onAdd,onInspect}){
  const {t:at}=useI18n();
 const {t:lt}=useI18n();
  const locations=project.entities.filter(entity=>entity.type==='location'),[locationId,setLocationId]=useState(''),location=locations.find(entity=>entity.id===locationId)||locations[0],scenes=project.entities.filter(entity=>entity.type==='scene');
  useEffect(()=>{if(project.journey?.focusLocationId)setLocationId(project.journey.focusLocationId);},[project.journey?.focusLocationId]);
  return <section className="location-studio" aria-label={lt('legacy.route66')}><header><div><span className="eyebrow">LOCATION REFERENCES</span><h3><MapPin size={20}/>{lt('legacy.route61')}</h3><p>{lt('legacy.explain9')}</p></div><button type="button" onClick={()=>onAdd('location')}><Plus size={15}/>{lt('legacy.route62')}</button></header>{!location?<div className="media-workspace-empty"><MapPin size={27}/><p>{lt('legacy.route63')}</p></div>:<><div className="location-tabs" role="group" aria-label={lt('legacy.route67')}>{locations.map(entity=><button type="button" key={entity.id} aria-pressed={entity.id===location.id} onClick={()=>setLocationId(entity.id)}>{entity.title}</button>)}</div><div className="location-summary"><strong>{location.title}</strong><p>{location.description||at('adjunct.locationStudio.copy306')}</p><button type="button" onClick={()=>onInspect(location.id)}>{lt('legacy.route64')}</button></div><LocationReferenceEditor key={location.id} project={project} location={location}/><details className="location-scene-use"><summary>{at('adjunct.locationStudio.copy307')}{locationShots(project,location.id).length}{at('adjunct.locationStudio.copy308')}</summary>{scenes.length?scenes.map(scene=><div key={scene.id}><strong>{scene.title}</strong><SceneLocationPicker project={project} scene={scene}/></div>):<p>{lt('legacy.route65')}</p>}</details><p className="media-workspace-hint">{lt('legacy.explain10')}</p></>}</section>;
}
