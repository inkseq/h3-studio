import React,{useEffect,useMemo,useReducer,useRef,useState,useSyncExternalStore} from 'react';
import {ArrowLeft,ArrowUpRight,Clock,Cpu,Info,LogOut,Play,Plus,RefreshCw,Search,Server,Settings2,ShieldCheck,X} from 'lucide-react';
import {useCloud} from './CloudStudio.jsx';
import {cloudController as cloud} from './cloud-controller.js';
import {createOperatorController} from './operator-controller.js';
import {age,allowed,dollars,duration,hasBoundOperatorSelection,runtimeDuration,reasonText,staleSnapshot,stateLabel,providerLabel,operatorMarketProvider,operatorModels,operatorCandidateKey,operatorCandidateSelection,operatorCandidateCurrent,operatorCandidatePreviewMatches,initialOperatorMarket,operatorMarketReducer,operatorMarketSummary,roundedSpec} from './operator-model.js';
import './operator.css';
import {operatorNodeGroups,operatorCapacitySummary,filterOperatorHistory,operatorExtensionCurrent,operatorExtensionReadback} from './operator-presentation.js';
import {Reasons,Badge,NodeCard,MachineRow,CandidateCard} from './operator-machine-view.jsx';
import DstackCapacity from './DstackCapacity.jsx';
export {NodeCard} from './operator-machine-view.jsx';

