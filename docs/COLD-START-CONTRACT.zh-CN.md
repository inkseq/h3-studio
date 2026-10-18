# 零工作机冷启动：等待容量的离线实现契约

日期：2026-10-04。持久审批、等待任务、唯一扩容关联和验收后激活已实现；真实云 bootstrap 尚未装配。本轮仅临时 SQLite、隔离本机 PostgreSQL 和 FakeProvider 验证，没有加载真实凭据、租机或生成 GPU 视频。

## 当前行为

ExecutionPolicies.evaluate 接受两种准入：匹配且心跳有效的 ready+busy 槽允许 queued；没有健康槽时，仅有独立且当前有效的冷启动审批、现行运营策略、资格/报价依据以及已有预算/容量 gate 都匹配，才允许 waiting_capacity。没有审批或 gate=0 时仍 blocked。

API plan.status=ready 表示可以提交已批准状态，不能解读为机器已就绪。公开 execution.admission_state 区分 queued 与 waiting_capacity；审批 ID/hash、实例预算账户和私有 manifest 不公开。等待任务没有 attempt，不占执行槽，普通 worker 不能 claim；验收后同一个 job ID 变成 queued，资金不再预留一次。

ColdStartCoordinator 默认 enabled=False，默认没有 scaler/provider；缺少 current-policy guard 时拒绝审批。当前应用未装配实际租机 controller、boot 验收服务、公开审批 API 或启用 Lium 的 CLI。中央历史成功记录不授予启动权限。

## 持久记录与事务

| 表 | 作用 | 约束 |
| --- | --- | --- |
| platform_capacity_approvals | 运营批准的启动快照 | 内容/hash 不可变，enabled 可独立撤销，精确 tenant/pool/model/config/recipe/policy hash 和费用/资格截止 |
| platform_capacity_cycles | 审批的唯一 bootstrap intent | 每个审批生命周期只关联一次实例；结束/unknown/失败后不能凭同审批再次租赁 |
| platform_capacity_waiters | job 的审批/实例/等待截止 | 每 job 一行，取消、激活、失败、recovery_hold 分开，保留原关联证据 |

Repository.create_schema 在既有启动锁内创建新表并补待处理 waiter 索引，不重建原表。审批 payload 只引用 LaunchSpec、ScalePolicy、billing Scope 和已有预算账户，不放凭据、SSH 私钥或认证 URL。

创建等待 job 的同一事务预留任务预算并校验审批；幂等重复先返回原 job，后来的策略撤销不造成二次预留。直接 create_job(initial_status='queued') 不能绕过 waiting plan。planned/blocked 的 enqueue 按不可变 plan 进入 waiting；激活不使用 enqueue，也不调用 _reserve。

扩容复用 ScaleCoordinator 的 leader/fence、连续观测、全局实例/物理 GPU gate、pool gate 与原有预算账本。TP2 记两张卡和一个执行槽。准入的费用检查是预检，实例 reservation 的事务检查才是并发权威，不另建预算账本。

新增纯 on_intent_reserved(connection,intent) 回调把唯一 cycle、当前 waiter 的 intent 关联与 reservation/create_started_at 一起提交，之后才单次 provider POST。before_create 再查撤销、当前策略和活跃等待需求；明确拒绝可证明未发 POST，实例实际费用 0。异常或创建响应丢失保持未知、占额、精确 tag 对账，不能自动第二租。

取消/失败使用 job→budget→waiter，实例关联使用 budget→approval→waiter，激活使用 job→approval→waiter 且不改预算，避免任务/实例共用账户时倒置锁序。

## 受信 Python 接口

repo.approve_capacity(approval_id, tenant_id=..., pool=..., model_id=..., configuration_id=..., recipe_ids=(...), policy_hash=..., qualification_evidence_id=..., qualification_expires_at=..., quote_expires_at=..., expires_at=..., launch=LaunchSpec(...), scale_policy=ScalePolicy(...), budget_scope=Scope(...), budget_account_ids=(...), enabled=False) 仅供 operator 使用。

批准需要固定 manifest、非 dry-run 策略、正的实例预算/物理容量和绝对 hard_deadline，且匹配当前 policy hash。相同 ID/内容幂等返回，不能偷偷重新启用被撤销审批；内容变化或已经结束的 cycle 需要新审批 ID。repo.set_capacity_approval_enabled(id, enabled=False) 单独撤销。API 不接受用户指定 pool、费用、模板、账号、设备或权限。

API 最小接法：ready 时按 execution.admission_state 选择 initial_status，否则 blocked；传既有预算给 create_job。原 planned job 的 repo.enqueue 已处理 waiting，不改 API 的幂等命名空间。

ColdStartCoordinator(repo, scaler=显式注入的协调器, enabled=False, approval_guard=policies.capacity_approval_current, activation_guard=policies.activation_allowed) 的 tick(leader_id, approval_id) 是可注入一次租赁契约；没有 scaler 时明确 cloud_controller_not_configured。真实 provider、boot、秘密加载均未由此构造。

