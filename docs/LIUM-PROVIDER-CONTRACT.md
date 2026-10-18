# Lium 云适配器契约

原始盘点日期：2026-10-04；计费文档再次核验：2026-10-10。后文“本轮证据”保留原始离线验收范围，不代表当前生产状态；部署与授权窗口查 `CURRENT-BASELINE.md` 及对应回执。实现：`studio_platform/lium_provider.py`；离线验收：`test_platform_lium_provider.py`。这是可被协调器注入的适配器代码，不代表已租赁、已上线或已完成模型资格验证。默认 `enabled=False` 不授予付费操作权限；历史初始 dry-run 配置不描述当前生产状态。

## 调用边界

`LiumProvider` 实现 `ScaleCoordinator` 的 `ProviderProtocol`：

- `create(tag, launch, hard_deadline=...) -> ProviderFact`
- `reconcile(tag, instance_id=None) -> ProviderFact`
- `destroy(tag, instance_id) -> ProviderFact`
- `billing(tag, instance_id=None) -> int | None`
- `validate_launch(launch, physical_gpus=..., slots=..., reserved_cost_microusd=..., hard_deadline=...) -> dict`：纯本地核对，不加载凭据、不联网。协调器在原有事务的预算/容量预留之前调用；不匹配时返回 `provider_manifest_or_reservation_mismatch`，不创建 instance intent。

必须由持久协调器先提交 `create_started_at` / `destroy_started_at` 再调用适配器。协调器持久 intent 防跨进程重复租赁；适配器还有当前进程的重复提交拒绝。不要单独编写循环反复 `create` / `destroy` 的控制脚本。失联后的内存状态不可替代数据库记录。

`enabled=True` 仅是适配器的显式操作开关，不构成用户预算、实际 GPU 或部署授权。它不注册 worker，不修改执行策略，不安装模型，不建立 SSH 隧道，也不把 VM `RUNNING` 当作模型可以生成。

## 中央配置

中央入口为 `C:\Users\danmo\Desktop\AI-Registry`；已知 WSL 对应 `/mnt/c/Users/danmo/Desktop/AI-Registry`。适配器只在第一次被允许的 HTTP 操作时导入现有 `api_registry`，明确调用 `load_api('lium', profile='lium--rig-root')`。要求 profile 元信息精确匹配：

| 项目 | 必须值 |
| --- | --- |
| resource ID | `provider-lium-h3-control`（历史资源关联，不是上游实例 ID） |
| service / profile | `lium` / `lium--rig-root` |
| base URL | `https://lium.io/api` |
| primary key variable | `LIUM_API_KEY` |
| 上游认证 | `X-API-Key` 请求头；值只留在运行进程内 |

不读取旧 `.env`、不修改运行环境、不打印或序列化 config/env/key、不复制加载器源码；异常只提供固定错误码。缺失 base URL、换 profile、已导入同名但来源不同的 `api_registry` 均拒绝，不回退到 SDK 默认账户。中央记录的 `reported_working` 是历史状态；本轮没有加载真实密钥或发起真实 API 请求。

Windows DPAPI 加密库不能直接复制到 AWS 使用。AWS 的中央加载器/运行身份方案尚未完成时，适配器必须继续禁用；不能从项目文件补一份 key 或退回原 `.env`。`AI_REGISTRY_ROOT` 可指定经过核验的中央实现位置，只是非秘密路径，不是凭据。

## 受信 manifest 与费用限制

`LiumManifest` 固定 `configuration_id`、准确 `model_id`、executor UUID、template UUID、GPU 数、执行槽数、地区标记、每 GPU 小时报价上限（整数 microUSD）、终止调度参数 `termination_hours`（整数小时，1–720，不是计费取整单位）、审批有效期及公钥内容。`LaunchSpec.offer_id/image_id` 必须分别等于 executor/template UUID。模型 ID、中央资源 ID 和 template ID 不可互换；不按价格自动选其他 executor，不回退默认 template，不接受来自用户请求的 Dockerfile、SSH key 路径或启动脚本。

仅接受一条无 authorized_keys 选项的标准公钥内容；不读私钥，也不自动注册/生成 SSH key。固定 template UUID 本身不保证模板内容不会改变，镜像摘要与启动后的实际模型/recipe/依赖资格仍需单独核验。

协调器必须预留精确 `gpu_count` / `execution_slots`，预算至少覆盖 `manifest price cap × gpu_count × manifest termination_hours`，复用原有跨池容量和预算账本。预留是保守占用，不是实际账单；未知创建、未知删除、未知实际费用均保留对应占用。

