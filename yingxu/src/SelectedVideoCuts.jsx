import GeneratedSoundAction from './GeneratedSoundAction.jsx';
import React,{useState} from 'react';
import {store} from './store.js';
import {LocalRangePreview,RangeFields} from './media-controls.jsx';
import {alignVideoRange,applyVideoRange,boundVideoRange,videoBinding,videoCut} from './video-cut-model.js';

const show=value=>Number.isFinite(value)?Number(value.toFixed(4)):'未知';
function CutEditor({shot,asset,onDone}){
  const existing=boundVideoRange(shot.data.selectedVideoRange,asset)?shot.data.selectedVideoRange:null;
  const [start,setStart]=useState(String(existing?.start??0)),[end,setEnd]=useState(existing?String(existing.end):''),[duration,setDuration]=useState(null),[position,setPosition]=useState(0),[error,setError]=useState('');
  const aligned=start!==''&&end!==''?alignVideoRange(Number(start),Number(end)):null;
  const save=useRangeDuration=>{
    if(start===''||end===''){setError('请填写入点和出点。');return;}
    const range={...videoBinding(asset),start:Number(start),end:Number(end)};
    let result;const ok=store.editProject(p=>{result=applyVideoRange(p,shot.id,range,{duration,useRangeDuration});});
    if(!ok){setError(store.getState().notice);return;}
    store.notify(useRangeDuration?'剪辑选段与镜头时长已一起保存。后续镜头/音轨位置可能改变，请重新核对字幕；可撤销恢复。':`选段已保存，镜头时长保持不变。实际取源 ${show(result.sourceStart)}–${show(result.sourceEnd)} 秒；请重新核对字幕。`);onDone();
  };
  return <div className="selected-cut-editor"><h4>{shot.title} · 源片选段</h4><p>调整的是采用视频的剪辑，不会修改原文件或生成输入参考。填写后点保存；收起、取消或切换步骤不会应用未保存的输入。</p><LocalRangePreview entity={asset} start={start} end={end} duration={duration} onDuration={value=>{setDuration(value);setEnd(old=>old===''?String(value):old);}} onPosition={setPosition}/><p className="muted">源片播放器可自由定位。当前位置 {show(position)} 秒；视频原声仅供源片试听，不会自动加入章节混音。</p><div className="media-control-row"><button disabled={!duration} type="button" onClick={()=>setStart(String(Number(position.toFixed(6))))}>将当前位置设为入点</button><button disabled={!duration||position<=0} type="button" onClick={()=>setEnd(String(Number(position.toFixed(6))))}>将当前位置设为出点</button></div><RangeFields start={start} end={end} setStart={setStart} setEnd={setEnd} prefix="剪辑源片" duration={duration}/>{aligned&&aligned.frames>0?<p role="status">原选段 {start}–{end} 秒；按24fps入点向上、出点向下，实际可用 {show(aligned.start)}–{show(aligned.end)} 秒，共 {aligned.frames} 帧 / {show(aligned.duration)} 秒。出点是不包含的结束边界。</p>:<p className="muted">填写起止后查看实际可用帧数；不足一帧不能保存。</p>}<p className="muted">保持时长时只取从实际入点起的镜头计划帧数，多余尾段不取。“使用选段时长”会移动后面的时间线；字幕文字保留，确认需要重新核对。</p>{error&&<p className="media-control-error" role="alert">{error}</p>}<div className="media-control-row"><button disabled={!duration} type="button" onClick={()=>save(false)}>保存选段，保持镜头 {show(shot.data.seconds)} 秒</button><button disabled={!duration||!aligned||aligned.frames<1} type="button" onClick={()=>save(true)}>使用选段时长并调整镜头</button><button type="button" onClick={onDone}>取消本次选段</button></div></div>;
}
export default function SelectedVideoCuts({project,shots,onChange=()=>{}}){
  const [editing,setEditing]=useState(null);
  const selected=shots.filter(shot=>project.entities.find(e=>e.id===shot.data.selectedAssetId)?.type==='video'||shot.data.selectedVideoRange);
  return <section className="selected-video-cuts" aria-label="采用视频剪辑选段"><h3>去掉片头片尾，留下要用的部分</h3><p>默认从已采用视频的开头取镜头时长。逐镜设入点和出点，原候选和文件都保留；不需要剪短可跳过。</p>{!selected.length&&<p className="muted">先在候选审核采用视频，再来设置；图片占位不能剪成实际视频。</p>}{selected.map(shot=>{const asset=project.entities.find(e=>e.id===shot.data.selectedAssetId),cut=videoCut(shot,asset);return <article className="selected-cut" key={shot.id}><div className="cloud-section-head"><b>{shot.title} · {asset?.title||'尚未采用视频'}</b><button type="button" disabled={asset?.type!=='video'||!asset?.data.fileId||asset.data.missingFile} onClick={()=>{onChange();setEditing(editing===shot.id?null:shot.id);}}>{editing===shot.id?'收起选段编辑':'设置剪辑选段'}</button></div>{cut.issue?<p className="cloud-warning">{cut.issue}</p>:<p>实际取源 {show(cut.sourceStart)}–{show(cut.sourceEnd)} 秒 / 镜头计划 {show(shot.data.seconds)} 秒{cut.unusedTailFrames?`；所选尾段另有 ${show(cut.unusedTailFrames/24)} 秒不取`:''}。{cut.range?'已保存独立剪辑选段。':'沿用从开头取片。'}</p>}{shot.data.selectedVideoRange&&<button type="button" onClick={()=>{onChange();store.editProject(p=>{delete p.entities.find(e=>e.id===shot.id).data.selectedVideoRange;});setEditing(null);store.notify('已恢复从源视频开头取片，镜头计划时长保持不变；可撤销。');}}>恢复从视频开头取片</button>}{asset?.type==='video'&&<GeneratedSoundAction key={shot.id+':'+asset.id} project={project} shot={shot}/>}{editing===shot.id&&asset?.type==='video'&&<CutEditor key={shot.id+':'+JSON.stringify(videoBinding(asset))} shot={shot} asset={asset} onDone={()=>setEditing(null)}/>}</article>})}</section>;
}
