import {dstackNodeStatus as rawDstackNodeStatus} from './dstack-presentation.js';
import {operatorFormatters,operatorDisplay} from './i18n/legacyOperatorPresentation.js';
import {useI18n,LanguageSwitcher} from './i18n/I18nProvider.jsx';
import './i18n/legacy-operator-language.css';
import React,{useEffect,useMemo,useRef,useState,useSyncExternalStore} from 'react';
import {Clock,Info,Play,Plus,RefreshCw,Server,X} from 'lucide-react';
import {createOperatorController} from './operator-controller.js';
import {createDstackClient} from './dstack-client.js';
import {age,dollars,duration} from './operator-model.js';
import {dstackGroups,dstackKeepOnSelection,dstackLifecycle,dstackLifetime,dstackMode,dstackNodeStatus,dstackPreviewCurrent,dstackProvider} from './dstack-presentation.js';
import './dstack.css';

const quietly=promise=>promise.catch(()=>{});
const date=value=>typeof value==='number'&&Number.isFinite(value)?new Date(value*1000).toLocaleString('zh-CN'):'尚未确认';
function Dialog({title,onClose,children}){const {t:lo,locale}=useI18n();
  const ref=useRef(),opener=useRef(document.activeElement);
  useEffect(()=>{ref.current.showModal();return()=>{ref.current?.close();opener.current?.isConnected&&opener.current.focus();};},[]);
  return <dialog ref={ref} className="op-dialog" aria-label={title} onCancel={event=>{event.preventDefault();onClose();}}><header><div><span className="op-eyebrow">GPU CONTROL</span><h2>{title}</h2></div><button className="op-icon" aria-label={lo('legacyOp.copy1')} onClick={onClose}><X size={19}/></button></header>{children}</dialog>;
}

