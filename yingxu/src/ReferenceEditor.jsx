import React from 'react';
import {store,roleLabels} from './store.js';
import {LocalRangePreview,RangeFields} from './media-controls.jsx';
import {validateRange} from './media-timing.js';
import './reference-editor.css';

const uses={image:['firstFrame','lastFrame','identity','reference'],video:['motion','reference'],audio:['audio','reference']};
function ReferenceCard({entity,link,shot,onInspect}){
  const saved=shot.data.referenceRanges?.[link.id],timed=entity.type!=='image';
  const [duration,setDuration]=React.useState(null),[start,setStart]=React.useState(String(saved?.start??0)),[end,setEnd]=React.useState(saved?.end===undefined?'':String(saved.end)),[error,setError]=React.useState(''),[message,setMessage]=React.useState('');
  React.useEffect(()=>{setStart(String(saved?.start??0));setEnd(saved?.end===undefined?'':String(saved.end));setError('');},[saved?.start,saved?.end,entity.data.fileId]);
  React.useEffect(()=>setDuration(null),[entity.data.fileId]);
  const metadata=d=>{setDuration(d);setEnd(value=>value===''?String(Number(d.toFixed(3))):value);};
  const change=(setter)=>(value)=>{setter(value);setError('');setMessage('选段尚未保存。');};
  const save=()=>{const problem=validateRange(start,end,duration);if(problem){setError(problem);return;}
    const ok=store.editProject(p=>{const s=p.entities.find(e=>e.id===shot.id);if(!s||!p.links.some(l=>l.id===link.id))return false;s.data.referenceRanges={...(s.data.referenceRanges||{}),[link.id]:{start:Number(start),end:Number(end),duration,fileId:entity.data.fileId}};s.status='review';});if(ok){setMessage('已保存选段；原文件保持完整。');setError('');}
  };
  const unlink=()=>store.editProject(p=>{p.links=p.links.filter(l=>l.id!==link.id);const s=p.entities.find(e=>e.id===shot.id);if(s){const ranges={...(s.data.referenceRanges||{})};delete ranges[link.id];s.data.referenceRanges=ranges;s.status='review';}});
  return <article className="reference-card" data-testid="reference-card"><header><div><strong>{entity.title}</strong><span>{roleLabels[link.role]}</span></div><button type="button" onClick={unlink}>解除关联</button></header>
    {saved?.fileId&&saved.fileId!==entity.data.fileId&&<p className="media-control-error" role="alert">原文件已更换，请重新检查片段并保存；旧范围暂不视为已确认。</p>}
    <LocalRangePreview entity={entity} start={start} end={end} duration={duration} onDuration={metadata}/>
    {timed&&<><RangeFields start={start} end={end} setStart={change(setStart)} setEnd={change(setEnd)} duration={duration}/><div className="media-control-row"><button type="button" onClick={save} disabled={!duration}>保存参考选段</button><span className="media-control-hint">{saved?`已保存 ${saved.start}–${saved.end} 秒`:'默认使用整个文件，可只截取有用的一段。'}</span></div></>}
    {error&&<p role="alert" className="media-control-error">{error}</p>}{message&&<p role="status" className="media-control-hint">{message}</p>}{onInspect&&<button type="button" className="media-text-button" onClick={()=>onInspect(entity.id)}>查看 / 重新上传原文件</button>}
  </article>;
}

export default function ReferenceEditor({project,shotId,onInspect}){
  const shot=project.entities.find(e=>e.id===shotId),assets=project.entities.filter(e=>['image','video','audio'].includes(e.type)),[assetId,setAssetId]=React.useState(''),[role,setRole]=React.useState('reference'),[error,setError]=React.useState('');
  const asset=assets.find(e=>e.id===assetId),links=project.links.filter(l=>l.target===shotId&&assets.some(a=>a.id===l.source));
  const selectAsset=id=>{setAssetId(id);const e=assets.find(a=>a.id===id);setRole(uses[e?.type]?.[0]||'reference');setError('');};
  if(!shot)return null;
  const attach=()=>{if(!asset){setError('先选择一份素材，再指定用途。');return;}const result=store.addLink(asset.id,shot.id,role);if(result.ok){setAssetId('');setError('');}else setError(result.error);};
  return <section className="reference-editor" aria-label={`${shot.title}的参考选段`}><h4>用什么来指导这个镜头</h4><p className="media-control-hint">图片帮助固定形象，视频示范动作，音频提供声音。参考是输入素材，不会自动成为最终画面。</p>
    {assets.length?<div className="reference-add"><label>已有参考素材<select aria-label="已有参考素材" value={assetId} onChange={e=>selectAsset(e.target.value)}><option value="">选择一份素材</option>{assets.map(a=><option key={a.id} value={a.id}>{({image:'图片',video:'视频',audio:'音频'}[a.type])} · {a.title}</option>)}</select></label><label>参考用途<select aria-label="参考用途" value={role} onChange={e=>setRole(e.target.value)}>{(uses[asset?.type]||['reference']).map(r=><option key={r} value={r}>{roleLabels[r]}</option>)}</select></label><button type="button" onClick={attach}>关联到此镜头</button></div>:<p className="media-control-empty">还没有图片、视频或音频。先到素材库上传，或跳过参考继续设计镜头。</p>}
    {error&&<p className="media-control-error" role="alert">{error}</p>}
    <div className="reference-list">{links.map(link=><ReferenceCard key={link.id} entity={assets.find(a=>a.id===link.source)} link={link} shot={shot} onInspect={onInspect}/>)}</div>{!!links.length&&<p className="media-control-hint">解除关联只影响此镜头，原文件仍在素材库。修改选段后请重新核对候选。</p>}
  </section>;
}
