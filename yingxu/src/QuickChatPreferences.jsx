import React,{useState} from 'react';
import {useI18n} from './i18n/I18nProvider.jsx';
import {preferenceBody,takeError} from './quick-chat-takes.js';
export default function QuickChatPreferences({controller,value,onSaved,busy}){
 const {t}=useI18n(),[copies,setCopies]=useState(String(value.copies_max)),[fold,setFold]=useState(String(value.takes_fold_count)),[error,setError]=useState('');
 async function save(event){event.preventDefault();setError('');try{const result=await controller.savePreferences(preferenceBody(value,copies,fold));onSaved(result);}catch(e){setError(takeError(e,t));}}
 return <form onSubmit={save}><p>{t('takes.preferencesHelp')}</p><label className="qc-field">{t('takes.copiesMax')}<input type="number" min="1" step="1" max="2147483647" value={copies} disabled={busy} onChange={e=>setCopies(e.target.value)}/></label><label className="qc-field">{t('takes.foldCount')}<input type="number" min="1" step="1" max="2147483647" value={fold} disabled={busy} onChange={e=>setFold(e.target.value)}/></label>{error&&<p role="alert" className="qc-error">{error}</p>}<button type="submit" disabled={busy} className="qc-primary">{t('takes.save')}</button></form>;
}