const quietly=promise=>promise.catch(()=>{});
const known=value=>Number.isFinite(value)?value:'—';
const date=value=>typeof value==='number'?new Date(value*1000).toLocaleString('zh-CN'):'尚未记录';
function Dialog({title,subtitle,onClose,children,wide=false}){
  const ref=useRef(),opener=useRef(document.activeElement);
  useEffect(()=>{ref.current.showModal();return()=>{ref.current?.close();opener.current?.isConnected&&opener.current.focus();};},[]);
  return <dialog ref={ref} className={'op-dialog'+(wide?' op-market-dialog':'')} aria-label={title} onCancel={e=>{e.preventDefault();onClose();}}><header><div><span className="op-eyebrow">GPU CONTROL</span><h2>{title}</h2>{subtitle&&<p>{subtitle}</p>}</div><button className="op-icon" aria-label="关闭面板" onClick={onClose}><X size={19}/></button></header>{children}</dialog>;
}
function RecipeEvidence({profile}){
  return <details className="op-details"><summary>查看该配方的历史实测与边界</summary><p>来自独立运行实验，单样本、缓存条件未受控；包含任务内加载，不含租机和下载。不能当成当前等待时间或质量保证。</p><div className="op-table-wrap"><table><thead><tr><th>输入方式</th><th>规格</th><th>步数</th><th>任务实测</th></tr></thead><tbody>{(profile.verified_cases||[]).flatMap(row=>(row.measurements||[]).map((m,index)=><tr key={row.id+'-'+index}><td>{row.mode==='fl'?'首尾帧':'全能参考'}<small>{(row.input_roles||[]).join(' · ')}</small></td><td>{row.width} × {row.height}<small>{row.frames} 帧 / {row.fps} fps</small></td><td>{row.steps}</td><td>{duration(m.total_seconds)}<small>{m.measured_at?.slice(0,10)} · n={m.sample_count}</small></td></tr>))}</tbody></table></div>{!profile.verified_cases?.length&&<p>尚无对应实测记录。</p>}{profile.warnings?.map((warning,index)=><p key={index}>{warning}</p>)}<p>模型与配置标识：<code>{profile.id}</code></p></details>;
}
function Login(){
  const c=useCloud(),[username,setUsername]=useState(''),[password,setPassword]=useState(''),[error,setError]=useState('');
  return <section className="op-empty op-login"><ShieldCheck size={32}/><h2>登录管理账户</h2><p>使用现有网站账户。是否具有机器管理权限，由服务器确认。</p><form onSubmit={async e=>{e.preventDefault();setError('');try{await cloud.login(username,password);}catch(error){setError(error.message);}}}><label>用户名<input autoComplete="username" required value={username} onChange={e=>setUsername(e.target.value)}/></label>{!['username-only-test','username-test'].includes(c.auth?.authentication)&&<label>密码<input type="password" autoComplete="current-password" required value={password} onChange={e=>setPassword(e.target.value)}/></label>}{['username-only-test','username-test'].includes(c.auth?.authentication)&&<p className="op-note">当前为本地测试登录。</p>}{error&&<p role="alert" className="op-error">{error}</p>}<button className="op-primary" disabled={c.busy||!c.auth||c.auth.auth_ready===false}>登录控制台</button></form></section>;
}
function ManualReviewDialog({node,busy,onClose,onReview}){
  const [absent,setAbsent]=useState(false),[noCharge,setNoCharge]=useState(false),[error,setError]=useState('');
  return <Dialog title="标记已关机" subtitle="人工核对账户与费用，保留历史和未结账单。" onClose={onClose}><p>请先在 {providerLabel(node.provider)} 账户中核对这台机器和费用，再确认下列两项。</p><p>{node.provider_instance_id?'供应商实例':'平台机器记录'}：<code>{node.provider_instance_id??node.id}</code></p>{node.provider_instance_id==null&&<p className="op-note">原启动尚未确认供应商实例身份，请核对账户中没有本次启动对应的机器；标记不会补写实例身份。</p>}<label className="op-check"><input type="checkbox" checked={absent} onChange={e=>setAbsent(e.target.checked)}/>账户机器列表中已没有这台机器</label><label className="op-check"><input type="checkbox" checked={noCharge} onChange={e=>setNoCharge(e.target.checked)}/>已核对未继续扣费</label><p className="op-note">记录审核账户、时间和原实例身份，暂停自动核对并从运行容量中排除。供应商最终删除和账单保留在 backlog；原任务、停止请求、期限和预算预留保留。仍有任务或执行槽义务时服务器会拒绝标记。</p>{error&&<p className="op-error" role="alert">{error}</p>}<footer className="op-dialog-actions"><button onClick={onClose}>取消</button><button className="op-primary" disabled={busy||!absent||!noCharge} onClick={async()=>{try{await onReview(node,{account_absent:absent,no_continuing_charge:noCharge});onClose();}catch(error){setError(error.message);}}}>确认标记</button></footer></Dialog>;
}
function ExtensionDialog({node,busy,controller,onClose,now}){
  const [minutes,setMinutes]=useState(30),[preview,setPreview]=useState(null),[error,setError]=useState(''),[loading,setLoading]=useState(false),[receipt,setReceipt]=useState(null);
  const seconds=minutes*60,valid=Number.isInteger(minutes)&&minutes>0;
  const current=operatorExtensionCurrent(node,preview,seconds,now),changed=preview&&(preview.node_version!==node.version||preview.current_deadline!==node.hard_deadline);
  const supported=!!node.actions?.extend,submitted=!!receipt,saved=operatorExtensionReadback(node,preview,receipt);
  async function check(){setError('');setLoading(true);setPreview(null);try{setPreview(await controller.extensionPreview(node,seconds));}catch(error){setError(error.message);}finally{setLoading(false);}}
  async function confirm(){if(!current)return;setError('');try{setReceipt(await controller.extend(preview));}catch(error){setError(error.message);}}
  return <Dialog title="延长这台机器的运行窗口" subtitle={`${providerLabel(node.provider)} · ${node.gpu_model||node.id}`} onClose={onClose}>
    <p>当前停止上限：<b>{date(node.hard_deadline)}</b></p><p className="op-note">只延长本机已有授权和预算覆盖的窗口。供应商已经安排的删除时间不会由这个按钮推迟；是否能延长，以服务器预览为准。</p>
    {!supported?<p className="op-error">当前服务尚未提供这台机器的延期接口。</p>:<><label>再运行多久<div className="op-unit-input"><input type="number" min="1" step="1" value={minutes} disabled={busy||loading||submitted} onChange={e=>{setMinutes(Number(e.target.value));setPreview(null);setError('');}}/><span>分钟</span></div></label>
      <button disabled={busy||loading||!valid||submitted} onClick={check}><RefreshCw size={14}/>{loading?'正在核对…':'预览期限与费用'}</button>
      {!preview&&<Reasons items={node.actions.extend.blockers||[]}/>}
      {preview&&<div className="op-preview"><b>{submitted?(saved?'延期已保存并核对':'操作已接受，期限待核对'):changed?'机器状态已改变，请重新预览':current?'可以延长此窗口':'当前不能延长'}</b><dl><dt>原停止上限</dt><dd>{date(preview.current_deadline)}</dd><dt>新的停止上限</dt><dd>{date(preview.new_deadline)}</dd><dt>预计新增使用成本</dt><dd>{dollars(preview.estimated_incremental_cost_microusd)}</dd><dt>新增预算预留</dt><dd>{dollars(preview.incremental_reservation_microusd)}</dd></dl><Reasons items={preview.blockers}/><p className="op-note">预计成本不是最终账单。新增预留为零表示已有预留覆盖，并不表示机器免费运行。确认有效至 {date(preview.expires_at)}。</p></div>}
      {submitted&&<p role="status" className="op-note">{saved?'服务器回执和当前机器期限一致。':'已收到操作回执，仍需刷新核对实际期限；请勿重复提交。'}当前停止上限：{date(node.hard_deadline)}。原机器、任务和预算记录保留。</p>}
    </>}{error&&<p role="alert" className="op-error">{error}</p>}
    <footer className="op-dialog-actions"><button onClick={onClose}>{submitted?'关闭':'取消'}</button>{supported&&!submitted&&<button className="op-primary" disabled={busy||loading||!current} onClick={confirm}><Clock size={14}/>确认延长窗口</button>}</footer>
  </Dialog>;
}
function PolicyDialog({policy,busy,onSave,onClose}){
  const [draft,setDraft]=useState({...policy}),[error,setError]=useState('');
  const fields=[['max_instances','最多机器数','台'],['max_physical_gpus','最多 GPU 数','张'],['idle_shutdown_seconds','空闲后关机','秒'],['max_ttl_seconds','单次租赁最长时间','秒']];
  return <Dialog title="全局运行上限" subtitle="所有人工启动与自动扩容共同遵守这些上限。" onClose={onClose}><form onSubmit={async e=>{e.preventDefault();setError('');try{const {version,...values}=draft;await onSave({...values,expected_version:policy.version});onClose();}catch(error){setError(error.message);}}}><label className="op-check"><input type="checkbox" checked={draft.enabled===true} onChange={e=>setDraft({...draft,enabled:e.target.checked})}/>允许容量服务启动新机器</label><p className="op-note">关闭后保留已接受任务和待核对的租赁，不等于强制停止所有机器。</p><div className="op-form-grid">{fields.map(([key,label,unit])=><label key={key}>{label}<div className="op-unit-input"><input type="number" required min={key==='max_ttl_seconds'?120:key==='idle_shutdown_seconds'?1:0} step="1" value={draft[key]??''} onChange={e=>setDraft({...draft,[key]:Number(e.target.value)})}/><span>{unit}</span></div></label>)}<label>全局整机费率上限<div className="op-unit-input"><input type="number" required min="0" step="0.01" value={draft.max_hourly_cost_microusd==null?'':draft.max_hourly_cost_microusd/1e6} onChange={e=>setDraft({...draft,max_hourly_cost_microusd:Math.round(Number(e.target.value)*1e6)})}/><span>USD / 小时</span></div></label></div><p className="op-note">修改不会重置累计费用或延长已有机器 TTL。保存时核对策略版本 {policy.version}。</p>{error&&<p role="alert" className="op-error">{error}</p>}<footer className="op-dialog-actions"><button type="button" onClick={onClose}>取消</button><button className="op-primary" disabled={busy}>保存上限</button></footer></form></Dialog>;
}

