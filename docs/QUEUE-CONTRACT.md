# 平台任务账本接口

2026-10-04。新模块不导入旧server、不打开旧data、不调用provider、不启动线程。
SQLAlchemy 2.0.54 + psycopg 3.3.6。生产URL使用postgresql+psycopg；SQLite用于隔离验证。

`Repository(database_url, clock=time.time)`公开`engine`（echo=False、hide_parameters=True），`create_schema()`只创建platform_*表并执行已写明的增量迁移。当前字段迁移是instance_intents缺provider时ADD COLUMN NOT NULL DEFAULT 'unknown'，以及registered_workers缺drain_requested时ADD COLUMN DEFAULT0并对旧draining行回填1；保留旧行、容量、停止准入意图及预算，不重建/删除表。SQLite BEGIN IMMEDIATE、PostgreSQL固定advisory transaction lock串行化初始化。未来字段变更必须另写显式迁移，create_all不会自动迁移其他旧表。
`Scope(tenant_id, owner_id, project_id, actor_id='browser')`必须由已鉴权后端创建；本模块不接受客户端owner作为身份。

- `put_document(scope, kind, document_id, payload, expected_version=None)`返回完整行；新建version=1，更新必须指定当前version。
- `get_document(...)`、`list_documents(scope,kind,limit=100,offset=0,allowed_ids=None,summary=False)`返回行及payload。项目列表可用project_id='__projects', kind='project', document_id=实际项目ID；`summary=True`仅支持project，SQL直接提取title，返回payload仅含title，不先加载每份完整文稿。默认/详情仍保留完整文稿。
- `create_plan(scope,request,execution_plan,expires_at=...,estimated_cost_microusd=0)`返回不可变plan；`get_plan(scope,plan_id)`读取。执行计划中`pool`和`expected_runtime_s`用于排程。
- `configure_budget(account_id,tenant_id=...,limit_microusd=...,owner_id=None,project_id=None)`设置明确预算。全局/用户/项目账户可一起传给创建方法；事务全部成功才准入。
- `create_job(scope,plan_id,idempotency_key,budget_account_ids=(),initial_status='queued')`返回完整job及`created`标记。planned/blocked不预留，queued/waiting_capacity预留；waiting还须在同一事务通过现行独立容量审批并写waiter，普通worker不能领取。`enqueue(scope,job_id,budget_account_ids=())`重新检查计划期限并按不可变准入状态进入queued或waiting_capacity。金额为整数microUSD；0必须是明确0，不代表报价未知。
- `get_job(scope,job_id)`；`get_job_for_owner(tenant_id,owner_id,job_id)`；`list_jobs_for_owner(tenant_id,owner_id,project_id=None,limit=100,offset=0)`；`list_artifacts(scope,job_id)`。
- `lookup_job_by_idempotency(scope,key)`返回当前身份/项目/actor下原job或None；HTTP层在可变镜头/素材校验前先做此查找，再由create_job检查摘要，已有任务重试不受后续编辑影响。
- `request_cancel(scope,job_id)`确未提交时立即取消并释放任务预算；提交之后保留预算，返回cancel_requested，不证明停止。任务状态与原attempt证据冲突或证据缺失时进入recovery_hold并记录取消意愿，不把错误queued/claimed标签当作成本0。等待任务的取消不会释放共享实例预算。

`TaskQueue(repo).claim(worker_id,pool,lease_seconds=90,purpose='generate')`返回`Claim(job:dict,lease:Lease)`或None。
`Lease(job_id,attempt_id,worker_id,fence,expires_at)`不可变。所有下列状态写入都传lease；旧fence/过期/其他worker拒绝。