worker 新提交继续用 policies.submission_allowed，额外查审批撤销/过期；activation_allowed 是可在事务中使用的纯策略 guard。已 running/unknown/collecting 的恢复不受新提交 guard 影响，保留上游结果与独立账务。

## 已提供的运行入口

studio_platform/capacity_cli.py 有三模式，默认 disabled 在 Settings、数据库、策略读取前退出：

    python -m studio_platform.capacity_cli

dry-run 只读已有数据库/schema与受保护运营策略，不做 DDL、观测写入、注册、任务变更或供应商调用。仅输出数量、资格/预算判断和纯推荐，不输出 payload、owner、提示词、DSN 或 URL：

    python -m studio_platform.capacity_cli --mode dry-run --approval-id OPERATOR_APPROVAL_ID --once

advance 是显式本地操作：消费既有 fleet 的验收/心跳记录，激活同一 job 或拒绝撤销/过期/来源变化的等待；不构造 scaler/provider，不租机、对账或销毁：

    python -m studio_platform.capacity_cli --mode advance --approval-id OPERATOR_APPROVAL_ID --once

显式 --loop 以 1..60 秒有界间隔重复；SIGTERM/SIGINT 停止新 turn，当前事务保留可恢复状态。--data-dir 保留已设置的 SIXNINE_DATABASE_URL 或 URL_FILE；未初始化 SQLite 文件拒绝创建，CLI 不负责 initdb。

fleet 负责对已有实例的确切 slot/模型/Comfy 资格登记，advance 消费记录，两者不自动创建彼此或云资源。单次 tick 类具备注入 FakeProvider/未来 provider 的契约，现 CLI 只装配本地等待推进。故这不是首单自动租卡的完整线上服务。

## 验收、等待期限和取消

VM running 不注册模型槽。必须由可信 operator/未来 boot 组件核验固定配置后 register，再明确 mark_ready；首次激活要求 ready、无当前任务、未 drain、心跳有效，backend/model/config/recipe 全匹配。已关联 cycle 还要求同一 provider instance。registered、错误身份、过期心跳均不激活。

工作机登记是可信运营记录，不是权重证明。实际 boot 尚需校验模型 revision、Comfy/plugins revision、GPU UUID、精度/显存以及真实多模态输入输出，不能以历史 benchmark 冒充实例当前验收。

15 分钟 plan 窗口只限制确认新任务；已确认 waiting 的 deadline 取审批、报价/资格截止和 host hard_deadline 减预计运行时间，不再取 plan.expires_at。这样大型模型启动超过 15 分钟仍可等待有效审批，新的过期确认仍拒绝。来源和硬截止持续核验，不承诺固定完成时间。

HTTP plan 携带 server-owned source hash。激活前读取同 tenant/owner 的项目文档，镜头、祖先场景/章节或依赖角色/参考变化时 failed(capacity_source_changed_before_activation)，要求重新预检。没有可变项目的可信底层夹具可以不带 server hash。

审批/策略撤销、资格/报价过期、等待截止、实例 draining/destroying/destroyed 或剩余 TTL 不够，都让未提交 job 失败并按 0 释放任务预留；实例未知费用继续独立占额。一个用户取消不能销毁别人的共享实例。

上游 idle 但仍有 queued/waiting 应用需求时，不按 idle 规则提前 drain。TTL 仍可触发 drain，但不得强杀未核清的 attempt/收集；供应商硬截止属于外部事实。已确认物理销毁和实际账单结算独立，未知账单保留 pending，不宣称免费。

灾难恢复显式禁用全部 approvals，waiters 与非终态 jobs 保持 recovery_hold，容量 gate 归零；保留 provider/intent/cycle/费用证据。held cancel 只记意愿，不能重新排队或创建。不能只靠 max0 来保护恢复。

## 本地证据与实际缺口

测试文件：test_platform_capacity.py、test_platform_capacity_api.py、test_platform_capacity_cli.py。覆盖双控制器/双 owner、单 intent、TP2、共享账户并发锁序、post 前撤销、unknown 不重租、ready/过期/错误配置、同 ID 激活/公平领取、取消、来源变化、15 分钟确认窗口与独立等待截止、恢复 hold、无 DDL dry-run、CLI URL_FILE 与有界退出。供应商全部 fake，数据库仅临时 SQLite 与真实本机隔离 PG。

受影响原 repository/queue/scaler/execution_policy 的 70 项回归通过。完整冷启动测试最终数量以本任务报告为准；没有 VBench、GPU boot、在线提供商或真实账单验收。

尚缺真实 Lium 身份/manifest/租赁验收、受限 boot/资格实现、受保护审批管理、真实 controller 装配/部署、实际账单核对和冷启动性能测量。遵守中央凭据迁移边界，GPU 不拿 DB 或主存储 key；Windows DPAPI 到 AWS 的凭据方案未被本轮跳过。当前 approvals 为空、云创建关闭，不能声明公网零卡首单自动生成已经上线。
