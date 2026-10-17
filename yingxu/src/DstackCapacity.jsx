import React,{useEffect,useMemo,useRef,useState,useSyncExternalStore} from 'react';
import {Clock,Info,Play,Plus,RefreshCw,Server,X} from 'lucide-react';
import {createOperatorController} from './operator-controller.js';
import {createDstackClient} from './dstack-client.js';
import {age,dollars,duration} from './operator-model.js';
import {dstackGroups,dstackKeepOnSelection,dstackLifecycle,dstackLifetime,dstackMode,dstackNodeStatus,dstackPreviewCurrent,dstackProvider} from './dstack-presentation.js';
import './dstack.css';

const quietly=promise=>promise.catch(()=>{});
const date=value=>typeof value==='number'&&Number.isFinite(value)?new Date(value*1000).toLocaleString('zh-CN'):'尚未确认';
function Dialog({title,onClose,children}){
  const ref=useRef(),opener=useRef(document.activeElement);
  useEffect(()=>{ref.current.showModal();return()=>{ref.current?.close();opener.current?.isConnected&&opener.current.focus();};},[]);
  return <dialog ref={ref} className="op-dialog" aria-label={title} onCancel={event=>{event.preventDefault();onClose();}}><header><div><span className="op-eyebrow">GPU CONTROL</span><h2>{title}</h2></div><button className="op-icon" aria-label="关闭面板" onClick={onClose}><X size={19}/></button></header>{children}</dialog>;
}

function Start({profiles,lifecycleVersion,controller,busy,now,onClose}){
  const models=[...new Set(profiles.map(profile=>profile.model_id))];
  const [model,setModel]=useState(models[0]||''),[mode,setMode]=useState(profiles[0]?.mode||'fl'),[chosen,setChosen]=useState(profiles[0]?.id||''),[minutes,setMinutes]=useState(Math.min(60,(profiles[0]?.max_ttl_seconds||3600)/60)),[preview,setPreview]=useState(null),[error,setError]=useState(''),[checking,setChecking]=useState(false);
  const options=profiles.filter(profile=>profile.model_id===model&&profile.mode===mode),profile=options.find(item=>item.id===chosen)||options[0];
  const [keepOn,setKeepOn]=useState('idle'),continuing=lifecycleVersion===2,policy=dstackKeepOnSelection(keepOn,minutes);
  const seconds=minutes*60,valid=!!profile&&(continuing?!!policy:Number.isInteger(seconds)&&seconds>=120&&seconds<=profile.max_ttl_seconds);
  const current=dstackPreviewCurrent(preview,profile?.id,continuing?policy:seconds,now);
  function changeModel(value){const first=profiles.find(profile=>profile.model_id===value);setModel(value);setMode(first?.mode||'fl');setChosen(first?.id||'');setPreview(null);setError('');}
  async function check(){if(!valid)return;setChecking(true);setError('');setPreview(null);const selection={profile_id:profile.id,...(continuing?policy:{ttl_seconds:seconds})};try{const result=await controller.preview(selection);setPreview({...result,selection});}catch(error){setError(error.message);}finally{setChecking(false);}}
  async function start(){if(!current)return;setError('');try{await controller.start(preview);onClose();}catch(error){setError(error.message);}}
  return <Dialog title="启动 dstack 机器" onClose={onClose}>
    <fieldset disabled={busy||checking}><div className="op-model-choice"><label>模型<select value={model} aria-label="dstack 模型" onChange={event=>changeModel(event.target.value)}>{models.map(value=><option key={value} value={value}>{value}</option>)}</select></label><div><span className="op-field-label">输入方式</span><div className="op-mode-options" role="group" aria-label="dstack 生成模式">{['fl','ref'].map(value=><button key={value} type="button" aria-pressed={mode===value} disabled={!profiles.some(profile=>profile.model_id===model&&profile.mode===value)} onClick={()=>{setMode(value);setChosen('');setPreview(null);setError('');}}>{dstackMode(value)}</button>)}</div></div></div>
      <label>部署配置<select aria-label="dstack 部署配置" value={profile?.id||''} onChange={event=>{setChosen(event.target.value);setPreview(null);setError('');}}>{options.map(profile=><option key={profile.id} value={profile.id}>{dstackProvider(profile.backend)} · {profile.gpu_names.join(' / ')} × 1</option>)}</select></label>
      {profile&&<p className="op-note">系统内存至少 {profile.memory_gib} GiB · 磁盘至少 {profile.disk_gib} GiB · GPU 显存至少 {profile.gpu_memory_gib} GiB。每台运行 1 张 GPU，保持所选模型与模式。</p>}
      {continuing&&<label>待机保留方式<select aria-label="dstack 待机保留方式" value={keepOn} onChange={event=>{setKeepOn(event.target.value);setPreview(null);setError('');}}><option value="idle">按空闲策略关机</option><option value="until">保留一段时间</option><option value="until_stopped">直到我手动关机</option></select></label>}
      {(!continuing||keepOn==='until')&&<label>{continuing?'至少保留待机多久':'最多运行多久'}<div className="op-unit-input"><input type="number" min={continuing?1:2} max={continuing?1440:profile?profile.max_ttl_seconds/60:undefined} step="1" value={minutes} onChange={event=>{setMinutes(Number(event.target.value));setPreview(null);setError('');}}/><span>分钟</span></div></label>}
      <p className="op-note">{continuing?'没有固定运行截止时间；按空闲与待机策略停止，并受原账户预算限制。':'窗口包含申请、准备和运行时间。'}供应商按配置与实时库存分配机器；预览不会租机。</p><button disabled={!valid} onClick={check}><RefreshCw size={14}/>{checking?'正在核对配置…':'预览费用与启动条件'}</button>
    </fieldset>
    {preview&&<div className="op-preview"><b>{current?'确认后开始租机':'预览已过期或选择已改变'}</b><dl><dt>每台费率上限</dt><dd>{dollars(preview.hourly_cost_microusd,{digits:3})} / 小时</dd><dt>{continuing?'初始预算预留':'预算预留上限'}</dt><dd>{dollars(preview.reservation_microusd)}</dd><dt>{continuing?'运行策略':'停止上限'}</dt><dd>{continuing?dstackLifetime(preview):date(preview.hard_deadline)}</dd>{dstackLifecycle(preview)&&<><dt>初始运行预留</dt><dd>{duration(preview.initial_reservation_seconds)}（预算估算，非关机期限）</dd></>}<dt>本次预览有效至</dt><dd>{date(preview.expires_at)}</dd></dl><p className="op-note">预算预留包含停止宽限，不是最终账单。{continuing?'继续运行按原账户额度补充预留；额度不足会排空停止。':''}下载、准备和空闲也可能计费。启动后只核对这一条机器记录，不自动换模型或重复申请。</p></div>}
    {error&&<p className="op-error" role="alert">{error}</p>}<footer className="op-dialog-actions"><button onClick={onClose}>取消</button><button className="op-primary" disabled={busy||checking||!current} onClick={start}><Play size={14}/>{busy?'正在处理…':'确认费用并启动'}</button></footer>
  </Dialog>;
}

