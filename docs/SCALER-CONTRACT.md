# 持久扩容协调器（默认关闭）

`studio_platform/scaler.py` 将纯函数 `autoscale.recommend` 接入持久账本。本轮只有注入的本地 fake provider 测试，**没有接通 Lium/Targon/Vast，没有租机或销毁真实机器**。`ScaleCoordinator.enabled=False`、`DisabledProvider.enabled=False`、`ScalePolicy.dry_run=True`、最大实例/物理卡数为 0；部署镜像中存在此模块不代表生产扩容已开启。

```python
from studio_platform.scaler import ScaleCoordinator, LaunchSpec
from studio_platform.autoscale import ScalePolicy

coordinator = ScaleCoordinator(repo)  # 不写数据、不联网、不创建进程
result = coordinator.tick(
    "controller-1", operator_scope, "h3-qualified-pool", demands, slots,
    policy=ScalePolicy(), launch=None, budget_account_ids=(),
)
```

开启控制循环前必须由 operator 同时明确配置：协调器 enabled、可信 provider adapter、非 dry-run 策略、有效 `LaunchSpec`、实例和全局物理卡 gate、现有预算账户、预留金额、硬 TTL。用户输入不能覆盖这些值。`LaunchSpec` 只接受 provider/configuration/model/region/offer/image 的无认证 ID；不保存 SDK key、SSH 私钥、任意 URL、启动脚本或自由 JSON。CPU 合成不通过这个 GPU 扩容协调器租机。

## 接口和状态

- `ScaleCoordinator(repo, provider=None, enabled=False, leader_seconds=60, min_observation_s=15)`。
- `tick(leader_id, scope, pool, demands, slots, *, policy=ScalePolicy(), launch=None, budget_account_ids=(), on_intent_reserved=None, before_create=None) -> dict`：一次控制周期，返回安全状态码/原因/intent UUID，不返回原始 provider 响应。前者是事务内纯数据库关联回调；后者在sole create前再查有效审批/需求，不接受用户任意回调或配置。
- `acquire(pool, leader_id) -> LeaderLease | None`：同池 CAS 租约/fence；失联不释放物理卡。新 leader 先处理前任的 intent。
- `settle(intent_id) -> dict`：只轮询已确认销毁实例的实际账单。未知费用继续 pending，重复同金额幂等，不同金额报冲突。
- `Repository.reserve_instance_intent(..., connection=None)`、`update_instance(..., connection=None)` 可在协调器的 leader/预算同一事务内调用；普通调用方式保留。
- `Repository.settle_instance_cost(intent_id, actual_cost_microusd=...)`：独立费用核对，要求实例已确认 destroyed。

调用者提供的是 operator 观测，`Demand` 只含安全 job/owner ID、时间、耗时与样本置信度；`Slot` 是执行槽而不是卡数。观测最多 4096 个 demand、128 个 slot。默认至少间隔 15 秒，至少连续两次超标才建议扩容；策略/manifest/预算账户或归属变化重置连续观测计数。未知模型耗时不按虚构速度扩容。

四个新表都在 `repository.metadata`，由 `Repository.create_schema()` 创建：

| 表 | 用途 |
|---|---|
| `platform_scaler_leaders` | 单池 leader/fence、冷却时间、观测序号 |
| `platform_scaler_observations` | 标量负载快照、纯函数决策，追加记录 |
| `platform_scaler_actions` | 唯一 intent、固定 launch manifest、已开始创建/销毁的标记 |
| `platform_scaler_receipts` | 追加的白名单事实，便于接管晚到响应 |

新表不会重建旧表。物理卡、实例和预算仍复用原有 Repository gate/reservations；不维护另一套平行预算。

## ProviderProtocol

可信 adapter 必须实现 `enabled: bool`、`provider_id: str` 和以下方法，网络调用必须有自己的有界 timeout。provider_id 必须精确匹配 launch/既有 intent 的供应商；不匹配时不能向另一供应商对账、销毁或取账单：

```python
create(tag: str, launch: LaunchSpec, *, hard_deadline: float) -> ProviderFact
reconcile(tag: str, instance_id: str | None) -> ProviderFact
destroy(tag: str, instance_id: str) -> ProviderFact
billing(tag: str, instance_id: str | None) -> int | None
```

`tag` 永远是持久 intent UUID，provider 的标签、请求 ID 和实例 ID 的准确关系必须实证。`ProviderFact` 只允许 `unknown/not_created/starting/running/destroyed`、无认证 instance ID、实际费用（micro-USD）、idle proof、idle_since、authoritative absence proof。VM `running` 仅代表供应商状态，**不会自动成为已通过模型资格审核的 ready slot**。独立 worker 的注册、配置匹配、心跳和 qualification 仍由 WorkerControl/ExecutionPolicies 负责。

`not_created` 必须有权威的未创建证明和实际费用 0；普通列表暂时找不到标签不是证明。`idle_confirmed=True` 必须检查专属 Comfy 队列/执行状态，不能从 VM running、没有用户点击或 lease 过期推断。记录的 idle proof 只在 30 秒内有效。实际费用必须来自账单，历史报价和预估金额不得填到 actual cost。

## 失败和销毁边界

1. 同一事务内预留既有预算/容量，写唯一 intent 和 `create_started_at`，提交后才调用 create。创建超时、崩溃在持久标记与 POST 之间均保持创建未知；绝不自动重发、也不因同池队列压力再租一台。
2. leader fence 丢失后只能追加晚到的安全事实，不能推进状态或开始破坏性操作。接管者先消费有实例 ID 的晚到事实，再向 provider 对账；空的 eventually-consistent 查询不能抹掉已知实例 ID。
3. TTL 到期只请求 drain。running、submitting、submission_unknown、collecting、cancel_requested 或未核清 worker 仍阻止主动 destroy；失联不是 GPU 空闲证明。销毁前还需要当前 provider/推理队列 idle proof。
4. 已提交 destroy 的超时不重复发送；保持 destroying 和占额，直到 provider 明确返回 destroyed。销毁中/已销毁实例不能被重新 mark_ready。
5. 确認 physical destroyed 后意图容量可以释放；未知账单仍占预算。相关任务未核清时设备绑定继续持有，任务不会被标成 succeeded。只有确认无当前任务的 slot 才可 retire。
6. 供应商独立硬 TTL 可能终止运行中的任务。协调器如实记录机器已消失，任务仍需对账/失败或重收集处理；不会伪造可下载成片。

基础设施 invoice 应在其专属/全局账户结算。用户 job 的估计额度或服务价格是另一个用途，不能把同一租机 invoice 在同一全局费用账户重复结算两次。未结账记录不能作为可释放额度。

## 本轮验证与边界

`test_platform_scaler.py` 使用临时 SQLite 和显式 localhost 测试 PostgreSQL 的独立 schema，注入 fake provider；没有真实云网络。覆盖重复 controller、持久提交窗口、创建/销毁响应丢失、leader 切换、跨池 gate、实际账单幂等、unknown 预算保留、TTL 与未核清任务。独立 `LiumProvider` adapter 代码已有默认disabled的离线实现（见 `LIUM-PROVIDER-CONTRACT.md`），但尚未装配生产租机循环；`capacity_cli`只做只读预览或本地waiting推进，不构造供应商。真实boot、供应商验证、实时GPU观察和线上负载仍需受控接入。