1. `begin_submission(lease)`先持久化，再由调用方POST。第二次begin_submission拒绝。
2. `record_submitted(lease,upstream_task_id)`；响应未知调用`mark_submission_unknown(lease,error_code=...)`。后者释放本地租约但保留预算，不可再purpose=generate领取。
3. `heartbeat(lease,lease_seconds=90)`返回更新expires_at的Lease。
4. 上游已完成后`begin_collection(lease)`；`collection_failed(lease,error_code=...,retry_after_s=30)`保留collecting，清租约。`claim(...purpose='collect')`继续原attempt，只重收集。
5. `claim(...purpose='reconcile')`只领失联running/submission_unknown/cancel_requested。查证现有上游任务，不允许调用begin_submission。
6. `complete(lease,artifact_specs,actual_cost_microusd=...,settlement=None)`。每条spec需kind、object_key、size_bytes>0、64位小写sha256、validated=True；可选content_type/width/height/duration_s/fps/has_audio，其他字段拒绝，避免夹带签名URL或上游秘密。collector须先真实持久存储/媒体校验。可选settlement(connection,specs)只做数据库工作，与artifact/任务终态同事务结算已有存储预留，不能在持锁回调中联网。取消后晚到成片保留succeeded及result.completed_after_cancel_request，不假称上游被停止。actual_cost=None仍完成作品，但billing_status=pending保留预算；受权后台之后settle_completed_job(scope,id,actual_cost_microusd=...)，相同结算幂等、不同金额冲突。
7. `fail(lease,error_code,actual_cost_microusd=...,upstream_stopped=False)`提交之后必须明确确认上游结束；`confirm_cancel(lease,upstream_stopped=True,actual_cost_microusd=...)`仅用于确认停止。成本不明时终结业务状态但保留预算与billing_status=pending；未提交的失败必须明确成本0。
8. `recover_expired()`只将有原attempt且确无提交意图/上游ID的claimed重新排队；缺失证据进入hold，存在提交证据转submission_unknown。submitting→submission_unknown，running/collecting保留阶段等待对账/收集，不新生成。
9. `release(lease,retry_after_s=5,error_code=None)`让出running/unknown/collecting/pending-cancel租约，保持原阶段/attempt；`defer_unsubmitted(lease,retry_after_s=30,error_code=...)`只能在claimed且尚无提交意图时返回queued，预算保持。

`pending_events()/acknowledge_event(event_id)`提供至少一次outbox投递；事件确认不是生成成功。
`configure_pool(pool,max_instances=0,max_physical_gpus=0)`默认不允许实例。
`configure_capacity(max_instances=0,max_physical_gpus=0)`设置跨池总门槛；未设置/0拒绝真实GPU登记与创建意图。降低门槛不会释放已有资源或停止计费。
`reserve_instance_intent(scope,pool,intent_key,physical_gpus=1,slots=1,reserved_cost_microusd=0,hard_deadline=...,budget_account_ids=(),dry_run=True,provider='unknown')`只预留意图，绝不租机。启用写入时同时锁跨池总容量、池上限和预算；creation_unknown保留预留。真实provider须准确指定，未知provider保守占用，不能推测与登记槽属于同一实例。已知provider/instance的意图与登记设备按较大物理卡数计数，防止同一实例重复计容量。
`update_instance(intent_id,state,provider_instance_id=None,destruction_confirmed=False,actual_cost_microusd=None)`须明确供应商物理销毁事实才释放实例意图容量；未知实际账单仍保留预算pending，后续独立settle_instance_cost。相关未核清任务的设备绑定也不会因此自动释放。hard_deadline/任务lease到期本身不会停止计费。

autoscale模块是无副作用预测；默认dry-run/max0。recommend返回none/dry_run/propose，不是租赁授权。调用方必须持久保存连续观测、在事务中预留唯一intent，另行获准执行provider动作。
异常为NotFound、Conflict、BudgetExceeded、InvalidTransition、LeaseLost；HTTP层自行映射404/409/429等。未授权资源统一NotFound。输入与provider证据不得含秘密或签名URL。

## 持久执行槽

`WorkerSpec(worker_id,pool,provider,instance_id,physical_gpu_ids,recipe_ids,model_id,configuration_id,backend='comfy-worker')`由受权操作员提供真实实例和GPU标识；GPU与recipe为显式唯一tuple。一个worker是一个执行槽，TP2占两张物理卡但仍只能执行一个任务。spec绑定后不可静默改名/换模型；退役再以新worker ID明确登记。所有control表使用同一`repository.metadata`，管理命令不需另注册metadata。

