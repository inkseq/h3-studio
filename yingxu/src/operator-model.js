export const epochMs=value=>typeof value==='number'&&Number.isFinite(value)?value*1000:null;
export const providerLabel=value=>({lium:'Lium',targon:'Targon'}[value]||value||'供应商未知');
export function dollars(value,{digits=2}={}){return typeof value==='number'&&Number.isFinite(value)?'$'+(value/1e6).toFixed(digits):'尚未核对';}
export function age(value,now=Date.now()){
  const time=epochMs(value);if(time===null)return '尚未观测';
  const seconds=Math.max(0,Math.floor((now-time)/1000));
  return seconds<60?`${seconds} 秒前`:seconds<3600?`${Math.floor(seconds/60)} 分钟前`:`${Math.floor(seconds/3600)} 小时前`;
}
export function duration(value){
  if(typeof value!=='number'||!Number.isFinite(value)||value<0)return '暂无实测';
  return value<60?`${Math.round(value)} 秒`:`${Math.floor(value/60)} 分 ${Math.round(value%60)} 秒`;
}
export function deadlineLabel(value,now=Date.now()){
  const time=epochMs(value);if(time===null)return '期限未知';
  if(time<=now)return '期限已到，等待状态核对';
  return '剩余 '+duration((time-now)/1000);
}
export function nodeDeadline(node,now=Date.now()){
  const observed=epochMs(node.provider_lifetime_observed_at),safe=node.provider_safe_deadline,cap=node.hard_deadline;
  const verified=node.provider_lifetime_state==='verified'&&Number.isFinite(safe)&&observed!==null&&observed<=now&&now-observed<=30000;
  return {verified,label:verified?'安全停止期限':'停止上限 · 供应商待核对',deadline:verified?(Number.isFinite(cap)?Math.min(cap,safe):safe):(Number.isFinite(cap)?cap:null)};
}
export function nodeRemovalConfirmation(node){
  const confirmation=node?.removal_confirmation;
  if(!['pending','overdue','confirmed','manually_reviewed'].includes(confirmation?.state))return null;
  const reviewed=confirmation.state==='manually_reviewed',pending=!reviewed&&confirmation.state!=='confirmed',overdue=confirmation.state==='overdue';
  const observation=confirmation.last_observation,observedState={starting:'启动中',running:'运行',destroyed:'删除',not_created:'未创建'}[observation?.state];
  const providerStatus={PENDING:'准备中',FAILED:'失败',STOPPED:'已停止',RUNNING:'运行'}[observation?.provider_status];
  const observed=[observedState&&`状态为“${observedState}”`,providerStatus&&`供应商报告“${providerStatus}”`].filter(Boolean).join('，');
  return {...confirmation,pending,reviewed,tone:reviewed?'neutral':overdue?'warn':pending?'neutral':'good',
    label:reviewed?'已人工审核 · 暂停核对':overdue?'删除尚未确认':pending?'等待确认删除':'已确认删除',
    message:reviewed?'管理员已核对账户无此机器且未继续扣费；暂停此实例的自动核对。供应商最终删除与账单仍保留在待办，原预算预留未结算。':pending?(overdue?'已等待 5 分钟以上。':'')+'供应商尚未确认删除；后台每分钟继续核对。':'机器删除已确认；费用以账本核对结果为准。',
    observationText:observed?`上次供应商观测：${observed}。`:'上次未能确认供应商状态。'};
}
const states={manually_reviewed:'已人工审核',reserved:'已预留预算',creating:'正在申请机器',creation_unknown:'租赁结果待核对',waiting_provider:'等待供应商启动',removal_pending:'等待供应商确认删除',awaiting_qualified_workers:'等待执行槽验收',bootstrap_unconfigured:'启动配置尚未就绪',provider_execution_unverified:'供应商执行尚未验收',observation_failed:'观测失败，待核对',ready:'可接单',running:'运行中',starting:'正在启动',provisioning:'正在租机',preparing:'正在准备',loading:'正在加载模型',loading_model:'正在加载模型',downloading:'正在下载权重',bootstrapping:'正在准备环境',checking:'正在检查',busy:'正在生成',idle:'空闲',draining:'正在排空',stopping:'正在停止',destroying:'正在销毁',stopped:'已停止',destroyed:'已销毁',disabled:'未启用',unconfigured:'未配置',unavailable:'暂不可用',unknown:'状态待核对',reconcile_required:'需要核对原操作',waiting:'等待核对',partial:'部分完成',pending:'已接受，等待处理',queued:'等待处理',accepted:'已接受',complete:'已完成',completed:'已完成',succeeded:'已完成',failed:'失败',blocked:'条件未满足',offline:'离线',healthy:'正常',stale:'状态已过期'};
export const stateLabel=value=>states[value]||'状态待核对';
export const stateTone=value=>['ready','healthy','complete','completed','succeeded'].includes(value)?'good':['failed','blocked','unknown','reconcile_required','stale','unavailable'].includes(value)?'warn':'neutral';
const reasons={operator_manual_review_controller_unavailable:"人工审核控制器尚未更新或心跳过期，请等待管理员完成启用。",operator_removal_manually_reviewed:'已人工核对账户无机器且未继续扣费，暂停自动核对。',operator_node_manually_reviewed:'此记录已人工审核，保留历史与未结账单。',operator_manual_review_worker_active:'仍有活跃执行槽或任务，暂不能人工审核。',operator_manual_review_device_unreleased:'执行槽设备义务尚未释放，暂不能人工审核。',operator_manual_review_attempt_unsafe:'原任务执行或结果收集未确认安全结束。',operator_manual_review_identity_mismatch:'供应商实例身份发生变化，请刷新后重新核对。',operator_manual_review_requires_requested_removal:'必须先提交这台机器的停止请求。',bootstrap_reconciliation_required:'启动结果需要核对，尚不能确认已准备好。',operator_bootstrap_failed:'启动检查未通过，请先处理已记录的原因。',wangp_configuration_permissions:'运行配置文件权限不符合要求，需要修复后核对原启动。',runtime_process_exited:'模型服务进程提前退出，需要核对原启动。',runtime_readiness_timeout:'模型服务未在规定时间内就绪，需要核对原启动。',wangp_runtime_dependency_missing:'运行环境缺少依赖，需要修复后核对原启动。',UnclassifiedBootstrapFailure:'启动检查异常，具体原因尚未确认。',operator_inventory_stale:'库存信息尚未更新，请等待控制器检查后刷新。',provider_inventory_unavailable:'目前无法核对供应商库存，请稍后刷新。',provider_inventory_unconfirmed:'供应商库存尚未确认，请等待控制器核对。',operator_ttl_below_provider_minimum:'所选期限短于供应商允许的最短期限，请调整本次运行时间后重新预览。',operator_deployment_not_configured:'此配方、模式与硬件尚无匹配的部署配置。',operator_binding_selection_mismatch:'所选条件与已配置部署不匹配。',operator_deployment_not_qualified:'此部署尚未完成执行验收。',operator_inventory_not_configured:'当前服务尚未配置供应商库存查询。',operator_ttl_limit:'所选运行期限超过策略上限。',operator_instance_limit:'将超过机器数量上限。',operator_gpu_limit:'将超过物理 GPU 数量上限。',operator_hourly_cost_limit:'将超过每小时费用上限。',operator_unpriced_existing_capacity:'已有容量的费用尚未核对，先保留原记录。',operator_authority_expiring:'已授权租赁期限不足，请核对原授权。',operator_reservation_insufficient:'预算预留不足以覆盖本次期限。',operator_policy_changed:'启动策略已改变，请核对当前操作。',operator_policy_version_conflict:'策略已被更新，请刷新后重新修改。',operator_binding_changed:'部署配置已改变，请核对原操作。',operator_bootstrap_unconfigured:'工作机启动流程尚未配置。',operator_provider_disabled:'供应商执行尚未启用。',operator_node_observation_stale:'机器观测已过期，请先核对。',operator_active_obligations:'仍有任务或租赁义务需要完成。',operator_capacity_disabled:'手动容量服务未启用。',operator_capacity_unconfigured:'手动容量服务尚未配置。',controller_unavailable:'控制器暂不可用。',controller_stale:'控制器状态已过期。',policy_disabled:'启动策略未启用。',provider_unconfigured:'供应商尚未连接。',no_matching_offers:'没有符合条件的机器。',inventory_unavailable:'目前无法核对库存。',max_instances_exceeded:'将超过机器数量上限。',max_physical_gpus_exceeded:'将超过 GPU 数量上限。',max_hourly_cost_exceeded:'将超过每小时费用上限。',budget_exceeded:'可用预算不足。',active_jobs:'仍有任务正在执行或收集。',pending_obligations:'仍有任务、租赁或费用需要核对。',unknown_rental:'原租赁结果未知，不能重复开机。',node_stale:'机器状态已过期，需先核对。',version_conflict:'机器或策略已改变，请刷新后重新预览。',preview_expired:'启动预览已过期，请重新预览。',profile_unavailable:'此部署配方尚未开放启动。',runtime_profile_unavailable:'此部署配方尚未开放启动。',capacity_not_configured:'容量控制尚未配置。'};
export function reasonText(value){const code=typeof value==='string'?value:value?.code;return reasons[code]||'条件尚未满足，请核对状态后重试。';}
Object.assign(reasons,{
  operator_provider_extension_unsupported:'供应商已安排停止时间，当前接口不能推迟。请保留原机器期限。',
  operator_provider_deadline_limit:'所选延期超过已核对的供应商安全期限，请缩短窗口或保留原期限。',
  operator_extension_unavailable:'当前机器没有可延长的已授权窗口。',
  operator_extension_preview_expired:'延期预览已过期，请重新核对期限与费用。',
  operator_extension_node_changed:'机器状态或停止上限已改变，请重新预览。',
  operator_gpu_not_catalogued:'此 GPU 型号或版本尚未纳入所选模型的验收配置。',
  inventory_refresh_timeout:'本轮库存查询超时，请检查控制器状态后重新查询。',
  operator_pool_paused:'该执行池已暂停启动，请联系管理员核对运行配置。',
  operator_provider_start_unqualified:'此供应商的租赁与执行流程尚未验收，当前不能启动。',
  inventory_ram_below_minimum:'系统内存低于当前要求。',inventory_disk_below_minimum:'可用磁盘低于当前要求。',
  inventory_bandwidth_below_minimum:'下载带宽低于当前要求。',inventory_price_above_limit:'报价超过当前价格上限。',
  inventory_country_mismatch:'地区不符合当前筛选条件。',inventory_insufficient_quantity:'库存数量不足以满足本次机器数量。',
  inventory_unknown_ram:'系统内存尚未上报。',inventory_unknown_disk:'可用磁盘尚未上报。',
  inventory_unknown_bandwidth:'下载带宽尚未上报，仍需核对。',inventory_unknown_country:'地区尚未上报，仍需核对。',
  inventory_unknown_price:'整机价格尚未确认。',
  inventory_unknown_allocation:'拆分或租赁状态尚未确认。',inventory_specs_unconfirmed:'库存已查询，但拆分或规格仍需核对，不能认定缺货。',
  inventory_unknown_cpu:'CPU 核数尚未上报。',inventory_cpu_below_minimum:'CPU 核数低于当前要求。',
  inventory_scan_failed:'供应商库存查询失败，请重新查询。',inventory_scan_stale:'库存观测已过期，请重新查询。',
  inventory_scanner_not_configured:'此供应商尚未配置库存查询。',
  operator_topology_not_qualified:'此 GPU 数量与运行方式尚未验收。',
  operator_execution_slots_unqualified:'尚未确认每张 GPU 都有匹配的执行槽。',
  operator_offer_stale:'此报价已过期，请重新查询并选择。',operator_offer_changed:'此报价条件已改变，请重新查询并预览。',
  operator_offer_unavailable:'此机器或资源规格已不可用，请重新查询。',
  operator_offer_observation_unavailable:'无法确认这项报价的最新观测，请刷新库存。',
  operator_offer_selection_mismatch:'报价与所选模型、模式或配置不一致，请重新选择。',
  operator_exact_offer_unsupported:'此配置尚不支持绑定到指定机器，暂不能启动。',
  operator_exact_offer_controller_unavailable:'当前控制器尚不能确认指定机器，请等待服务更新后重新预览。',
  inventory_price_above_guidance:'报价高于此模型的参考价格。',inventory_bandwidth_below_guidance:'下载带宽低于参考值，准备可能更慢。',
  inventory_bandwidth_unknown:'下载带宽尚未上报，无法比较准备速度。',
});
export function operatorMarketProvider(market,provider,now=Date.now()){
  const observation=market?.providers?.find(item=>item.provider===provider),observed=epochMs(observation?.observed_at);
  const seconds=Number.isFinite(market?.fresh_seconds)&&market.fresh_seconds>0?market.fresh_seconds:120;
  const refreshDeadline=epochMs(observation?.refresh_requested_at);
  const timedOut=observation?.refresh_status==='pending'&&refreshDeadline!==null&&now-refreshDeadline>=60000;
  const refreshStatus=timedOut?'timeout':observation?.refresh_status,refreshing=refreshStatus==='pending';
  const refreshProblem={failed:'本轮查询失败',timeout:'本轮查询超时'}[refreshStatus];
  const fresh=observation?.status==='ok'&&observed!==null&&observed<=now&&now-observed<=seconds*1000&&!refreshProblem;
  const status=observation?.status==='ok'&&!fresh?'stale':observation?.status||'unconfirmed';
  return {...observation,provider,fresh,status,refreshing,refresh_status:refreshStatus,refresh_reason_code:timedOut?'inventory_refresh_timeout':observation?.refresh_reason_code,label:refreshing?(fresh?'上次库存仍有效 · 刷新中':'正在查询库存'):refreshProblem||({ok:'库存已核对',error:'库存查询失败',unconfigured:'尚未配置库存查询',stale:'库存已过期，需重新查询',unconfirmed:'库存尚未核对'}[status]||'库存尚未核对')};
}
export function operatorRecommendationsCurrent(market,selection,now=Date.now()){
  return ['ok','partial'].includes(market?.status)&&market.reason_code==='inventory_no_matching_stock'&&operatorMarketProvider(market,selection.provider||'lium',now).fresh;
}
export function operatorOfferReadiness(row){
  if(row.blockers?.some(item=>(typeof item==='string'?item:item?.code)==='operator_pool_paused'))return '启动已暂停';
  if(row.specs_confirmed===false)return '规格待核对';
  const deploymentQualified=typeof row.deployment_qualified==='boolean'?row.deployment_qualified:row.qualification==='qualified';
  if(!deploymentQualified)return '部署待验收';
  return row.blockers?.length?'条件待调整':'待启动预览';
}
export function operatorInventorySummary(result,selection,now=Date.now()){
  if(result?.market){
    const provider=selection.provider||'lium',observation=operatorMarketProvider(result.market,provider,now);
    if(!observation.fresh)return `${providerLabel(provider)} 库存尚未确认，当前不能判断有无库存。`;
    if(result.market.reason_code==='inventory_specs_unconfirmed')return `库存已查询，但拆分或规格仍需核对，不能认定 ${selection.gpu_type} 缺货。`;
    if(result.market.status==='unconfirmed')return '库存查询已返回，但筛选条件尚未确认，当前不能判断有无匹配库存。';
    const count=result.market.offers?.length||0;
    return count?`找到 ${count} 项所选硬件报价，筛选与部署条件见下方。`:`当前没有所选 ${selection.gpu_type} 的库存报价。`;
  }
  if(result?.status==='available')return result.offers?.length?`当前返回 ${result.offers.length} 项库存，仍需预览核对启动条件。`:'库存查询成功，供应商未返回逐项报价；请预览核对可用条件。';
  return reasonText(result?.reason_code);
}
export function recommendationSelection(selection,row){
  if(!['lium','targon'].includes(row?.provider)||typeof row?.gpu_type!=='string'||!row.gpu_type.trim())return null;
  return {...selection,provider:row.provider,gpu_type:row.gpu_type};
}
export function operatorHardwareOptions(profile,selection){
  const options=(profile?.gpu_models||[]).map(item=>typeof item==='string'?{id:item,label:item}:{id:item.gpu_type||item.id,label:item.label||item.gpu_type||item.id});
  if(selection.gpu_type&&!options.some(item=>item.id===selection.gpu_type))options.push({id:selection.gpu_type,label:selection.gpu_type+' · 库存候选'});
  return options;
}
export function staleSnapshot(snapshot,now=Date.now()){
  const observed=epochMs(snapshot?.observed_at);
  return !snapshot||observed===null||now-observed>30000;
}
export function allowed(snapshot,permission,now=Date.now()){
  return !staleSnapshot(snapshot,now)&&snapshot.operator?.permissions?.[permission]===true;
}
export function profileSelection(profile,selection,customFilters=false){
  const filters=customFilters?{...selection.filters}:{allowed_countries:[]};
  for(const [key,bytes] of [['min_ram_gib',profile?.minimum_ram_bytes],['min_disk_gib',profile?.minimum_disk_bytes]]){
    if(Number.isFinite(bytes)&&bytes>0)filters[key]=Math.ceil(bytes/1024**3);else delete filters[key];
  }
  if(!customFilters)for(const [key,value] of [['min_download_mbps',profile?.hardware_filters?.minimum_download_mbps],['max_price_per_gpu_hour_microusd',profile?.hardware_filters?.maximum_price_per_gpu_hour_microusd]]){
    if(Number.isFinite(value)&&value>0)filters[key]=value;
  }
  return {...selection,runtime_profile_id:profile?.id||'',gpu_type:profile?.gpu_models?.[0]||'',gpu_count:profile?.gpu_count_options?.[0]||1,filters};
}
export function operatorFilterEdit(filters,key,value){
  const next={...filters};if(value==null||value==='')delete next[key];else next[key]=value;return next;
}
export function hasBoundOperatorSelection(preview){
  return typeof preview?.configuration_id==='string'&&preview.configuration_id.trim().length>0&&!!preview.selection;
}
export function previewOperatorSelection(selection,preview){
  return hasBoundOperatorSelection(preview)?{...preview.selection,provider:preview.selection.provider??selection.provider??'lium',filters:{...preview.selection.filters}}:selection;
}
export function runtimeDuration(policy,reportedMinimum=null,value=null){
  const maximumMinutes=Number.isFinite(policy?.max_ttl_seconds)?Math.floor(policy.max_ttl_seconds/60):null;
  const minimumMinutes=Number.isFinite(reportedMinimum)&&reportedMinimum>0?Math.ceil(reportedMinimum/60):null;
  const feasible=maximumMinutes!==null&&maximumMinutes>=Math.max(2,minimumMinutes??2);
  const suggestedSeconds=feasible?Math.min(maximumMinutes,Math.max(180,minimumMinutes??2))*60:null;
  const problem=!feasible?'当前策略上限无法容纳供应商期限，请先核对运行上限。':value!==null&&(!Number.isFinite(value)||value%60!==0||value<120||value>maximumMinutes*60||minimumMinutes!==null&&value<minimumMinutes*60)?'本次运行时间不在已核对的范围内，请调整后重新预览。':null;
  return {minimumMinutes,maximumMinutes,suggestedSeconds,problem};
}
export function initialOperatorSelection(policy){
  return {runtime_profile_id:'',mode:'fl',provider:'lium',gpu_type:'',node_count:1,gpu_count:1,ttl_seconds:runtimeDuration(policy).suggestedSeconds??0,filters:{allowed_countries:[]}};
}
export function operatorStartPayload(selection,customFilters=false){
  if(customFilters)return selection;
  const {filters,...body}=selection;return body;
}

