import React,{useState} from 'react';
import {store} from './store.js';
import {chapterForShot,generatedAudioOption,upsertGeneratedSound} from './generated-sound-model.js';

export default function GeneratedSoundAction({project,shot}){
  const [confirm,setConfirm]=useState(false),option=generatedAudioOption(project,shot.id),chapterId=chapterForShot(project,shot.id);
  const existing=project.journey?.soundTracks?.[chapterId]?.find(t=>t.shotId===shot.id&&t.generatedFrom);
  return <div className="generated-sound-action"><button type="button" disabled={!!option.issue} onClick={()=>setConfirm(!confirm)}>使用这次生成的声音</button>{option.issue?<p className="muted">{option.issue}</p>:<p className="muted">同一次任务的独立FLAC{option.audio.data.simulation?'（模拟来源）':''}。{existing?'已有此镜头的生成音轨，再次选择会更新原轨，不会叠加。':'不会随采用画面自动加入章节。'}</p>}
    {confirm&&!option.issue&&<div className="journey-soft-panel"><p>将「{option.audio.title}」加入当前镜头，音量70%，并把声音方案切为混合声音。跟随该视频的入点、时长和镜头位置；已有手动音轨保留。</p><div className="cloud-actions"><button type="button" onClick={()=>{if(store.editProject(p=>{upsertGeneratedSound(p,shot.id);})){setConfirm(false);store.notify('已更新本镜头生成音轨并切到混合声音。可在节奏与声音试听、调音量或静音；可撤销。');}}}>加入音轨并切到混合声音</button><button type="button" onClick={()=>setConfirm(false)}>暂不加入声音</button></div></div>}
  </div>;
}
