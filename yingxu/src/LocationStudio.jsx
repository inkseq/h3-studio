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
  const input=useRef(),gate=useRef(createUploadGate()),[busy,setBusy]=useState(false),[error,setError]=useState(''),[dragging,setDragging]=useState(false),[chosen,setChosen]=useState('');
  const ids=location.data.referenceAssetIds||[],assets=ids.map(id=>project.entities.find(entity=>entity.id===id)).filter(Boolean),images=project.entities.filter(entity=>entity.type==='image'&&!ids.includes(entity.id));
  const edit=next=>{let result;const ok=store.editProject(project=>{result=setLocationReferences(project,location.id,next);return result.ok;});if(!ok)setError(result?.error||store.getState().notice);};
  async function accept(files){if(!files.length)return;if(gate.current.busy)return;setError('');await gate.current.run(async()=>{setBusy(true);try{for(const file of files)await uploadLocationImage({store,locationId:location.id,file,upload:uploadFile});}catch(error){setError(error.message);}finally{setBusy(false);if(input.current)input.current.value='';}});}
  return <section className="location-reference-editor" aria-label={`${location.title} · 场景参考图片`} onDragOver={event=>{event.preventDefault();if(!busy)setDragging(true);}} onDragLeave={()=>setDragging(false)} onDrop={event=>{event.preventDefault();setDragging(false);accept([...event.dataTransfer.files]);}}><h4><ImagePlus size={17}/>场景参考图片</h4><p>上传空间布局、氛围或不同机位的图片。绑定到场戏后，镜头可继承这些参考。</p><input ref={input} type="file" multiple accept="image/png,image/jpeg,image/webp,image/gif" aria-label={`${location.title} · 上传场景参考图`} className="visually-hidden" onChange={event=>accept([...event.target.files])}/><button type="button" disabled={busy} className={'h3-drop-button '+(dragging?'is-dragging':'')} onClick={()=>input.current.click()}><Upload size={18}/>{busy?'正在保存…':'拖入场景图片，或点击上传'}</button>{!!assets.length&&<div className="h3-media-grid">{assets.map(asset=><article key={asset.id}><div className="h3-media-preview"><AssetPreview entity={asset}/></div><div className="h3-media-caption"><b>{asset.title}</b><button type="button" disabled={busy} onClick={()=>edit(ids.filter(id=>id!==asset.id))}><Trash2 size={13}/>解除地点关联</button></div></article>)}</div>}{!!images.length&&<div className="location-reuse"><label>复用素材库图片<select aria-label={`${location.title} · 已有图片`} value={chosen} onChange={event=>setChosen(event.target.value)}><option value="">选择已有图片</option>{images.map(image=><option key={image.id} value={image.id}>{image.title}</option>)}</select></label><button type="button" disabled={!chosen||busy} onClick={()=>{edit([...ids,chosen]);setChosen('');}}>加入地点参考</button></div>}{error&&<p role="alert" className="cloud-error">{error}</p>}</section>;
}

export function SceneLocationPicker({project,scene}){
  const locations=project.entities.filter(entity=>entity.type==='location');
  return !!locations.length&&<label className="scene-location-picker">场戏的默认地点<select aria-label={`${scene.title} · 场戏地点`} value={scene.data.locationId||''} onChange={event=>{let result;store.editProject(project=>{result=bindLocation(project,scene.id,event.target.value);return result.ok;});if(result?.ok===false)store.notify(result.error);}}><option value="">暂不指定</option>{locations.map(location=><option key={location.id} value={location.id}>{location.title}</option>)}</select><small>这场的新镜头沿用同一地点；镜头里可以单独覆盖。</small></label>;
}

export default function LocationStudio({project,onAdd,onInspect}){
  const locations=project.entities.filter(entity=>entity.type==='location'),[locationId,setLocationId]=useState(''),location=locations.find(entity=>entity.id===locationId)||locations[0],scenes=project.entities.filter(entity=>entity.type==='scene');
  useEffect(()=>{if(project.journey?.focusLocationId)setLocationId(project.journey.focusLocationId);},[project.journey?.focusLocationId]);
  return <section className="location-studio" aria-label="地点视觉档案"><header><div><span className="eyebrow">LOCATION REFERENCES</span><h3><MapPin size={20}/>让每场戏发生在看得见的地方</h3><p>先确定地点参考，再安排它用在哪些场戏。不同镜头可以共用同一个空间。</p></div><button type="button" onClick={()=>onAdd('location')}><Plus size={15}/>添加地点</button></header>{!location?<div className="media-workspace-empty"><MapPin size={27}/><p>添加一个地点，直接上传场景图。</p></div>:<><div className="location-tabs" role="group" aria-label="选择地点">{locations.map(entity=><button type="button" key={entity.id} aria-pressed={entity.id===location.id} onClick={()=>setLocationId(entity.id)}>{entity.title}</button>)}</div><div className="location-summary"><strong>{location.title}</strong><p>{location.description||'先放入参考图，地点设定可稍后补充。'}</p><button type="button" onClick={()=>onInspect(location.id)}>编辑地点名称与设定</button></div><LocationReferenceEditor key={location.id} project={project} location={location}/><details className="location-scene-use"><summary>安排到场戏 · 已用于 {locationShots(project,location.id).length} 个镜头</summary>{scenes.length?scenes.map(scene=><div key={scene.id}><strong>{scene.title}</strong><SceneLocationPicker project={project} scene={scene}/></div>):<p>还没有场戏，添加后再选择地点。</p>}</details><p className="media-workspace-hint">地点图片在全能参考模式中作为图像输入；不会自动生成 3D 世界或新增机位图片。</p></>}</section>;
}