function NodeDialog({node,controller,busy,now,onClose}){
  const [action,setAction]=useState(''),[minutes,setMinutes]=useState(10),[error,setError]=useState(''),[keepOn,setKeepOn]=useState(node.lifecycle?.keep_on||'idle');
  const lifecycle=dstackLifecycle(node),continuing=!!lifecycle,policy=dstackKeepOnSelection(keepOn,minutes);
  const status=dstackNodeStatus(node,now),left=typeof node.hard_deadline==='number'?Math.max(0,(node.hard_deadline*1000-now)/1000):0;
  const canHold=node.desired_state==='running'&&node.state!=='stopped'&&(continuing||!node.lifecycle&&left>0),canStop=node.desired_state==='running'&&node.state!=='stopped';
  const holdValid=canHold&&(continuing?!!policy:Number.isInteger(minutes)&&minutes>=1&&minutes*60<=left);
  async function submit(){setError('');try{if(action==='stop')await controller.stop(node);else await controller.hold(node,continuing?policy:minutes*60);onClose();}catch(error){setError(error.message);}}
  return <Dialog title="dstack 机器详情" onClose={onClose}><span className={'op-badge '+status.tone}>{status.label}</span><p>{dstackProvider(node.provider)} · {dstackMode(node.mode)}</p><dl className="op-dstack-facts"><dt>机器记录</dt><dd><code>{node.id}</code></dd><dt>运行配置</dt><dd><code>{node.runtime_profile_id}</code></dd><dt>最近观测</dt><dd>{age(node.observed_at,now)}</dd><dt>{continuing?'运行策略':'停止上限'}</dt><dd>{continuing?dstackLifetime(node):date(node.hard_deadline)}</dd>{!continuing&&<><dt>保留待机至</dt><dd>{node.hold_until?date(node.hold_until):'按原空闲策略停止'}</dd></>}<dt>费率上限</dt><dd>{dollars(node.hourly_cost_microusd,{digits:3})} / 小时</dd></dl>
    <p className="op-note">运行接口就绪后仍以业务准入与执行槽为准。原任务、结果收集和未确认费用均保留。{node.billing_state!=='no_charge_confirmed'?'最终费用尚未结算。':'此记录尚未向供应商申请，不产生费用。'}</p>{node.reason_code&&<details className="op-details"><summary>诊断信息</summary><code>{node.reason_code}</code></details>}
    {canHold&&<div className="op-dstack-hold">{continuing&&<label>待机保留方式<select aria-label="dstack 待机保留方式" value={keepOn} onChange={event=>{setKeepOn(event.target.value);setAction('');}}><option value="idle">按空闲策略关机</option><option value="until">保留一段时间</option><option value="until_stopped">直到我手动关机</option></select></label>}{(!continuing||keepOn==='until')&&<label>保留待机<div className="op-unit-input"><input type="number" min="1" max={continuing?1440:Math.floor(left/60)} step="1" value={minutes} onChange={event=>{setMinutes(Number(event.target.value));setAction('');}}/><span>分钟</span></div></label>}<small>{continuing?'只调整空闲关机偏好；原费率上限、账户预算和任务保护继续生效。':'暂缓空闲关机，仍在原停止上限内；不新增预算、不延长供应商租赁。'}</small></div>}
    <div className="op-dstack-actions"><button disabled={busy||!holdValid} onClick={()=>{setAction('hold');setError('');}}><Clock size={14}/>{continuing?'确认待机策略':'确认保留待机'}</button><button disabled={busy||!canStop} onClick={()=>{setAction('stop');setError('');}}>排空后关机</button></div>
    {action&&<div className="op-preview"><b>{action==='stop'?'停止接单，完成原任务后关机':continuing?keepOn==='until_stopped'?'保留至手动关机':keepOn==='idle'?'恢复空闲关机':`至少保留待机 ${minutes} 分钟`:`暂缓空闲关机 ${minutes} 分钟`}</b><p className="op-note">{action==='stop'?'提交停止意愿后，服务器会继续核对原任务与供应商状态；收到请求不表示已经停止扣费。':continuing?`没有固定关机期限。原账户预算限制继续生效，预算不足会排空停止；当前费率上限 ${dollars(node.hourly_cost_microusd,{digits:3})} / 小时。`:`原停止上限保持 ${date(node.hard_deadline)}，预算预留不变；不能保留超过这个时间。`}</p><button className="op-primary" disabled={busy||action==='hold'&&!holdValid} onClick={submit}>{busy?'正在处理…':'确认提交'}</button></div>}
    {error&&<p className="op-error" role="alert">{error}</p>}<footer className="op-dialog-actions"><button onClick={onClose}>关闭</button></footer>
  </Dialog>;
}

