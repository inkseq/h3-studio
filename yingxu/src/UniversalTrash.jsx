import StudioHeader from './StudioHeader.jsx';
import {cloudController as cloud} from './cloud-controller.js';
import CreationHistory from './CreationHistory.jsx';
import {createQuickChatClient} from './quick-chat-client.js';
import {createQuickChatController} from './quick-chat-controller.js';
import React,{createContext,useContext,useEffect,useMemo,useRef,useState,useSyncExternalStore} from 'react';
import {createCloudClient} from './cloud-client.js';
import {createTrashController,trashTargetKey,trashErrorKey} from './trash-commands.js';
import {useI18n} from './i18n/I18nProvider.jsx';
import {ChatDialog} from './QuickChatControls.jsx';
import {store} from './store.js';
import ItemMenu from './ItemMenu.jsx';
import {BookOpen,Box,Clapperboard,Film,Image,Layers,MapPin,Music,RotateCcw,Trash2,UserRound,SlidersHorizontal} from 'lucide-react';
import {usePageActive,useAppLocation} from './AppRouteCache.jsx';
import {navigateApp} from './app-navigation.js';
import './v2/ops-adapter.css';
const Context=createContext(null);
const trashIcon=kind=>({project:BookOpen,chapter:BookOpen,scene:Clapperboard,shot:Clapperboard,character:UserRound,look:UserRound,location:MapPin,location_version:MapPin,prop:Box,image:Image,video:Film,audio:Music,artifact:Layers,take:Layers})[kind]||Box;
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
 return <Context.Provider value={value}>{children}{error&&<div className="trash-notice" role="alert">{error}<button onClick={()=>setError('')}>{t('trash.close')}</button></div>}{pending&&<div className="trash-notice" role="status"><span>{t('trash.pending')}</span><button disabled={busy} onClick={()=>run(async()=>{const r=await controller.recover();if(r.state==='completed')await completed(r);})}>{t('trash.check')}</button>{pending.operationId&&<button disabled={busy} onClick={()=>run(async()=>{const r=await controller.resume();if(r.state==='completed')await completed(r);})}>{t('trash.resume')}</button>}</div>}{notice&&!pending&&!error&&<div className="trash-notice" role="status"><span>{t(notice.action==='trash'?'trash.moved':notice.action==='restore'?'trash.restored':'trash.deleted')}</span>{notice.action==='trash'&&<button disabled={busy} onClick={()=>request('restore',notice.targets)}>{t('trash.undo')}</button>}<button onClick={()=>setNotice(null)}>{t('trash.close')}</button></div>}{preview&&<ChatDialog className="ops-dialog" title={t('trash.confirm')} onClose={()=>{if(!busy)setPreview(null);}}><p>{t('trash.irreversible')}</p><ul>{preview.items.map(row=><li key={trashTargetKey(row.target)}>{row.title||t('trash.untitled')}</li>)}</ul><p>{t('trash.summary',{removed:preview.removed?.objects??0,bytes:preview.removed?.bytes??0,kept:preview.kept?.objects??0})}</p><button disabled={busy} onClick={()=>setPreview(null)}>{t('trash.cancel')}</button><button className="danger" disabled={busy} onClick={()=>run(async()=>{const r=await controller.start(preview);setPreview(null);if(r.state==='completed')await completed(r);})}>{t('trash.purge')}</button></ChatDialog>}</Context.Provider>;
}
export function TrashMenu({target,onRename,onEdit,onTrashSettled,disabled=false}){const ctx=useTrash(),{t}=useI18n();if(!ctx?.account)return null;return <ItemMenu disabled={disabled||ctx.busy||!!ctx.pending}>{close=><>{onRename&&<button role="menuitem" onClick={()=>{close();onRename();}}>{t('trash.rename')}</button>}{onEdit&&<button role="menuitem" onClick={()=>{close();onEdit();}}>{t('trash.edit')}</button>}<button role="menuitem" onClick={async event=>{const dialog=event.currentTarget.closest('dialog');close();const receipt=await ctx.request('trash',[target]);if(dialog?.isConnected){if(onTrashSettled)onTrashSettled();else if(receipt?.state==='completed')dialog.dispatchEvent(new Event('inkseq-close-after-trash'));}}}>{t('trash.move')}</button></>}</ItemMenu>;}
export default function UniversalTrash(){
 const ctx=useTrash(),{t,locale}=useI18n(),pageActive=usePageActive(),location=useAppLocation(),[rows,setRows]=useState([]),[next,setNext]=useState(null),[kind,setKind]=useState(''),[group,setGroup]=useState('all'),[selected,setSelected]=useState([]),[loading,setLoading]=useState(false),[quickCount,setQuickCount]=useState(null),[error,setError]=useState(''),epoch=useRef(0);
 useEffect(()=>{if(pageActive)document.title=t('trash.title');},[pageActive,t]);
 useEffect(()=>{if(pageActive&&new URLSearchParams(location.search).get('kind')==='conversation'){setGroup('session');setKind('conversation');setSelected([]);}},[pageActive,location.search]);
 const account=ctx?.account,revision=ctx?.revision;
 useEffect(()=>{
  let live=true;setQuickCount(null);if(!account)return;
  const client=createQuickChatClient();client.setAccount(account);
  client.sessions({view:'trash'}).then(value=>{if(live&&Array.isArray(value.sessions))setQuickCount({account,count:value.sessions.length,more:!!value.next_cursor});}).catch(()=>{});
  return()=>{live=false;client.reset();};
 },[account,revision]);
 const receiveQuickCount=(count,more)=>setQuickCount(previous=>previous?.account===account&&previous.count===count&&previous.more===more?previous:{account,count,more});
 const load=async(append=false)=>{const n=++epoch.current;setLoading(true);setError('');try{const data=await ctx.controller.list(kind,append?next:0);if(n!==epoch.current)return;setRows(old=>append?[...old,...data.items.filter(x=>!old.some(y=>y.trash_id===x.trash_id))]:data.items);setNext(data.next_offset);if(!append)setSelected([]);}catch{if(n===epoch.current)setError(t('trash.failed'));}finally{if(n===epoch.current)setLoading(false);}};
 useEffect(()=>{setRows([]);setSelected([]);if(ctx?.account&&kind!=='conversation')load();return()=>{epoch.current++;};},[ctx?.account,ctx?.revision,kind]);
 const inGroup=(row,value=group)=>value==='all'||value==='story'&&['project','chapter','scene','shot','artifact'].includes(row.kind)||value==='lib'&&['character','location','prop','look','location_version','image','video','audio','asset'].includes(row.kind)||value==='session'&&row.kind==='take';
 const items=rows.filter(row=>inGroup(row)),targets=items.filter(row=>selected.includes(row.trash_id)).map(row=>row.target),locked=loading||ctx?.busy||!!ctx?.pending;
 const changeGroup=value=>{const nextKind=value==='session'?'conversation':'';setGroup(value);setKind(nextKind);setSelected([]);const url=new URL(window.location.href);if(nextKind)url.searchParams.set('kind',nextKind);else url.searchParams.delete('kind');navigateApp(url.pathname+url.search,{replace:true,restore:false,scroll:false});};
 const empty=()=>ctx.run(async()=>{const all=[...rows];let offset=next;const seen=new Set();while(offset!==null){if(seen.has(offset))throw Error('trash.failed');seen.add(offset);const page=await ctx.controller.list(kind,offset);all.push(...page.items);offset=page.next_offset;}return all.filter(r=>r.state==='trashed'&&inGroup(r)).map(r=>r.target);}).then(value=>{if(value?.length)ctx.request('purge',value);});
 return <div className="home ops-adapter"><StudioHeader account={ctx?.account} active="trash"/><main className="trash-page">
   <div className="trash-head"><h1 className="page-title"><Trash2 size={20}/>{t('trash.title')}<small>{ctx?.account&&kind!=='conversation'?rows.length+(next!==null?'+':''):''}</small></h1><div className="spacer"/>{ctx?.account&&kind!=='conversation'&&<button className="ghost small danger-text" disabled={locked||!rows.length} onClick={empty}>{t(kind||group!=='all'?'trash.emptyFiltered':'trash.empty')}</button>}</div>
   <p className="muted">{t('trash.retention')}</p>
   {!ctx?.account?<div className="empty-state"><span><Trash2 size={22}/></span><b>{t('trash.signIn')}</b><a href="/quick-chat">{t('trash.signIn')}</a></div>:<>
     <div className="seg trash-filter">{[['all',t('trash.all')],['story',locale==='zh'?'故事和镜头':'Stories and shots'],['lib',t('library.title')],['session',t('nav.quickCreate')]].map(([id,label])=><button key={id} className={group===id?'on':''} onClick={()=>changeGroup(id)}>{label}<span className="trash-tab-count">{id==='session'?quickCount?.account===account?quickCount.count+(quickCount.more?'+':''):'—':rows.filter(row=>inGroup(row,id)).length+(next!==null?'+':'')}</span></button>)}</div>
     {kind==='conversation'?<div className="ops-conversation-trash"><ConversationTrash key={ctx.account} account={ctx.account} busy={ctx.busy||!!ctx.pending} onCount={receiveQuickCount}/></div>:<>
       <details className="ops-tools"><summary><SlidersHorizontal size={14}/>{t('trash.filters')}</summary><div className="row tight"><select aria-label={t('trash.all')} value={kind} onChange={e=>{setKind(e.target.value);setGroup('all');}}>{['','project','chapter','scene','shot','character','location','prop','look','location_version','image','video','audio','asset','artifact','take'].map(k=><option key={k} value={k}>{t(k?'trash.kind.'+k:'trash.all')}</option>)}</select><button className="small" disabled={locked} onClick={()=>load()}>{t('trash.refresh')}</button><button className="small" disabled={locked} onClick={()=>setSelected(items.filter(r=>r.state==='trashed').map(r=>r.trash_id))}>{t('trash.selectAll')}</button></div></details>
       {error&&<p role="alert">{error}</p>}
       {items.length?<div className="trash-list" aria-busy={loading}>{targets.length>0&&<div className="trash-bar" role="status">{locale==='zh'?`已选 ${targets.length} 项`:`${targets.length} selected`}<div className="spacer"/><button className="small" disabled={locked} onClick={()=>ctx.request('restore',targets)}><RotateCcw size={13}/>{t('trash.restore')}</button><button className="small danger" disabled={locked} onClick={()=>ctx.request('purge',targets)}>{t('trash.purge')}</button></div>}
         {items.map(row=>{const Icon=trashIcon(row.kind);return <div className="trash-row" key={row.trash_id}>
           <input type="checkbox" disabled={locked||row.state!=='trashed'} checked={selected.includes(row.trash_id)} aria-label={row.title||t('trash.untitled')} onChange={e=>setSelected(old=>e.target.checked?[...old,row.trash_id]:old.filter(id=>id!==row.trash_id))}/>
           <span className="thumb" aria-hidden="true"><Icon size={18}/></span>
           <div className="name"><b className="clip" title={row.title}>{row.title||t('trash.untitled')}</b><small>{t('trash.kind.'+row.kind)} · {row.state==='purging'?t('trash.pending'):t('trash.moved')}</small></div>
           <span className="left" title={t('trash.moved')}>{row.trashed_at?new Date(row.trashed_at*1000).toLocaleDateString(locale==='zh'?'zh-CN':'en'):'—'}</span>
           <div className="acts"><button className="small" disabled={locked||row.state!=='trashed'} onClick={()=>ctx.request('restore',[row.target])}><RotateCcw size={13}/>{t('trash.restore')}</button><button className="small ghost danger" title={t('trash.purge')} aria-label={t('trash.purge')} disabled={locked||row.state!=='trashed'} onClick={()=>ctx.request('purge',[row.target])}><Trash2 size={13}/></button></div>
         </div>;})}
       </div>:!error&&<div className="empty-state" role={loading?'status':undefined}><span><Trash2 size={22}/></span><b>{loading?t('settings.loading'):t('trash.none')}</b></div>}
       {next!==null&&<button className="small" disabled={locked} onClick={()=>load(true)}>{t('trash.more')}</button>}
     </>}
   </>}
 </main></div>;
}

