import {useI18n} from './i18n/I18nProvider.jsx';
import {legacyReason} from './i18n/legacyReasons.js';
import React,{useState} from 'react';
import {store} from './store.js';
import {chapterForShot,generatedAudioOption,upsertGeneratedSound} from './generated-sound-model.js';

export default function GeneratedSoundAction({project,shot}){const {t:lt,locale}=useI18n();
  const [confirm,setConfirm]=useState(false),option=generatedAudioOption(project,shot.id),chapterId=chapterForShot(project,shot.id);
  const existing=project.journey?.soundTracks?.[chapterId]?.find(t=>t.shotId===shot.id&&t.generatedFrom);
  return <div className="generated-sound-action"><button type="button" disabled={!!option.issue} onClick={()=>setConfirm(!confirm)}>{lt('handover.ui22')}</button>{option.issue?<p className="muted">{legacyReason(option.issue,locale)}</p>:<p className="muted">{lt('sound.independent')}{option.audio.data.simulation?lt('handover.ui23'):''}。{existing?lt('handover.ui24'):lt('handover.ui25')}</p>}
    {confirm&&!option.issue&&<div className="journey-soft-panel"><p>{lt('sound.addConfirm',{title:option.audio.title})}</p><div className="cloud-actions"><button type="button" onClick={()=>{if(store.editProject(p=>{upsertGeneratedSound(p,shot.id);})){setConfirm(false);store.notify(lt('handover.ui26'));}}}>{lt('handover.ui27')}</button><button type="button" onClick={()=>setConfirm(false)}>{lt('handover.ui28')}</button></div></div>}
  </div>;
}