function Start({profiles,lifecycleVersion,controller,busy,now,onClose}){const {t:lo,locale}=useI18n();const {date,duration,dollars,dstackProvider,dstackMode,dstackLifetime}=operatorFormatters(locale);
  const models=[...new Set(profiles.map(profile=>profile.model_id))];
  const [model,setModel]=useState(models[0]||''),[mode,setMode]=useState(profiles[0]?.mode||'fl'),[chosen,setChosen]=useState(profiles[0]?.id||''),[minutes,setMinutes]=useState(Math.min(60,(profiles[0]?.max_ttl_seconds||3600)/60)),[preview,setPreview]=useState(null),[error,setError]=useState(''),[checking,setChecking]=useState(false);
  const options=profiles.filter(profile=>profile.model_id===model&&profile.mode===mode),profile=options.find(item=>item.id===chosen)||options[0];
  const [keepOn,setKeepOn]=useState('idle'),continuing=lifecycleVersion===2,policy=dstackKeepOnSelection(keepOn,minutes);
  const seconds=minutes*60,valid=!!profile&&(continuing?!!policy:Number.isInteger(seconds)&&seconds>=120&&seconds<=profile.max_ttl_seconds);
  const current=dstackPreviewCurrent(preview,profile?.id,continuing?policy:seconds,now);
  function changeModel(value){const first=profiles.find(profile=>profile.model_id===value);setModel(value);setMode(first?.mode||'fl');setChosen(first?.id||'');setPreview(null);setError('');}
  async function check(){if(!valid)return;setChecking(true);setError('');setPreview(null);const selection={profile_id:profile.id,...(continuing?policy:{ttl_seconds:seconds})};try{const result=await controller.preview(selection);setPreview({...result,selection});}catch(error){setError(error.message);}finally{setChecking(false);}}
  async function start(){if(!current)return;setError('');try{await controller.start(preview);onClose();}catch(error){setError(error.message);}}
  return <Dialog title={lo('legacyOp.copy243')} onClose={onClose}>
    <fieldset disabled={busy||checking}><div className="op-model-choice"><label>{lo('legacyOp.copy244')}<select value={model} aria-label={lo('legacyOp.copy245')} onChange={event=>changeModel(event.target.value)}>{models.map(value=><option key={value} value={value}>{value}</option>)}</select></label><div><span className="op-field-label">{lo('legacyOp.copy4')}</span><div className="op-mode-options" role="group" aria-label={lo('legacyOp.copy246')}>{['fl','ref'].map(value=><button key={value} type="button" aria-pressed={mode===value} disabled={!profiles.some(profile=>profile.model_id===model&&profile.mode===value)} onClick={()=>{setMode(value);setChosen('');setPreview(null);setError('');}}>{dstackMode(value)}</button>)}</div></div></div>
      <label>{lo('legacyOp.copy247')}<select aria-label={lo('legacyOp.copy248')} value={profile?.id||''} onChange={event=>{setChosen(event.target.value);setPreview(null);setError('');}}>{options.map(profile=><option key={profile.id} value={profile.id}>{dstackProvider(profile.backend)} · {profile.gpu_names.join(' / ')} × 1</option>)}</select></label>
      {profile&&<p className="op-note">{lo('legacyOp.copy69')} {profile.memory_gib} {lo('legacyOp.copy249')} {profile.disk_gib} {lo('legacyOp.copy250')} {profile.gpu_memory_gib} {lo('legacyOp.copy251')}</p>}
      {continuing&&<label>{lo('legacyOp.copy252')}<select aria-label={lo('legacyOp.copy253')} value={keepOn} onChange={event=>{setKeepOn(event.target.value);setPreview(null);setError('');}}><option value="idle">{lo('legacyOp.copy254')}</option><option value="until">{lo('legacyOp.copy255')}</option><option value="until_stopped">{lo('legacyOp.copy256')}</option></select></label>}
      {(!continuing||keepOn==='until')&&<label>{continuing?lo('legacyOp.copy257'):lo('legacyOp.copy138')}<div className="op-unit-input"><input type="number" min={continuing?1:2} max={continuing?1440:profile?profile.max_ttl_seconds/60:undefined} step="1" value={minutes} onChange={event=>{setMinutes(Number(event.target.value));setPreview(null);setError('');}}/><span>{lo('legacyOp.copy36')}</span></div></label>}
      <p className="op-note">{continuing?lo('legacyOp.copy258'):lo('legacyOp.copy259')}{lo('legacyOp.copy260')}</p><button disabled={!valid} onClick={check}><RefreshCw size={14}/>{checking?lo('legacyOp.copy261'):lo('legacyOp.copy262')}</button>
    </fieldset>
    {preview&&<div className="op-preview"><b>{current?lo('legacyOp.copy111'):lo('legacyOp.copy263')}</b><dl><dt>{lo('legacyOp.copy264')}</dt><dd>{dollars(preview.hourly_cost_microusd,{digits:3})} {lo('legacyOp.copy114')}</dd><dt>{continuing?lo('legacyOp.copy265'):lo('legacyOp.copy266')}</dt><dd>{dollars(preview.reservation_microusd)}</dd><dt>{continuing?lo('legacyOp.copy267'):lo('legacyOp.copy268')}</dt><dd>{continuing?dstackLifetime(preview):date(preview.hard_deadline)}</dd>{dstackLifecycle(preview)&&<><dt>{lo('legacyOp.copy269')}</dt><dd>{duration(preview.initial_reservation_seconds)}{lo('legacyOp.copy270')}</dd></>}<dt>{lo('legacyOp.copy271')}</dt><dd>{date(preview.expires_at)}</dd></dl><p className="op-note">{lo('legacyOp.copy272')}{continuing?lo('legacyOp.copy273'):''}{lo('legacyOp.copy274')}</p></div>}
    {error&&<p className="op-error" role="alert">{error}</p>}<footer className="op-dialog-actions"><button onClick={onClose}>{lo('legacy.panel36')}</button><button className="op-primary" disabled={busy||checking||!current} onClick={start}><Play size={14}/>{busy?lo('legacyOp.copy125'):lo('legacyOp.copy126')}</button></footer>
  </Dialog>;
}