function FilterFacts({filters={}}){
  const entries=[['min_ram_gib','系统内存至少',value=>roundedSpec(value,'GiB')],['min_disk_gib','可用磁盘至少',value=>roundedSpec(value,'GiB')],['min_cpu_cores','CPU 至少',value=>roundedSpec(value,'核')],['min_download_mbps','下载带宽至少',value=>roundedSpec(value,'Mbps')],['max_price_per_gpu_hour_microusd','每卡费率上限',value=>dollars(value,{digits:3})+' / 小时'],['allowed_countries','允许地区',value=>value.length?value.join('、'):'不限']];
  return <dl className="op-filter-facts">{entries.filter(([key])=>Array.isArray(filters[key])||Number.isFinite(filters[key])&&filters[key]>0).map(([key,label,format])=><div key={key}><dt>{label}</dt><dd>{format(filters[key])}</dd></div>)}</dl>;
}
function MarketFilters({result,catalog}){
  if(!result.filters?.length)return null;
  return <details className="op-details op-market-filters">
    <summary>当前筛选条件与排除原因{result.excluded_count>0&&` · ${result.excluded_count} 项被筛掉`}</summary>
    <p>条件来自服务端配置。当前模型准入条件包含已配置的资源门槛，不代表每项都是实测硬件最低值。调整运行策略由服务端处理，模型与精度保持所选配置。「参考条件」中的带宽与价格用于提示和排序；「服务端部署配置」中的资源、带宽与费率上限会限制实际启动。</p>
    <p>Lium 按可分配内存筛选，会扣除至少 4 GiB 的系统保留；Targon 按资源规格中的已分配内存筛选。</p>
    {result.filters.map(profile=><section key={profile.runtime_profile_id}>
      <h4>{catalog?.profiles?.find(item=>item.id===profile.runtime_profile_id)?.label||profile.runtime_profile_id}</h4>
      <p className="op-note">当前模型准入条件</p><FilterFacts filters={profile.hard_requirements}/>
      <p className="op-note">参考条件</p><FilterFacts filters={profile.guidance}/>
      {profile.deployments?.length?<details className="op-details">
        <summary>服务端部署配置 · {profile.deployments.length} 项</summary>
        {profile.deployments.map(binding=><section key={binding.binding_id}>
          <p><b>{providerLabel(binding.provider)} · {binding.gpu_type} × {binding.gpu_count}</b> · {binding.enabled?'已启用':'未启用'}</p>
          <FilterFacts filters={binding.filters}/>
          <p>整机费率上限 {dollars(binding.hourly_cost_ceiling_microusd,{digits:3})} / 小时 · 配置 <code>{binding.binding_id}</code></p>
        </section>)}
      </details>:<p className="op-note">尚无匹配的服务端部署配置。</p>}
    </section>)}
    {result.excluded?.length>0&&<details className="op-details">
      <summary>查看被筛掉的机器 · {result.excluded_count} 项</summary>
      <p>以下库存低于当前模型的资源准入门槛，或 GPU 型号、版本尚未纳入所选模型的验收配置。其他未满足的启动条件显示在机器卡片上。</p>
      <div className="op-table-wrap"><table><thead><tr><th>供应商与机器</th><th>上报资源</th><th>排除原因</th></tr></thead><tbody>
        {result.excluded.map((offer,index)=><tr key={`${offer.provider}-${offer.offer_id}-${offer.runtime_profile_id}-${index}`}>
          <td>{providerLabel(offer.provider)} · {offer.gpu_type} × {offer.gpu_count}<small><code>{offer.offer_id}</code></small></td>
          <td>内存 {roundedSpec(offer.ram_gib,'GiB')}{Number.isFinite(offer.allocation_ram_gib)&&offer.allocation_ram_gib!==offer.ram_gib&&<small>可分配 {roundedSpec(offer.allocation_ram_gib,'GiB')}</small>}<small>磁盘 {roundedSpec(offer.disk_gib,'GiB')}</small></td>
          <td><Reasons items={offer.blockers}/><small>{offer.runtime_profile_id}</small></td>
        </tr>)}
      </tbody></table></div>
      {result.excluded_count>result.excluded.length&&<p>已展示前 {result.excluded.length} 项，共 {result.excluded_count} 项。</p>}
    </details>}
  </details>;
}
function StartDrawer({catalog,policy,controller,busy,onClose}){
  const models=useMemo(()=>operatorModels(catalog),[catalog]),[market,dispatch]=useReducer(operatorMarketReducer,policy,initialOperatorMarket),[now,setNow]=useState(Date.now());
  const scanSignal=useRef(new AbortController());
  const {query,result,row,selection,preview,error}=market;
  useEffect(()=>{const timer=setInterval(()=>setNow(Date.now()),1000);return()=>clearInterval(timer);},[]);
  useEffect(()=>{const signal=new AbortController();scanSignal.current=signal;return()=>signal.abort();},[]);
  const model=models.find(item=>item.id===query.model_id),profile=model?.profiles.find(item=>item.id===selection?.runtime_profile_id);
  const ttl=runtimeDuration(policy,null,query.ttl_seconds),expired=!!preview&&preview.expires_at*1000<=now;
  const current=operatorCandidateCurrent(result,query,row,now),matches=operatorCandidatePreviewMatches(selection,preview);
  const canStart=!!preview?.can_start&&hasBoundOperatorSelection(preview)&&matches&&!expired&&current&&!ttl.problem&&preview.policy_version===policy.version;
  function change(patch){dispatch({type:'change',patch});}
  async function scan(){
    const revision=market.revision+1;dispatch({type:'scan'});
    try{const response=await controller.refreshCandidates(query,result=>dispatch({type:'refresh-progress',revision,result}),scanSignal.current.signal);dispatch({type:'result',revision,result:response});}
    catch(error){dispatch({type:'error',revision,error:error.message});}
  }
  async function choose(candidate){
    const next=operatorCandidateSelection(catalog,query,candidate);
    if(!next||!operatorCandidateCurrent(result,query,candidate,Date.now()))return;
    const revision=market.revision+1;dispatch({type:'select',row:candidate,selection:next});
    try{const response=await controller.preview(next);dispatch({type:'preview',revision,preview:response});if(!operatorCandidatePreviewMatches(next,response))dispatch({type:'error',revision,error:'预览返回的机器身份与所选配置不一致，不能确认启动。请重新查询。'});}
    catch(error){dispatch({type:'error',revision,error:error.message});}
  }
  async function start(){
    if(!canStart)return;
    try{await controller.start(preview);onClose();}catch(error){dispatch({type:'error',revision:market.revision,error:error.message});}
  }
  function back(){dispatch({type:'back'});}
  if(selection)return <Dialog key="start-confirmation" title={row?.offer_kind==='resource_sku'?'确认按此规格启动':'确认启动这台机器'} subtitle={`${providerLabel(selection.provider)} · ${selection.gpu_type} × ${selection.gpu_count}`} onClose={back}>
    <p className="op-note">{model?.label} · {query.mode==='fl'?'FL2VA 首尾帧':'Ref2VA 全能参考'} · 最多运行 {duration(selection.ttl_seconds)}</p>
    {market.loading==='preview'&&<p role="status">正在核对库存与费用…</p>}
    {preview&&<div className="op-preview"><div><b>{expired?'费用确认已过期':!matches?'机器身份不一致':!current?'库存观测已过期':canStart?'确认后开始租机':'暂时不能启动'}</b></div><dl><dt>整机当前报价</dt><dd>{dollars((preview.selected_offer||row)?.hourly_cost_microusd,{digits:3})} / 小时</dd><dt>本次预算预留</dt><dd>{dollars(preview.reservation_microusd)}</dd><dt>最多运行</dt><dd>{duration(selection.ttl_seconds)}</dd></dl><Reasons items={preview.blockers}/>{preview.policy_version!==policy.version&&<p className="op-error">运行策略已更新，请返回重新查询。</p>}
      <details className="op-details"><summary>费用与部署详情</summary><p>预留上限 {dollars(preview.estimated_hourly_cost_microusd)} / 小时；费用确认有效至 {date(preview.expires_at)}。</p><p>机器或规格：<code>{selection.offer_id}</code></p>{Number.isFinite(preview.minimum_ttl_seconds)&&<p>最短启动窗口：{duration(preview.minimum_ttl_seconds)}</p>}{profile&&<RecipeEvidence profile={profile}/>}</details>
      <p className="op-note">下载、准备和空闲也计费。预留不是最终账单；仅启动这项配置，不自动换机器。</p>
    </div>}
    {error&&<p className="op-error" role="alert">{error}</p>}
    <footer className="op-dialog-actions"><button disabled={busy} onClick={back}><ArrowLeft size={14}/>返回机器列表</button><button className="op-primary" disabled={busy||!canStart} onClick={start}><Play size={15}/>{busy?'正在处理…':'确认费用并启动'}</button></footer>
  </Dialog>;
  return <Dialog key="machine-list" wide title="为模型选择机器" subtitle="选模型与输入方式，在机器卡片上启动。" onClose={onClose}>
    <fieldset disabled={busy}>
      <section className="op-model-choice"><label>模型与精度<select aria-label="模型与精度" value={query.model_id} onChange={e=>change({model_id:e.target.value})}><option value="">请选择模型</option>{models.map(item=><option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
        <div><span className="op-field-label">输入方式</span><div className="op-mode-options" role="group" aria-label="机器生成模式">{[['fl','FL2VA','首尾帧'],['ref','Ref2VA','全能参考']].map(([mode,label,note])=><button type="button" key={mode} aria-pressed={query.mode===mode} onClick={()=>change({mode})}><b>{label}</b><small>{note}</small></button>)}</div></div>
      </section>
      {model&&<p className="op-model-caption">{model.id} · 将比较同一模型、精度的兼容部署，不自动替换模型。</p>}
      {ttl.problem&&<p className="op-error" role="alert">{ttl.problem}</p>}
      <div className="op-market-query"><button className="op-primary" type="button" disabled={busy||market.loading==='candidates'||!model||!!ttl.problem} onClick={scan}><RefreshCw size={14}/>{market.loading==='candidates'?'正在查询两家供应商…':result?'刷新兼容机器':'查看兼容机器'}</button><span>同时查询 Lium 和 Targon，不租机、不预留预算</span><details className="op-details op-start-window"><summary>启动窗口 · {duration(query.ttl_seconds)}</summary><label>最多运行多久<div className="op-unit-input"><input type="number" min="2" max={ttl.maximumMinutes??undefined} step="1" value={query.ttl_seconds/60} onChange={e=>change({ttl_seconds:Number(e.target.value)*60})}/><span>分钟</span></div></label><p>当前策略上限 {ttl.maximumMinutes??'尚未确认'} 分钟。窗口包含下载、准备和运行，是本次安全停止上限，不是机器保活保证；修改后重新查询与预览。</p></details></div>
      {result?<section className="op-candidates" aria-label="兼容机器列表"><div className="op-candidates-heading"><h3>选一个适合本次任务的配置</h3><small>{age(result.observed_at,now)}更新</small></div><p className="op-note" role="status">{operatorMarketSummary(result,now)}</p>
        <div className="op-market-providers" aria-live="polite">{['lium','targon'].map(provider=>{const observation=operatorMarketProvider(result,provider,now);return <details className="op-details" key={provider}><summary><b>{providerLabel(provider)}</b><span className={'op-badge '+(observation.fresh||observation.refreshing?'neutral':'warn')}>{observation.label}</span><small>{age(observation.observed_at,now)}</small></summary>{Number.isFinite(observation.refresh_requested_at)&&<p>本轮查询请求于 {date(observation.refresh_requested_at)}</p>}<p>最近库存观测于 {date(observation.observed_at)}{(observation.refresh_reason_code||observation.reason_code)&&<> · {reasonText(observation.refresh_reason_code||observation.reason_code)}</>}</p></details>;})}</div>
        <MarketFilters result={result} catalog={catalog}/>
        {result.candidates?.length?<div className="op-candidate-grid">{result.candidates.map((candidate,index)=><CandidateCard key={operatorCandidateKey(candidate)} row={candidate} index={index} current={operatorCandidateCurrent(result,query,candidate,now)} busy={busy||!operatorCandidateSelection(catalog,query,candidate)} onStart={choose}/>)}</div>:<div className="op-market-empty"><Server size={25}/><p>暂无可展示的兼容配置</p><small>保留所选模型与模式，稍后刷新库存。</small></div>}
      </section>:<div className="op-market-empty"><Server size={27}/><p>{market.loading==='candidates'?'正在核对兼容机器…':model?'查看该模型的兼容机器':'先选想运行的模型'}</p><small>GPU 型号、供应商和数量将在查询结果中一起比较。</small></div>}
    </fieldset>{error&&<p className="op-error" role="alert">{error}</p>}
    <footer className="op-dialog-actions"><button onClick={onClose}>关闭</button></footer>
  </Dialog>;
}

export default function OperatorConsole(){
  const c=useCloud(),controller=useMemo(()=>createOperatorController(),[]),state=useSyncExternalStore(controller.subscribe,controller.getState,controller.getState),[dialog,setDialog]=useState(null),[actionError,setActionError]=useState(''),[now,setNow]=useState(Date.now()),[machineView,setMachineView]=useState('current'),[historySearch,setHistorySearch]=useState(''),[historyProvider,setHistoryProvider]=useState('all');
  useEffect(()=>{document.title='映序 · GPU 控制台';quietly(cloud.initialize());return()=>controller.destroy();},[controller]);
  useEffect(()=>{setDialog(null);setActionError('');setMachineView('current');setHistorySearch('');setHistoryProvider('all');void controller.setAccount(c.account);},[c.account,controller]);
  useEffect(()=>{let stopped=false,timer;const tick=async()=>{setNow(Date.now());if(document.visibilityState!=='hidden')try{await controller.poll();}catch{}if(!stopped)timer=setTimeout(tick,5000);};timer=setTimeout(tick,5000);return()=>{stopped=true;clearTimeout(timer);};},[controller,c.account]);
  const snapshot=state.snapshot,stale=staleSnapshot(snapshot,now),profiles=state.catalog?.profiles||state.catalog?.deployment_profiles||[],busy=state.busy||!!state.pending;
  const groups=operatorNodeGroups(snapshot?.nodes),capacity=operatorCapacitySummary(snapshot,now),history=filterOperatorHistory(groups.history,{search:historySearch,provider:historyProvider});
  const selectedNode=dialog?.node?.id?(snapshot?.nodes||[]).find(node=>node.id===dialog.node.id)||dialog.node:null;
  const openNode=node=>setDialog({type:'detail',node});
  const nodeAction=(kind,node)=>{setActionError('');setDialog(kind==='extend'?{type:'extend',node}:{type:'action',kind,node});};
  const row=node=><MachineRow key={node.id} node={node} profiles={profiles} now={now} disabled={busy||stale} onOpen={openNode} onExtend={node=>nodeAction('extend',node)} onReview={node=>nodeAction('manualReview',node)}/>;
  const unbound=(snapshot?.operations||[]).filter(op=>!(op.node_ids||[]).length&&['accepted','running','waiting','unknown'].includes(op.state));
  const refresh=()=>quietly(controller.refresh());
  const content=!c.account?<Login/>:state.denied?<section className="op-empty"><ShieldCheck size={32}/><h2>此账户没有机器管理权限</h2><p>仍可返回创作。GPU 租赁权限不会随普通 API Key 自动授予。</p><a href="/quick-chat">返回快速创作 <ArrowUpRight size={14}/></a></section>:!snapshot?<section className="op-empty"><Server size={34}/><h2>{state.loading?'正在读取控制台…':state.unavailable?'当前服务尚未接入控制台':'暂时无法读取机器状态'}</h2><p>没有确认到的状态不会显示为“零台机器”或“已停止”。</p><button onClick={refresh} disabled={state.loading}><RefreshCw size={14}/>重新读取</button></section>:<>
    <div className="op-overview">{[['观测就绪执行槽',capacity.ready_slots,`准备中 ${capacity.starting_nodes} 台 · 状态待核对 ${capacity.unknown_nodes} 台 · 实际接单以准入检查为准`],['运行 / 排队',`${known(snapshot.summary?.jobs_running)} / ${known(snapshot.summary?.jobs_waiting)}`,'现有任务保持原身份'],['已分配机器 / GPU',`${known(snapshot.summary?.nodes_active)} / ${known(snapshot.summary?.gpus_allocated)}`,'含准备和排空阶段，不表示模型已就绪'],['整机合计费率',dollars(snapshot.summary?.hourly_cost_microusd),snapshot.summary?.hourly_cost_basis==='approved_ceiling_including_pending_commands'?'批准上限 / 小时 · 含待启动预约':'每小时 · 不是最终账单']].map(([label,value,note])=><div key={label}><label>{label}</label><strong>{stale?'—':value??'—'}</strong><small>{stale?'观测已过期，请刷新':note}</small></div>)}</div>
    <div className={'op-controller-line '+(stale||snapshot.controller?.stale?'warn':'')}><span><i/>{snapshot.controller?.stale?'控制器心跳已过期':stateLabel(snapshot.controller?.state)}</span><span>最近心跳：{age(snapshot.controller?.last_heartbeat_at,now)}</span><span>策略：{snapshot.policy?.enabled?'允许启动新容量':'已暂停新容量'}</span><span>空闲 {duration(snapshot.policy?.idle_shutdown_seconds)} 后关机</span></div>
    <section className="op-machine-section"><div className="op-section-heading"><div><span className="op-eyebrow">CAPACITY</span><h2>机器管理</h2><p>可接单的匹配执行槽处理 Quick Chat 与 API 队列。</p></div><button className="op-primary" disabled={busy||!allowed(snapshot,'start',now)} onClick={()=>setDialog({type:'start'})}><Plus size={16}/>启动机器</button></div>
      <div className="op-machine-tabs" role="group" aria-label="机器记录范围"><button aria-pressed={machineView==='current'} onClick={()=>setMachineView('current')}>当前与待审核 <span>{groups.current.length+groups.pending_review.length}</span></button><button aria-pressed={machineView==='history'} onClick={()=>setMachineView('history')}>历史记录 <span>{groups.history.length}</span></button></div>
      {machineView==='current'?<>
        <div className="op-list-heading"><h3>当前机器 <span>{groups.current.length}</span></h3><small>运行、准备和停止过程 · 点开查看详情</small></div>
        {groups.current.length||unbound.length?<div className="op-machine-list">{groups.current.map(row)}{unbound.map(op=><div className="op-operation-row" key={op.id}><Server size={18}/><div><b>启动请求已接受，等待机器绑定</b><small>{stateLabel(op.state)} · {age(op.updated_at,now)} · <code>{op.id}</code></small></div></div>)}</div>:<div className="op-list-empty"><Server size={22}/><p>当前没有已登记的运行或准备机器。</p></div>}
        <div className="op-list-heading"><h3>待审核 <span>{groups.pending_review.length}</span></h3><small>需要核对原记录，不重复开机</small></div>
        {groups.pending_review.length?<div className="op-machine-list">{groups.pending_review.map(row)}</div>:<p className="op-note op-list-note">没有待处理的机器记录。</p>}
      </>:<>
        <div className="op-history-filters"><label><Search size={15}/><input type="search" placeholder="搜索机器、实例、GPU 或配方" aria-label="搜索历史机器" value={historySearch} onChange={e=>setHistorySearch(e.target.value)}/></label><select aria-label="历史供应商" value={historyProvider} onChange={e=>setHistoryProvider(e.target.value)}><option value="all">全部供应商</option><option value="lium">Lium</option><option value="targon">Targon</option></select><small>{history.length} / {groups.history.length} 条</small></div>
        <p className="op-note">已结束及已人工审核的记录保留在这里，点开可查看当时的状态、原因和费用信息。</p>
        {history.length?<div className="op-machine-list">{history.map(row)}</div>:<div className="op-list-empty"><Search size={22}/><p>{groups.history.length?'没有符合搜索条件的历史记录。':'尚无历史机器记录。'}</p></div>}
      </>}
    </section><details className="op-operations op-details"><summary>最近管理操作 · {snapshot.operations?.length||0} 条</summary><p>以服务器持久记录为准。</p>{snapshot.operations?.length?<div className="op-table-wrap"><table><thead><tr><th>操作</th><th>状态</th><th>关联机器</th><th>更新时间</th><th>操作标识</th></tr></thead><tbody>{snapshot.operations.map(op=><tr key={op.id}><td>{{start:'启动机器',drain:'停止接单',stop:'排空后关机',manual_review:'人工审核删除',extend:'延长运行窗口'}[op.kind]||op.kind}</td><td><Badge state={op.state}/>{op.reason_code&&<small>{reasonText(op.reason_code)} <code>{op.reason_code}</code></small>}</td><td>{op.node_ids?.join('、')||'待绑定'}</td><td>{age(op.updated_at,now)}</td><td><code>{op.id}</code></td></tr>)}</tbody></table></div>:<p className="op-note">尚无管理操作记录。</p>}</details>
  </>;
  return <div className="op-shell"><a className="skip" href="#operator-main">跳到 GPU 控制台</a><aside className="op-sidebar"><a className="op-brand" href="/quick-chat">Ⅱ <b>映序</b><small>STUDIO</small></a><a className="op-back" href="/quick-chat"><ArrowLeft size={15}/>返回创作</a><div className="op-sidebar-label">运营工作区</div><a className="active" href="/operator" aria-current="page"><Server size={17}/>GPU 控制台</a><div className="op-side-note"><ShieldCheck size={17}/><p>一个控制台，共用现有任务与租赁账本。</p></div><div className="op-sidebar-account"><span>{(c.account||'访').slice(0,1).toUpperCase()}</span><div><b>{c.account||'未登录'}</b><small>管理权限由服务器确认</small></div>{c.account&&<button className="op-icon" aria-label="退出登录" onClick={()=>quietly(cloud.logout())}><LogOut size={16}/></button>}</div></aside>
    <main id="operator-main"><header className="op-topbar"><span>工作室 / 计算资源</span><div><span>{snapshot?`更新于 ${age(snapshot.observed_at,now)}`:'等待连接'}</span><button className="op-icon" aria-label="刷新机器状态" disabled={state.loading||!c.account} onClick={refresh}><RefreshCw size={17}/></button></div></header><div className="op-content"><div className="op-page-heading"><div><span className="op-eyebrow">GPU WORKSPACE</span><h1>让算力准备好。</h1><p>配置机器、看清准备进度，让创作接上真实生成。</p></div><button disabled={busy||!allowed(snapshot,'update_policy',now)} onClick={()=>setDialog({type:'policy'})}><Settings2 size={16}/>运行上限</button></div>
      {state.error&&!state.denied&&<div className="op-banner warn" role="alert"><Info size={17}/><span>{state.error}</span></div>}{snapshot&&stale&&<div className="op-banner warn" role="status">当前机器观测已过期，管理操作暂不可用。保留上次记录，等待重新核对。</div>}{state.pending&&<div className="op-banner warn" role="alert"><div><b>原操作结果待核对</b><p>不要重复开机。刷新和核对沿用原操作标识，账户切换也不会删除记录。</p></div><button disabled={state.busy} onClick={()=>quietly(controller.recover())}>核对原操作</button></div>}{content}
      {c.account&&!state.denied&&<DstackCapacity account={c.account}/>}
      <footer className="op-page-foot"><ShieldCheck size={14}/>启动、排空和停止均写入同一控制服务；浏览器不直连供应商。</footer></div></main>
    {dialog?.type==='detail'&&selectedNode&&<Dialog key={'detail-'+selectedNode.id} title="机器详情" subtitle="以当前服务器观测为准" onClose={()=>setDialog(null)}><NodeCard node={selectedNode} profiles={profiles} now={now} disabled={busy||stale} onAction={nodeAction}/></Dialog>}
    {dialog?.type==='extend'&&selectedNode&&<ExtensionDialog key={'extend-'+selectedNode.id} node={selectedNode} now={now} controller={controller} busy={busy||stale} onClose={()=>setDialog(null)}/>}
    {dialog?.type==='start'&&snapshot&&<StartDrawer catalog={state.catalog} policy={snapshot.policy} controller={controller} busy={busy||!allowed(snapshot,'start',now)} onClose={()=>setDialog(null)}/>}{dialog?.type==='policy'&&snapshot&&<PolicyDialog policy={snapshot.policy} busy={busy||!allowed(snapshot,'update_policy',now)} onClose={()=>setDialog(null)} onSave={body=>controller.updatePolicy(body)}/>}{dialog?.type==='action'&&dialog.kind==='manualReview'&&<ManualReviewDialog node={dialog.node} busy={busy||stale} onClose={()=>setDialog(null)} onReview={(node,attestation)=>controller.manualReview(node,attestation)}/>}{dialog?.type==='action'&&dialog.kind!=='manualReview'&&<Dialog title={dialog.kind==='drain'?'停止接收新任务':'排空后安全关机'} subtitle={dialog.node.gpu_model||dialog.node.id} onClose={()=>setDialog(null)}><p>{dialog.kind==='drain'?'机器将停止接收新任务，继续处理原有任务和结果收集。':'先停止接单，完成任务、结果收集及必要核对后再销毁实例。存在未确认义务时会保持等待。'}</p><p className="op-note">提交意愿不代表机器已关闭。以管理操作和供应商销毁回执为准。</p>{actionError&&<p className="op-error" role="alert">{actionError}</p>}<footer className="op-dialog-actions"><button onClick={()=>setDialog(null)}>取消</button><button className="op-primary" disabled={busy||stale} onClick={async()=>{try{await controller[dialog.kind](dialog.node);setDialog(null);}catch(error){setActionError(error.message);}}}>确认{dialog.kind==='drain'?'停止接单':'排空后关机'}</button></footer></Dialog>}
  </div>;
}