- `WorkerControl(repo,registration_seconds=120).register(spec)`登记设备所有权，初始registered。相同provider/instance/GPU不能由另一worker同时拥有；先锁全局容量门槛，跨池共用上限。登记不证明就绪。
- `mark_ready(worker_id,upstream_idle_confirmed=True)`只接受明确空闲事实，未结束的current_job阻止就绪；安全重排queued须最新attempt无submission/upstream ID。此操作明确恢复准入并清drain_requested；端口能连、历史reported_working、租约过期均不是空闲事实。
- `claim(worker_id,pool,purpose=...,lease_seconds=...)`在一事务内领取并绑定槽。必须精确匹配backend、pool、recipe、请求中的model ID和execution_plan.configuration_id。浏览器不能指定这些控制权限。
- `pool_status(pool,model_id=...,configuration_id=...,recipe_id=None,backend='comfy-worker')`只读返回matched_slots及ready/busy/unknown/registered/draining/retired计数、observed_at。ready需有效登记心跳且无current_job；leased/reconciling归busy；过期归unknown但不改库/释放设备。它是注册心跳证据，不是实时GPU或模型资格测量。排队准入可使用精确匹配且资格未过期的ready+busy，失联/draining不作为健康容量；实际领取仍只在ready。
- `observe(worker_id,job_id)`按已提交账本事实更新槽；生成未知保持占用，安全未提交defer可归还槽。queued必须有原attempt且无提交意图/上游ID才视为安全，标签冲突或缺失证据保持unknown/current_job；retire也使用相同证明，不能释放未知占用。
- `heartbeat(worker_id,fence,lease_seconds=...)`续登记心跳；旧fence/过期拒绝。`recover_expired()`改unknown，保留设备所有权。
- `drain(worker_id)`持久设置drain_requested，停止新生成，仍可核对当前attempt；标记贯穿租约过期、核对和当前作品完成，不因此恢复新生成。`retire(worker_id,upstream_idle_confirmed=True)`只在当前任务已结束或安全未提交时释放设备登记，绝不销毁云实例/释放云账单预算。

此Python接口是受信控制面，不自带用户鉴权或机器认证。对HTTP公开时必须由API另行鉴权，不能将register、预算修改、结算直接交给普通用户或映序浏览器。
# 调度快照读取边界补充

`TaskQueue.claim` 的候选查询只读取 id/tenant/owner/created_at 调度标量，不把池内全部 prompt/素材/执行 JSON 读进内存。按原 owner 公平性与 15 分钟防饥饿顺序选中候选后，仅锁定读取该 job，并再次检查取消、lease 和 not_before，最后返回该 job 的完整不可变快照。候选仍按完整池内标量排序，未以简单前 N 个截断造成 owner 饥饿。

`WorkerControl.claim` 在数据库查询中按 exact backend/model/configuration/recipe 过滤，保持 nested request model 优先级；中选 job 仍通过原 `matches` 校验。额外内部参数 `job_filter`/`validator` 用于受信调度控制面，不接收用户表达式。旧 `job_ids` 调用继续有效。

`list_jobs_for_owner(..., summary=True)` 和 `list_jobs(scope, summary=True)` 供 API 列表显式使用；默认仍返回完整行。摘要只投影 client_ref/recipe/effective request/simulation/backend 及原标量/result，保留现有 public_job 字段、missing/null 与 boolean 类型，省去 assets/sources/内部编译快照。有效提示词仍按原 API 协议返回；详情/worker 继续使用 get_job。

`list_artifacts_for_jobs(tenant_id,owner_id,job_projects)` 接受最多100个已由调用者核准项目权限的 `{job_id:project_id}`，再按 tenant/owner/project 三项核对，返回 `{job_id:[完整artifact行]}`。任一项目/账户/ID不匹配即 NotFound；存在检查不读取 job JSON。普通 list_artifacts 也使用标量权限检查。

# 2026-10-04 可靠性补充

`list_jobs_for_owner(..., project_ids=None)` 与 `list_documents(..., allowed_ids=None)` 在 SQL 分页前过滤授权 ID。None 保持原有完整 owner 范围；空集合返回空，不表示无过滤。授权集合限4096；仍与 tenant/owner 和显式 project_id 取交集，不能放宽已有身份范围。

`recovery_hold` 不属于任何自动 claim 阶段。普通取消仅记录 `result.recovery_cancel_requested=true`，保留 hold/原 attempt/未知预算；所有租约操作对 hold 拒绝。Control 观察 held 当前任务保持 unknown 和占槽，不算健康排队容量。没有普通 API 自动解锁 hold；恢复仍需受信操作员核验。

`recover_expired(summary=True)` 用标量 JOIN 读取过期租约和原 attempt 证据，不加载 request/执行快照。worker 使用此路径；默认 summary=False 仍返回完整 job，兼容旧 Python 客户端。只有原 attempt 无 submission_started_at 和 upstream_task_id 的 claimed 才重排；存在提交证据继续 submission_unknown，缺失 attempt 进入 hold。普通 generate claim 遇到错误重排但原 attempt 已提交，也进入 hold，避免第二次付费提交。

零工作机冷启动的持久waiting、审批和本地激活闭环已有离线实现；真实租机/boot/controller装配仍未启用，契约见 `COLD-START-CONTRACT.zh-CN.md`，不能把上述恢复功能当作公网首单自动租机能力。