function NodeDialog({node,controller,busy,now,onClose}){const {t:lo,locale}=useI18n();const {date,age,dollars,dstackNodeStatus,dstackProvider,dstackMode,dstackLifetime}=operatorFormatters(locale);
  const [action,setAction]=useState(''),[minutes,setMinutes]=useState(10),[error,setError]=useState(''),[keepOn,setKeepOn]=useState(node.lifecycle?.keep_on||'idle');
  const lifecycle=dstackLifecycle(node),continuing=!!lifecycle,policy=dstackKeepOnSelection(keepOn,minutes);
  const status=dstackNodeStatus(node,now),left=typeof node.hard_deadline==='number'?Math.max(0,(node.hard_deadline*1000-now)/1000):0;
  const canHold=node.desired_state==='running'&&node.state!=='stopped'&&(continuing||!node.lifecycle&&left>0),canStop=node.desired_state==='running'&&node.state!=='stopped';
  const holdValid=canHold&&(continuing?!!policy:Number.isInteger(minutes)&&minutes>=1&&minutes*60<=left);
  async function submit(){setError('');try{if(action==='stop')await controller.stop(node);else await controller.hold(node,continuing?policy:minutes*60);onClose();}catch(error){setError(error.message);}}
  return <Dialog title={lo('legacyOp.copy275')} onClose={onClose}><span className={'op-badge '+status.tone}>{status.label}</span><p>{dstackProvider(node.provider)} · {dstackMode(node.mode)}</p><dl className="op-dstack-facts"><dt>{lo('legacyOp.copy276')}</dt><dd><code>{node.id}</code></dd><dt>{lo('legacyOp.copy277')}</dt><dd><code>{node.runtime_profile_id}</code></dd><dt>{lo('legacyOp.copy278')}</dt><dd>{age(node.observed_at,now)}</dd><dt>{continuing?lo('legacyOp.copy267'):lo('legacyOp.copy268')}</dt><dd>{continuing?dstackLifetime(node):date(node.hard_deadline)}</dd>{!continuing&&<><dt>{lo('legacyOp.copy279')}</dt><dd>{node.hold_until?date(node.hold_until):lo('legacyOp.copy280')}</dd></>}<dt>{lo('legacyOp.copy281')}</dt><dd>{dollars(node.hourly_cost_microusd,{digits:3})} {lo('legacyOp.copy114')}</dd></dl>
    <p className="op-note">{lo('legacyOp.copy282')}{node.billing_state!=='no_charge_confirmed'?lo('legacyOp.copy283'):lo('legacyOp.copy284')}</p>{node.reason_code&&<details className="op-details"><summary>{lo('legacyOp.copy285')}</summary><code>{node.reason_code}</code></details>}
    {canHold&&<div className="op-dstack-hold">{continuing&&<label>{lo('legacyOp.copy252')}<select aria-label={lo('legacyOp.copy253')} value={keepOn} onChange={event=>{setKeepOn(event.target.value);setAction('');}}><option value="idle">{lo('legacyOp.copy254')}</option><option value="until">{lo('legacyOp.copy255')}</option><option value="until_stopped">{lo('legacyOp.copy256')}</option></select></label>}{(!continuing||keepOn==='until')&&<label>{lo('legacyOp.copy286')}<div className="op-unit-input"><input type="number" min="1" max={continuing?1440:Math.floor(left/60)} step="1" value={minutes} onChange={event=>{setMinutes(Number(event.target.value));setAction('');}}/><span>{lo('legacyOp.copy36')}</span></div></label>}<small>{continuing?lo('legacyOp.copy287'):lo('legacyOp.copy288')}</small></div>}
    <div className="op-dstack-actions"><button disabled={busy||!holdValid} onClick={()=>{setAction('hold');setError('');}}><Clock size={14}/>{continuing?lo('legacyOp.copy289'):lo('legacyOp.copy290')}</button><button disabled={busy||!canStop} onClick={()=>{setAction('stop');setError('');}}>{lo('legacyOp.copy208')}</button></div>
    {action&&<div className="op-preview"><b>{action==='stop'?lo('legacyOp.copy291'):continuing?keepOn==='until_stopped'?lo('legacyOp.copy292'):keepOn==='idle'?lo('legacyOp.copy293'):lo('legacyOp.copy294',{x0:minutes}):lo('legacyOp.copy295',{x0:minutes})}</b><p className="op-note">{action==='stop'?lo('legacyOp.copy296'):continuing?lo('legacyOp.copy297',{x0:dollars(node.hourly_cost_microusd,{digits:3})}):lo('legacyOp.copy298',{x0:date(node.hard_deadline)})}</p><button className="op-primary" disabled={busy||action==='hold'&&!holdValid} onClick={submit}>{busy?lo('legacyOp.copy125'):lo('legacyOp.copy299')}</button></div>}
    {error&&<p className="op-error" role="alert">{error}</p>}<footer className="op-dialog-actions"><button onClick={onClose}>{lo('storyboard.copy58')}</button></footer>
  </Dialog>;
}