官方按秒计费：pod 每小时价格 × 实际计费秒数 / 3,600，无一分钟或一小时向上取整；时钟从 deploy request 开始，包含供应商准备时间，按官方已移除流程在 removal request 后不再计费。参考 [Billing](https://docs.lium.io/pod-users/billing)，核验日期 2026-10-10。余额扣款周期和 `termination_hours` 调度粒度不是计费粒度；请求接受时间未知时不能推断已经停止计费。最终结算仍读该 pod 的 ledger `total`，不以本地时长估算替代账单，也不追溯改写已有结算。

创建前读取 `GET /executors` 和 `GET /templates`，检查精确 ID、GPU 数及 `price_per_gpu`。官方将该价格定义为每 GPU 每小时，租赁提交使用固定 `POST /executors/{UUID}/rent`、`pod_name`、`template_id`、`user_public_key`、`gpu_count`、`termination_hours`。[官方 Quickstart](https://docs.lium.io/developers/quickstart)

**GET 报价与 POST 租赁之间存在价格竞态。**固定 executor 的租赁接口未核实支持原子 price-cap 参数，所以 manifest 的 `allow_preflight_only_price_cap` 默认 false；未明确认可该限制就不能提交。即便明确开启，上述费用预留也不能被称为绝对费用上限。未来上线需验证服务器原子价格约束或由运营方另行审批风险，不允许添加猜测参数。

提交的终止调度小时数为审批 TTL 与剩余绝对截止时间向下取整的较小值，另留 60 秒提交余量；这只影响最迟停止时间，不表示把实际费用向上取整。剩余不足一小时加余量即拒绝。预检之后紧邻 POST 再核对审批与截止时间。该余量是客户端保守防护，不是已实测的供应商 SLA；服务器具体起算时间、调度延迟和自动销毁效果需真实受控验证。

## 未知状态与销毁证据

持久 instance UUID 作为 tag，pod name 固定 `sixnine-<UUID>`。读取可见 pods 的全部返回行，精确比较名称；不接受前缀、HUID 或 SDK 的 first-match 选择。有两个相同 tag、已知 pod ID 不匹配、格式异常就拒绝并保留占用。

租赁 POST 不重试、不跟随重定向、不用环境代理；请求与响应被限时/限字节且错误文本不带原始 body。响应必须明确 `success=true` 和合法 pod UUID，否则创建未知。租赁响应丢失后仅 reconcile；本进程重复 create 也拒绝。进程重启后的限制依赖持久协调器，不靠内存集合。

空列表或 HTTP 404 不能说明实例不存在。API key 的 pod visibility 可能让已有 pod 返回 404 或空列表。[官方 API key scope / visibility](https://docs.lium.io/pod-users/api-keys)

FAILED、STOPPED、REBOOT_FAILED 或其他 VM 状态不算已销毁。官方说明停止/失败等状态仍可能保留资源及继续计费。[官方 Billing](https://docs.lium.io/pod-users/billing)

删除先核对精确 tag 和 pod ID；未可见则只继续对账，不向猜测 ID 发 DELETE。DELETE 只提交一次。本轮未钉死完整 DELETE 终态 response schema，所以接受的 2xx 单独仍返回 unknown，直到同 ID/tag 的 `GET /pods/{id}/statement` 明确 `removed=true`、有效创建/移除时间才形成 destroyed fact。404 不替代此证据。removed statement 是官方持久账本资料，实例移除后仍可读；最终扣款在移除时入账。[官方 Pod statements](https://docs.lium.io/pod-users/statements)

confirmed removed 与实际费用独立：有效移除证据可以释放物理容量，缺失/非法 total 仍保持 budget pending；后续协调器 `settle` 读取相同 statement。只有 removed statement 的 ledger `total` 用于实际金额，不使用 `spend_to_date`、运行秒数×报价、余额变化或估算。金额以 Decimal 读取；账本精度高于 microUSD 时向上归整不足 1 microUSD。账单中的历史其他实例或其他 tag 不可拿来结算本 intent。

官方有 `GET /users/me/events` / pod event log，以及独立的请求 audit；本轮没有核实足以证明销毁完成的精确 event subtype，所以不把请求日志的 `pod.delete` 当作已销毁证据。未来接事件时必须提供钉死的 schema/revision 和离线 contract fixtures。[官方 SDK](https://docs.lium.io/developers/sdk) · [官方请求 Audit](https://docs.lium.io/developers/account-audit-log)

## 空闲与模型资格

VM running 只描述云主机。可选 `idle_probe(tag, instance_id)` 必须返回 `InferenceIdleProof`；匹配实例、时间不超过 30 秒、确实检查推理队列且 `idle=true` 才提供空闲事实。没有 callback、抛错、错实例、过期/未来时间或普通 dict 都不证明空闲。回调独立于 Lium pod 状态，不通过 VM running 自动得出空闲。

协调器仍要核对已注册 worker、任务/attempt、未知提交、收集状态和 drain；供应商空闲回调不能绕过这些检查。真实 Comfy queue、安全隧道、boot、镜像/权重 revision、recipe/model/configuration 实测资格与 worker 注册尚需后续实现/验证，不在此 adapter 内伪造。

## 本轮证据与剩余缺口

27 项 `test_platform_lium_provider` 全为 MockTransport / 假凭据对象；含中央 profile 选择、manifest/预算绑定、TTL、租赁响应丢失、重启恢复、重复 tag、权限 404、删除未知、最终账本与独立空闲证明。SQLite 临时库覆盖实际协调器持久 intent；隔离本机 PostgreSQL 测试只使用 `sixnine_test` 的独立 schema。`test_platform_scaler` 19 项仍通过。未连接 Lium，未生成 GPU 视频，未创建/删除真实云资源。

仍需：AWS 可用的中央凭据方案；固定 executor/template 的审批；预算/价格竞态决策；boot/资格控制器；真实 SSH/Comfy 队列 callback；ledger/template/租赁字段实际 contract 验证；供应商 TTL 行为验证；真实观察源到协调器循环的运营接入。adapter 代码和离线测试完成不等于上述项目完成。