export default function DstackCapacity({account}){
  const controller=useMemo(()=>createOperatorController({client:createDstackClient(),pendingNamespace:'dstack'}),[]);
  const state=useSyncExternalStore(controller.subscribe,controller.getState,controller.getState),[now,setNow]=useState(Date.now()),[dialog,setDialog]=useState(null),[view,setView]=useState('current');
  useEffect(()=>{setDialog(null);setView('current');void controller.setAccount(account);},[account,controller]);
  useEffect(()=>{if(!state.snapshot||!state.catalog||state.denied||state.unavailable)setDialog(null);},[state.snapshot,state.catalog,state.denied,state.unavailable]);
  useEffect(()=>()=>controller.destroy(),[controller]);
  useEffect(()=>{let stopped=false,timer;const tick=async()=>{setNow(Date.now());if(document.visibilityState!=='hidden')try{await controller.poll();}catch{}if(!stopped)timer=setTimeout(tick,5000);};timer=setTimeout(tick,5000);return()=>{stopped=true;clearTimeout(timer);};},[controller,account]);
  const snapshot=state.snapshot,catalog=state.catalog,busy=state.busy||!!state.pending,groups=dstackGroups(snapshot?.nodes,now),enabled=catalog?.enabled===true&&snapshot?.enabled===true,confirmed=!!snapshot&&!!catalog&&!state.denied&&!state.unavailable;
  const nodes=view==='history'?groups.history:[...groups.current,...groups.review];
  const selected=dialog?.node?(snapshot?.nodes||[]).find(node=>node.id===dialog.node.id)||dialog.node:null;
  return <section className="op-machine-section op-dstack-section" aria-labelledby="dstack-heading"><div className="op-section-heading"><div><span className="op-eyebrow">DSTACK · VAST / RUNPOD</span><h2 id="dstack-heading">dstack 机器</h2><p>使用现有任务与预算账本，每台机器独立运行一个模型模式。</p></div><div className="op-dstack-actions"><button aria-label="刷新 dstack 机器状态" disabled={state.loading||state.busy} onClick={()=>quietly(controller.refresh())}><RefreshCw size={15}/></button><button className="op-primary" disabled={busy||!enabled||!catalog?.profiles?.length} onClick={()=>setDialog({type:'start'})}><Plus size={16}/>启动机器</button></div></div>
    {state.pending&&<div className="op-banner warn" role="alert"><Info size={16}/><div><b>原 dstack 操作结果待核对</b><p>保留原操作标识，不能再次开机。</p></div><button disabled={state.busy} onClick={()=>quietly(controller.recover())}>核对原操作</button></div>}
    {state.error&&<p className="op-error" role="alert">{state.error}</p>}
    {!snapshot?<p className="op-note" role="status">{state.loading?'正在读取 dstack 机器记录…':state.denied?'此账户没有 dstack 管理权限。':state.unavailable?'当前服务尚未提供 dstack 接口，原 Lium/Targon 控制不受影响。':'尚未确认 dstack 状态；不会显示为零台运行机器。'}</p>:<>
      {!enabled&&<p className="op-note" role="status">dstack 新容量未启用。需要管理员配置经过验收的运行镜像、模型配置和预算后才能启动；已有记录仍保留。</p>}
      {enabled&&!catalog?.profiles?.length&&<p className="op-note">此账户没有已配置的部署选项，请联系管理员。</p>}
      <div className="op-dstack-summary"><span>运行接口就绪 <b>{groups.current.filter(node=>dstackNodeStatus(node,now).label==='运行接口就绪').length}</b></span><span>准备中 <b>{groups.current.filter(node=>['reserved','creating','starting','runtime_unconfirmed'].includes(node.state)).length}</b></span><span>待核对 <b>{groups.review.length}</b></span><small>最终接单以任务准入为准</small></div>
      <div className="op-machine-tabs" role="group" aria-label="dstack 机器记录范围"><button aria-pressed={view==='current'} onClick={()=>setView('current')}>当前与待审核 <span>{groups.current.length+groups.review.length}</span></button><button aria-pressed={view==='history'} onClick={()=>setView('history')}>历史记录 <span>{groups.history.length}</span></button></div>
      {nodes.length?<div className="op-machine-list">{nodes.map(node=>{const status=dstackNodeStatus(node,now);return <button key={node.id} className="op-dstack-row" onClick={()=>setDialog({type:'node',node})}><Server size={19}/><span><b>{dstackProvider(node.provider)} · {dstackMode(node.mode)}</b><small>{node.runtime_profile_id}</small><small>{dstackLifetime(node)} · {age(node.observed_at,now)}观测</small></span><span className={'op-badge '+status.tone}>{status.label}</span><span>{dollars(node.hourly_cost_microusd,{digits:3})}<small>每小时上限</small></span></button>;})}</div>:<p className="op-note op-list-note">{view==='history'?'没有已确认结束的历史记录。':'当前没有登记在此账户的 dstack 机器。'}</p>}
    </>}
    {confirmed&&dialog?.type==='start'&&<Start profiles={catalog.profiles} lifecycleVersion={catalog.lifecycle_version} controller={controller} busy={busy||!enabled} now={now} onClose={()=>setDialog(null)}/>}
    {confirmed&&dialog?.type==='node'&&selected&&<NodeDialog key={selected.id} node={selected} controller={controller} busy={busy} now={now} onClose={()=>setDialog(null)}/>}
  </section>;
}
