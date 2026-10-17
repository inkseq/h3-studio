import React,{useLayoutEffect,useRef,useState} from 'react';
import {useI18n} from './i18n/I18nProvider.jsx';

// Remount the edit buffer on an identity OR authoritative-value change. An old
// blur handler must never acquire the new conversation's save callback.
export default function TakeCount(props){
  return <TakeCountEditor key={JSON.stringify([props.contextKey,props.value])} {...props}/>;
}
function TakeCountEditor({value,maximum,disabled,onSave,isCurrent=()=>true}){
  const {t}=useI18n(),[draft,setDraft]=useState(String(value)),[error,setError]=useState(''),active=useRef(false),saving=useRef(false);
  useLayoutEffect(()=>{active.current=true;return()=>{active.current=false;};},[]);
  async function save(){
    if(!active.current||saving.current||disabled||!isCurrent())return;
    const n=Number(draft);
    if(!Number.isInteger(n)||n<1||n>maximum){setError(t('takes.invalidPreferences'));return;}
    setError('');if(n===value)return;
    saving.current=true;
    try{await onSave(n,()=>active.current&&isCurrent());}
    finally{saving.current=false;}
  }
  return <label className="qc-card-count">{t('workspace.copiesWord')}<input type="number" min="1" step="1" max={maximum} aria-label={t('workspace.copy83')} value={draft} disabled={disabled} onChange={e=>setDraft(e.target.value)} onBlur={save} onKeyDown={e=>{if(e.key==='Enter'){e.preventDefault();e.currentTarget.blur();}}}/>{error&&<small role="alert">{error}</small>}</label>;
}
