import {cloudController as cloud} from './cloud-controller.js';
import CreationHistory from './CreationHistory.jsx';
import {createQuickChatController} from './quick-chat-controller.js';
import React,{createContext,useContext,useEffect,useMemo,useRef,useState,useSyncExternalStore} from 'react';
import {createCloudClient} from './cloud-client.js';
import {createTrashController,trashTargetKey,trashErrorKey} from './trash-commands.js';
import {useI18n} from './i18n/I18nProvider.jsx';
import {ChatDialog} from './QuickChatControls.jsx';
import {store} from './store.js';
import ItemMenu from './ItemMenu.jsx';
import './trash.css';
const Context=createContext(null);
export const useTrash=()=>useContext(Context);
export function TrashProvider({account,children}){
 const {t}=useI18n(),identity=useRef(account);identity.current=account;
 const [busy,setBusy]=useState(false),[error,setError]=useState(''),[preview,setPreview]=useState(null),[notice,setNotice]=useState(null),[revision,setRevision]=useState(0);
 const api=useMemo(()=>{const client=createCloudClient();client.setAccount(account);return client;},[account]);
 const controller=useMemo(()=>createTrashController({api,account,getAccount:()=>identity.current,storage:localStorage}),[account,api]);
 useEffect(()=>{setPreview(null);setNotice(null);setError('');setBusy(false);setRevision(v=>v+1);},[account]);
 let pending=null;try{pending=controller.pending();}catch{pending={};}
 async function run(fn){if(!account||busy)return;const original=account;setBusy(true);setError('');try{const result=await fn();if(identity.current!==original)return;setRevision(v=>v+1);return result;}catch(e){if(identity.current===original)setError(t(trashErrorKey(e)));}finally{if(identity.current===original)setBusy(false);}}
 async function completed(receipt,current=store.getState()){
  if(identity.current!==account)return;
  setPreview(null);setNotice({action:receipt.action,targets:receipt.items.map(row=>row.target)});
  window.dispatchEvent(new CustomEvent('inkseq-trash-changed',{detail:receipt}));
  if(receipt.items.some(row=>row.target.kind==='project'))void cloud.list({replace:true}).catch(()=>{});
  const targets=receipt.items.map(row=>row.target),fresh=()=>identity.current===account&&store.getState().workspaceEpoch===current.workspaceEpoch&&!store.getState().workspace.dirty;
  if(current.workspace.mode!=='cloud'||!fresh()||!targets.some(row=>row.project_id===current.project.id))return;
  if(receipt.action==='trash'&&targets.some(row=>row.kind==='project'&&row.id===current.project.id)){store.leaveCloudProject();return;}
  try{const updated=await api.request('/v1/projects/'+encodeURIComponent(current.project.id));if(fresh())store.applyCloudCommand(updated.project,{account,expectedVersion:current.workspace.serverVersion,version:updated.version,expectedProject:current.project});}catch{/* Preserve accepted receipt and any newer local edits. */}
 }
 async function request(action,targets){return run(async()=>{const current=store.getState();if(current.workspace.mode==='cloud'&&current.workspace.dirty&&targets.some(row=>row.project_id===current.project.id))throw Error('trash.unsaved');const value=await controller.preview(action,targets);if(action==='purge'){setPreview(value);return;}const receipt=await controller.start(value);if(receipt.state==='completed')await completed(receipt,current);return receipt;});}
 const value={account,controller,busy,pending,revision,request,run};
 return <Context.Provider value={value}>{children}{error&&<div className="trash-notice" role="alert">{error}<button onClick={()=>setError('')}>{t('trash.close')}</button></div>}{pending&&<div className="trash-notice" role="status"><span>{t('trash.pending')}</span><button disabled={busy} onClick={()=>run(async()=>{const r=await controller.recover();if(r.state==='completed')await completed(r);})}>{t('trash.check')}</button>{pending.operationId&&<button disabled={busy} onClick={()=>run(async()=>{const r=await controller.resume();if(r.state==='completed')await completed(r);})}>{t('trash.resume')}</button>}</div>}{notice&&!pending&&!error&&<div className="trash-notice" role="status"><span>{t(notice.action==='trash'?'trash.moved':notice.action==='restore'?'trash.restored':'trash.deleted')}</span>{notice.action==='trash'&&<button disabled={busy} onClick={()=>request('restore',notice.targets)}>{t('trash.undo')}</button>}<button onClick={()=>setNotice(null)}>{t('trash.close')}</button></div>}{preview&&<ChatDialog title={t('trash.confirm')} onClose={()=>{if(!busy)setPreview(null);}}><p>{t('trash.irreversible')}</p><ul>{preview.items.map(row=><li key={trashTargetKey(row.target)}>{row.title||t('trash.untitled')}</li>)}</ul><p>{t('trash.summary',{removed:preview.removed?.objects??0,bytes:preview.removed?.bytes??0,kept:preview.kept?.objects??0})}</p><button disabled={busy} onClick={()=>setPreview(null)}>{t('trash.cancel')}</button><button className="danger" disabled={busy} onClick={()=>run(async()=>{const r=await controller.start(preview);setPreview(null);if(r.state==='completed')await completed(r);})}>{t('trash.purge')}</button></ChatDialog>}</Context.Provider>;
}
export function TrashMenu({target,onRename,onEdit,onTrashSettled,disabled=false}){const ctx=useTrash(),{t}=useI18n();if(!ctx?.account)return null;return <ItemMenu disabled={disabled||ctx.busy||!!ctx.pending}>{close=><>{onRename&&<button role="menuitem" onClick={()=>{close();onRename();}}>{t('trash.rename')}</button>}{onEdit&&<button role="menuitem" onClick={()=>{close();onEdit();}}>{t('trash.edit')}</button>}<button role="menuitem" onClick={async event=>{const dialog=event.currentTarget.closest('dialog');close();const receipt=await ctx.request('trash',[target]);if(dialog?.isConnected){if(onTrashSettled)onTrashSettled();else if(receipt?.state==='completed')dialog.dispatchEvent(new Event('inkseq-close-after-trash'));}}}>{t('trash.move')}</button></>}</ItemMenu>;}
export default function UniversalTrash(){
 const ctx=useTrash(),{t}=useI18n(),[rows,setRows]=useState([]),[next,setNext]=useState(null),[kind,setKind]=useState(''),[selected,setSelected]=useState([]),[loading,setLoading]=useState(false),[error,setError]=useState(''),epoch=useRef(0);
 const load=async(append=false)=>{const n=++epoch.current;setLoading(true);setError('');try{const data=await ctx.controller.list(kind,append?next:0);if(n!==epoch.current)return;setRows(old=>append?[...old,...data.items.filter(x=>!old.some(y=>y.trash_id===x.trash_id))]:data.items);setNext(data.next_offset);if(!append)setSelected([]);}catch{if(n===epoch.current)setError(t('trash.failed'));}finally{if(n===epoch.current)setLoading(false);}};
 useEffect(()=>{setRows([]);setSelected([]);if(ctx?.account&&kind!=='conversation')load();return()=>{epoch.current++;};},[ctx?.account,ctx?.revision,kind]);
 if(!ctx?.account)return <main className="trash-page"><h1>{t('trash.title')}</h1><p>{t('trash.signIn')}</p><a href="/quick-chat">{t('trash.signIn')}</a></main>;
 if(kind==='conversation')return <main className="trash-page"><h1>{t('trash.title')}</h1><button onClick={()=>setKind('')}>{t('trash.all')}</button><ConversationTrash key={ctx.account} account={ctx.account} busy={ctx.busy||!!ctx.pending}/></main>;
 const targets=rows.filter(row=>selected.includes(row.trash_id)).map(row=>row.target),locked=loading||ctx.busy||!!ctx.pending;
 return <main className="trash-page"><h1>{t('trash.title')}</h1><p>{t('trash.retention')}</p><button onClick={()=>setKind('conversation')}>{t('trash.conversations')}</button><div className="trash-toolbar"><select value={kind} onChange={e=>setKind(e.target.value)} aria-label={t('trash.all')}><option value="">{t('trash.all')}</option>{['conversation','project','chapter','scene','shot','character','location','prop','look','location_version','image','video','audio','asset','artifact','take'].map(k=><option key={k} value={k}>{t('trash.kind.'+k)}</option>)}</select><button disabled={locked} onClick={()=>load()}>{t('trash.refresh')}</button><button disabled={locked} onClick={()=>setSelected(rows.filter(r=>r.state==='trashed').map(r=>r.trash_id))}>{t('trash.selectAll')}</button><button disabled={locked||!targets.length} onClick={()=>ctx.request('restore',targets)}>{t('trash.restore')}</button><button disabled={locked||!targets.length} onClick={()=>ctx.request('purge',targets)}>{t('trash.purge')}</button><button disabled={locked||!rows.length} onClick={()=>ctx.run(async()=>{const all=[...rows];let offset=next;const seen=new Set();while(offset!==null){if(seen.has(offset))throw Error('trash.failed');seen.add(offset);const page=await ctx.controller.list(kind,offset);all.push(...page.items);offset=page.next_offset;}return all.filter(r=>r.state==='trashed').map(r=>r.target);}).then(value=>{if(value)ctx.request('purge',value);})}>{t(kind?'trash.emptyFiltered':'trash.empty')}</button></div>{error&&<p role="alert">{error}</p>}{!rows.length&&!loading&&<p>{t('trash.none')}</p>}{rows.map(row=><div className="trash-row" key={row.trash_id}><input type="checkbox" disabled={locked||row.state!=='trashed'} checked={selected.includes(row.trash_id)} aria-label={row.title||t('trash.untitled')} onChange={e=>setSelected(old=>e.target.checked?[...old,row.trash_id]:old.filter(id=>id!==row.trash_id))}/><div><b>{row.title||t('trash.untitled')}</b><small> · {t('trash.kind.'+row.kind)} · {row.state==='purging'?t('trash.pending'):t('trash.moved')}</small></div><button disabled={locked||row.state!=='trashed'} onClick={()=>ctx.request('restore',[row.target])}>{t('trash.restore')}</button></div>)}{next!==null&&<button disabled={locked} onClick={()=>load(true)}>{t('trash.more')}</button>}</main>;
}