export default function DstackCapacity({account}){const {t:lo,locale}=useI18n();const {age,dollars,dstackNodeStatus,dstackProvider,dstackMode,dstackLifetime}=operatorFormatters(locale);
  const controller=useMemo(()=>createOperatorController({client:createDstackClient(),pendingNamespace:'dstack'}),[]);
  const state=useSyncExternalStore(controller.subscribe,controller.getState,controller.getState),[now,setNow]=useState(Date.now()),[dialog,setDialog]=useState(null),[view,setView]=useState('current');
  useEffect(()=>{setDialog(null);setView('current');void controller.setAccount(account);},[account,controller]);
  useEffect(()=>{if(!state.snapshot||!state.catalog||state.denied||state.unavailable)setDialog(null);},[state.snapshot,state.catalog,state.denied,state.unavailable]);
  useEffect(()=>()=>controller.destroy(),[controller]);
  useEffect(()=>{let stopped=false,timer;const tick=async()=>{setNow(Date.now());if(document.visibilityState!=='hidden')try{await controller.poll();}catch{}if(!stopped)timer=setTimeout(tick,5000);};timer=setTimeout(tick,5000);return()=>{stopped=true;clearTimeout(timer);};},[controller,account]);
  const snapshot=state.snapshot,catalog=state.catalog,busy=state.busy||!!state.pending,groups=dstackGroups(snapshot?.nodes,now),enabled=catalog?.enabled===true&&snapshot?.enabled===true,confirmed=!!snapshot&&!!catalog&&!state.denied&&!state.unavailable;
  const nodes=view==='history'?groups.history:[...groups.current,...groups.review];
  const selected=dialog?.node?(snapshot?.nodes||[]).find(node=>node.id===dialog.node.id)||dialog.node:null;
  return <section className="op-machine-section op-dstack-section" aria-labelledby="dstack-heading"><div className="op-section-heading"><div><span className="op-eyebrow">DSTACK · VAST / RUNPOD</span><h2 id="dstack-heading">{lo('legacyOp.copy300')}</h2><p>{lo('legacyOp.copy301')}</p></div><div className="op-dstack-actions"><button aria-label={lo('legacyOp.copy302')} disabled={state.loading||state.busy} onClick={()=>quietly(controller.refresh())}><RefreshCw size={15}/></button><button className="op-primary" disabled={busy||!enabled||!catalog?.profiles?.length} onClick={()=>setDialog({type:'start'})}><Plus size={16}/>{lo('legacyOp.copy181')}</button></div></div>
    {state.pending&&<div className="op-banner warn" role="alert"><Info size={16}/><div><b>{lo('legacyOp.copy303')}</b><p>{lo('legacyOp.copy304')}</p></div><button disabled={state.busy} onClick={()=>quietly(controller.recover())}>{lo('legacyOp.copy233')}</button></div>}
    {state.error&&<p className="op-error" role="alert">{state.error}</p>}
    {!snapshot?<p className="op-note" role="status">{state.loading?lo('legacyOp.copy305'):state.denied?lo('legacyOp.copy306'):state.unavailable?lo('legacyOp.copy307'):lo('legacyOp.copy308')}</p>:<>
      {!enabled&&<p className="op-note" role="status">{lo('legacyOp.copy309')}</p>}
      {enabled&&!catalog?.profiles?.length&&<p className="op-note">{lo('legacyOp.copy310')}</p>}
      <div className="op-dstack-summary"><span>{lo('legacyOp.copy311')} <b>{groups.current.filter(node=>rawDstackNodeStatus(node,now).label==='运行接口就绪').length}</b></span><span>{lo('legacyOp.copy312')} <b>{groups.current.filter(node=>['reserved','creating','starting','runtime_unconfirmed'].includes(node.state)).length}</b></span><span>{lo('legacyOp.copy313')} <b>{groups.review.length}</b></span><small>{lo('legacyOp.copy314')}</small></div>
      <div className="op-machine-tabs" role="group" aria-label={lo('legacyOp.copy315')}><button aria-pressed={view==='current'} onClick={()=>setView('current')}>{lo('legacyOp.copy183')} <span>{groups.current.length+groups.review.length}</span></button><button aria-pressed={view==='history'} onClick={()=>setView('history')}>{lo('legacyOp.copy184')} <span>{groups.history.length}</span></button></div>
      {nodes.length?<div className="op-machine-list">{nodes.map(node=>{const status=dstackNodeStatus(node,now);return <button key={node.id} className="op-dstack-row" onClick={()=>setDialog({type:'node',node})}><Server size={19}/><span><b>{dstackProvider(node.provider)} · {dstackMode(node.mode)}</b><small>{node.runtime_profile_id}</small><small>{dstackLifetime(node)} · {age(node.observed_at,now)}{lo('legacyOp.copy316')}</small></span><span className={'op-badge '+status.tone}>{status.label}</span><span>{dollars(node.hourly_cost_microusd,{digits:3})}<small>{lo('legacyOp.copy317')}</small></span></button>;})}</div>:<p className="op-note op-list-note">{view==='history'?lo('legacyOp.copy318'):lo('legacyOp.copy319')}</p>}
    </>}
    {confirmed&&dialog?.type==='start'&&<Start profiles={catalog.profiles} lifecycleVersion={catalog.lifecycle_version} controller={controller} busy={busy||!enabled} now={now} onClose={()=>setDialog(null)}/>}
    {confirmed&&dialog?.type==='node'&&selected&&<NodeDialog key={selected.id} node={selected} controller={controller} busy={busy} now={now} onClose={()=>setDialog(null)}/>}
  </section>;
}