/** Model choice is independent of the hardware-specific deployment profile. */
export function operatorModels(catalog){
  const groups=new Map();
  for(const profile of catalog?.profiles||catalog?.deployment_profiles||[]){
    if(!profile.model_id)continue;
    if(!groups.has(profile.model_id))groups.set(profile.model_id,{id:profile.model_id,label:profile.model_id.replace(/^MiniMax-H3-?/,'H3 · ').replaceAll('-',' '),profiles:[]});
    groups.get(profile.model_id).profiles.push(profile);
  }
  return [...groups.values()];
}
export const operatorCandidateKey=row=>JSON.stringify([row?.provider,row?.offer_id,row?.selection?.runtime_profile_id,row?.gpu_count]);
export function operatorCandidateSelection(catalog,query,row){
  const choice=row?.selection,profile=(catalog?.profiles||catalog?.deployment_profiles||[]).find(item=>item.id===choice?.runtime_profile_id);
  if(!profile||profile.model_id!==query.model_id||choice.mode!==query.mode||choice.ttl_seconds!==query.ttl_seconds||choice.node_count!==1||
    !['lium','targon'].includes(choice.provider)||choice.provider!==row.provider||choice.gpu_type!==row.gpu_type||
    !Number.isInteger(choice.gpu_count)||choice.gpu_count<1||choice.gpu_count!==row.gpu_count||
    typeof choice.offer_id!=='string'||!choice.offer_id||choice.offer_id!==row.offer_id||!choice.filters||typeof choice.filters!=='object')return null;
  return structuredClone(choice);
}
export function operatorCandidateCurrent(result,query,row,now=Date.now()){
  const observed=epochMs(row?.observed_at),seconds=result?.fresh_seconds||120;
  return !!row&&result?.model_id===query.model_id&&result?.mode===query.mode&&row.selection?.ttl_seconds===query.ttl_seconds&&
    operatorMarketProvider(result,row.provider,now).fresh&&observed!==null&&observed<=now&&now-observed<=seconds*1000;
}
export function operatorCandidatePreviewMatches(selection,preview){
  return !!selection&&!!preview?.selection&&['runtime_profile_id','mode','provider','gpu_type','gpu_count','node_count','ttl_seconds','offer_id'].every(key=>preview.selection[key]===selection[key])&&
    (!preview.selected_offer||['offer_id','provider','gpu_type','gpu_count'].every(key=>preview.selected_offer[key]===selection[key]));
}
export function initialOperatorMarket(policy){
  return {query:{model_id:'',mode:'fl',ttl_seconds:runtimeDuration(policy).suggestedSeconds??0},revision:0,result:null,row:null,selection:null,preview:null,confirmed:false,loading:null,error:''};
}
export function operatorMarketReducer(state,action){
  if(action.type==='back')return {...state,revision:state.revision+1,row:null,selection:null,preview:null,confirmed:false,loading:null,error:''};
  if(action.type==='change')return {...initialOperatorMarket(),query:{...state.query,...action.patch},revision:state.revision+1};
  if(action.type==='scan')return {...state,revision:state.revision+1,row:null,selection:null,preview:null,confirmed:false,loading:'candidates',error:''};
  if(action.type==='select')return {...state,revision:state.revision+1,row:action.row,selection:action.selection,preview:null,confirmed:false,loading:'preview',error:''};
  if(action.type==='confirm')return {...state,confirmed:action.value};
  if(action.revision!==state.revision)return state;
  if(action.type==='refresh-progress')return {...state,result:action.result};
  if(action.type==='result')return {...state,result:action.result,loading:null};
  if(action.type==='preview')return {...state,preview:action.preview,loading:null};
  if(action.type==='error')return {...state,error:action.error,loading:null};
  return state;
}
export function operatorMarketSummary(result,now=Date.now()){
  if(!result)return '查询后将显示 Lium 与 Targon 的兼容机器。';
  if(result.providers?.some(item=>operatorMarketProvider(result,item.provider,now).refreshing))return '正在查询 Lium 与 Targon；已核对的一方可以先预览启动，另一方继续查询。';
  const fresh=(result.providers||[]).some(item=>operatorMarketProvider(result,item.provider,now).fresh);
  if(!fresh)return '库存尚未确认，请刷新查询；当前不能判断有无合适机器。';
  if(result.candidates?.length)return `${result.candidates.length} 项兼容配置 · 已验收且满足条件的优先，再按整组价格与下载带宽排序。`;
  return result.reason_code==='inventory_specs_unconfirmed'?'库存已查询，部分规格仍需核对，暂不能认定缺货。':'当前已核对的库存中没有兼容配置；其他供应商的未确认库存不代表缺货。';
}
export function operatorSlotsLabel(row){
  return row.deployment_qualified===true&&Number.isInteger(row.execution_slots)&&row.execution_slots===row.gpu_count?
    `${row.execution_slots} 个执行槽 · 每卡 1 槽`:'执行槽数量待验收';
}
export const roundedSpec=(value,unit)=>Number.isFinite(value)?`${new Intl.NumberFormat('zh-CN',{maximumFractionDigits:1}).format(value)} ${unit}`:'尚未上报';
export const operatorRankReason=code=>({deployment_qualified:'部署已验收',deployment_pending:'部署待验收',whole_allocation_price:'按整组价格比较',bandwidth_known:'带宽已上报',bandwidth_unknown:'带宽待核对'}[code]||null);