export function StoryItemManager({state,onClose,onEdit}){
 const {t}=useI18n(),[filter,setFilter]=useState('');const p=state.project;
 return <ChatDialog title={t('trash.manage')} onClose={onClose}><input type="search" aria-label={t('trash.search')} value={filter} onChange={e=>setFilter(e.target.value)}/>{p.entities.filter(e=>(e.title||'').toLocaleLowerCase().includes(filter.toLocaleLowerCase())).map(e=><div className="trash-row" key={e.id}><div><b>{e.title}</b><small> · {t('trash.kind.'+e.type)}</small></div><TrashMenu target={{kind:'entity',project_id:p.id,id:e.id}} onTrashSettled={onClose} onEdit={()=>onEdit(e.id)} onRename={()=>onEdit(e.id)}/></div>)}</ChatDialog>;
}

function ConversationTrash({account,busy}){
 const controller=useMemo(()=>createQuickChatController(),[]),state=useSyncExternalStore(controller.subscribe,controller.getState,controller.getState);
 useEffect(()=>{let active=true;controller.setAccount(account).then(()=>{if(active&&controller.getState().account===account)return controller.historyView('trash');}).catch(()=>{});return()=>{active=false;controller.destroy();};},[account,controller]);
 return <CreationHistory state={state} controller={controller} busy={busy||state.busy} embeddedTrash onSelect={()=>{}}/>;
}

export function RenameDialog({title,onSave,onClose}){
 const {t}=useI18n(),[value,setValue]=useState(title||''),[saving,setSaving]=useState(false),[error,setError]=useState(''),alive=useRef(true);
 useEffect(()=>()=>{alive.current=false;},[]);
 return <ChatDialog title={t('trash.rename')} onClose={()=>{if(!saving)onClose();}}><form onSubmit={async e=>{e.preventDefault();if(saving)return;setSaving(true);setError('');try{await onSave(value);if(alive.current)onClose();}catch{if(alive.current)setError(t('trash.failed'));}finally{if(alive.current)setSaving(false);}}}><label>{t('trash.name')}<input value={value} onChange={e=>setValue(e.target.value)} disabled={saving}/></label>{error&&<p role="alert">{error}</p>}<button type="button" disabled={saving} onClick={onClose}>{t('trash.cancel')}</button><button disabled={saving||!value.trim()}>{t('trash.save')}</button></form></ChatDialog>;
}