export function StoryItemManager({state,onClose,onEdit}){
 const {t}=useI18n(),[filter,setFilter]=useState('');const p=state.project;
 return <ChatDialog className="ops-dialog" title={t('trash.manage')} onClose={onClose}><input type="search" aria-label={t('trash.search')} value={filter} onChange={e=>setFilter(e.target.value)}/>{p.entities.filter(e=>(e.title||'').toLocaleLowerCase().includes(filter.toLocaleLowerCase())).map(e=><div className="ops-item-row" key={e.id}><div><b>{e.title}</b><small> · {t('trash.kind.'+e.type)}</small></div><TrashMenu target={{kind:'entity',project_id:p.id,id:e.id}} onTrashSettled={onClose} onEdit={()=>onEdit(e.id)} onRename={()=>onEdit(e.id)}/></div>)}</ChatDialog>;
}

function ConversationTrash({account,busy,onCount}){
 const controller=useMemo(()=>createQuickChatController(),[]),state=useSyncExternalStore(controller.subscribe,controller.getState,controller.getState);
 useEffect(()=>{if(state.account===account&&state.historyReceipt?.state==='completed')window.dispatchEvent(new CustomEvent('inkseq-quick-history-changed',{detail:{account}}));},[account,state.account,state.historyReceipt?.action_id,state.historyReceipt?.state]);
 useEffect(()=>{let active=true;controller.setAccount(account).then(()=>{if(active&&controller.getState().account===account)return controller.historyView('trash');}).catch(()=>{});return()=>{active=false;controller.destroy();};},[account,controller]);
 useEffect(()=>{if(state.account===account&&state.historyView==='trash'&&state.trashAsOf!==null)onCount?.(state.trashSessions.length,!!state.trashCursor);},[state.account,state.historyView,state.trashAsOf,state.trashSessions,state.trashCursor,account]);
 return <CreationHistory state={state} controller={controller} busy={busy||state.busy} embeddedTrash onSelect={()=>{}}/>;
}

export function RenameDialog({title,onSave,onClose}){
 const {t}=useI18n(),[value,setValue]=useState(title||''),[saving,setSaving]=useState(false),[error,setError]=useState(''),alive=useRef(true);
 useEffect(()=>()=>{alive.current=false;},[]);
 return <ChatDialog className="ops-dialog" title={t('trash.rename')} onClose={()=>{if(!saving)onClose();}}><form onSubmit={async e=>{e.preventDefault();if(saving)return;setSaving(true);setError('');try{await onSave(value);if(alive.current)onClose();}catch{if(alive.current)setError(t('trash.failed'));}finally{if(alive.current)setSaving(false);}}}><label>{t('trash.name')}<input value={value} onChange={e=>setValue(e.target.value)} disabled={saving}/></label>{error&&<p role="alert">{error}</p>}<button type="button" disabled={saving} onClick={onClose}>{t('trash.cancel')}</button><button disabled={saving||!value.trim()}>{t('trash.save')}</button></form></ChatDialog>;
}
