# Lium GPU 接入 Sixnine 生产队列：最小实施设计

检查时间：2026-10-04，Australia/Sydney。只读了仓库、中央资源/profile 元信息与隔离验收 runner；本文件不证明云端已经切换。

本轮新增 `studio_platform/lium_runtime_aws.py` 与离线测试。没有租赁、启动/停止服务、创建 AWS Secret、改变 IAM、迁移数据库或写入真实凭据。当前本地 GPU 验收属于独立 SQLite/tenant；它的成功不能当作 `www.sixnine.art` 已能生成。

## 应走的实际路径

`用户/PAT → www.sixnine.art API → 现有生产 PostgreSQL jobs/attempts/budgets → 同一 AWS CPU 主机上的 gpu-controller → 私有 SSH tunnel → Lium ComfyUI → CPU worker 下载、解码核验、写本机对象存储与 artifacts → 原网站授权内容路由 → artifact.adopt/shot.select`

GPU 主机只获得模型公开下载来源、固定 bootstrap/manifest、Comfy 请求与该请求所需素材。生产 PostgreSQL、AWS/Lium API Key、网站 PAT、对象存储凭据都留在 CPU 侧。端口 8188 保持 GPU loopback，不公开免认证 ComfyUI。

最短接入继续使用现已部署的 **local 对象存储**，controller 与 API 挂载同一 `/srv/sixnine/platform-data` 到 `/data`。`Fleet.create_store` 目前只接受 local/R2，API 对 S3 也要求显式注入适配；这次不把切换 S3/Hippius 混入生成验收。不同机器各自的 `/data` 不是共享存储，不能用相同路径名冒充。

## 已具备的代码与必须补的接线

| 环节 | 已有依据 | 生产缺口 / 最小实施 |
|---|---|---|
| Lium 精确实例/预算控制 | `lium_provider.py`、`scaler.py`：先登记 intent/预留，单次 create，unknown 只 reconcile，供应商 TTL，销毁账单核验 | 生产 controller 要读生产 PG，不能继续 `.platform-gpu-live/ledger.sqlite3` |
| 下载/模型启动 | `lium_bootstrap.py`，source SHA、固定 Comfy/模型 revision、单卡、SSH marker | `Dockerfile.platform`/`.dockerignore` 没有包含 `bootstrap_cloud.py` 与 `model_manifest.json`；controller 专用镜像或精确只读挂载这两个公开文件 |
| CPU worker | `fleet.py`、`worker.py`，Comfy tunnel、request tag、GPU/worker lease、产物校验 | 在 controller 同一容器启动 Fleet 子进程；它们才能访问 controller 的 `127.0.0.1:1888x`；不能拆到另一个容器却保留 loopback endpoint |
| 业务准入 | `ExecutionPolicies`：qualified envelope、报价/预算、worker 心跳、cold-start approval | API 与 controller 同挂一个 operator-owned policy 文件，且都设置 generation/backend；policy 不得由用户请求覆盖 |
| 冷启动 | `ColdStartCoordinator.tick` + `approve_capacity` | `capacity_cli.py` 有意不接 cloud adapter；需要新的生产 runner 注入真实 scaler/provider/guards；仅 `--mode advance` 不会租机 |
| 排队扩容 | `ScaleCoordinator.tick` 需要调用者提供 demands/slots | 从生产 PG 查询当前配置、tenant、owner 的 queued jobs；剔除其它池和已终结任务；starting 机器计入未来容量，unknown 机器继续占预算/卡数 |
| 密钥 | 新 `AwsLiumLoader`，provider 已有 `loader=` 注入口 | 批准的 Windows→SM 受控进程内导入尚未发生；EC2 角色的 exact Secret Get 权限/运行身份需要部署方处理 |
| 生产 Compose | 现 app/db/db-init/caddy，app 无公网 egress | 新增独立 controller（database + edge），API 保持 internal 网络；同 UID/GID 10001、无 published ports、无 Docker socket |
| 部署校验 | `check_config.py` 精确 allowlist 4 services、开关全部 0、health 要求 disabled | 必须新增明确的 GPU 验收 profile 并连同渲染后 Compose 测试更新；不能绕过 checker 或整体放宽白名单 |

中央正式资源关联：`provider-lium-h3-control`；已核对 profile `lium/lium--rig-root`，base URL `https://lium.io/api`，变量名 `LIUM_API_KEY`。中央 `reported_working` 是历史资料，不是本次生产生成证据。

## Linux 凭据适配（本轮已实现，尚未线上验证）

Windows DPAPI 库不能直接在 EC2 Linux 解密。新增 adapter 以 AWS Secrets Manager 作为明确授权的集中加密运行时后端，**不复制中央加载器源码、不回退到环境 Key 或其它 profile**。

- Secret 名称固定 `/sixnine/platform/lium`，地区固定 `ap-southeast-1`。构造器要求完整 ARN（含账户、Secret 后缀）和具体 VersionId；都属于非秘密元信息。
- Secret 内容字段为 `schema_version`、`service`、`profile`、`base_url`、`primary_key_variable`、`api_key`。前五项必须精确等于 `1`、`lium`、`lium--rig-root`、`https://lium.io/api`、`LIUM_API_KEY`。密钥值只能通过授权导入程序在进程内送入 SM，不能写入本文档/普通 JSON/命令参数/项目 .env。
- Loader 对 GetSecretValue 返回的 ARN、Name、VersionId 再次校验，拒绝未知字段、重复 JSON 键、SecretBinary、错误端点。对象 repr 不显示 API Key；异常只给固定错误码。
- 初始化/导入模块不访问 AWS；第一次匹配 profile 加载才 GET 固定版本，进程内复用。AWS 角色轮换由 SDK 处理；**Lium Key 轮换需要审查新版本并重建 loader/controller**，不会随着 AWSCURRENT 悄悄换账户。
- 注入方式：`LiumProvider(enabled=..., manifests=..., loader=AwsLiumLoader(secret_arn, version_id), idle_probe=...)`。必须先通过部署/预算开关，再创建启用的 provider。关闭时分别关闭 provider 与 loader。
- Controller 需要精确 Secret 的 `secretsmanager:GetSecretValue`；自定义 KMS key 才另需相应 decrypt 授权。使用 EC2 实例角色，不放静态 AWS access key。不得把 AWS role 凭据转给 GPU。
- 当前 EC2 metadata 的容器可达性没有在此检查。若容器使用实例角色，部署方需验证 IMDSv2 路径/hop-limit 和仅 controller 可访问的网络边界；不要为了取角色而赋予 app 公网 egress、host network 或 Docker socket。
- SSH 私钥不属于 API credential 迁移；单独由部署方在 CPU 受限安全位置提供，controller 只读挂载。`known_hosts` 是 controller 的持久状态；新租机首次信任必须明确启用，后续不忽略 host-key mismatch。

离线验证：`python -m unittest test_platform_lium_runtime_aws test_platform_lium_provider -v`，33 tests passed。覆盖不触网构造、固定 profile/version、并发只读一次、元信息冲突、敏感异常遮蔽、环境不变，以及通过现有 LiumProvider + HTTP mock 完整装配。没有使用真实 AWS/Lium 凭据。

## 生产 runner 的有限验收配置

新 runner 默认 disabled。仅运营入口可启用；不新增给普通用户的租机管理 endpoint。

1. **身份和状态唯一性**：`tenant='sixnine'`、验收 owner `superdan`、固定验收 project ID、独立 production pool/configuration ID。使用生产 DSN secret 挂载，绝不能退回 SQLite 或混入 local-test auth。`Settings.from_environment` 默认 tenant 恰为 sixnine，但要运行前显式校验。
2. **共同预算上限**：本轮用户总计 $50 已含本地两卡测试。先以本地账本/供应商 statement 核清已有花费与未释放预留，再批准生产剩余额度；不能另建一个全新 $50 当新增预算。生产 budget 限额不得超过已证明剩余值。
3. **Owner 限定**：execution policy 的 budget template 使用本轮唯一前缀，例如 `h3-prod-acceptance-20261004-{owner_id}`，只配置 `owner_id='superdan'` 的账户；同时设置 production shared ceiling。这样 supervan 或其它 owner 即使知道配方也不能消耗本次验收资金。查询 demands 再限定六项：tenant/owner/project/pool/configuration/状态。
4. **限定 envelope**：只使用真实 smoke 已验证的 `h3-base-fl2va-v1`、原版 BF16、4 秒、480P、4 steps、音频开启、tiled 视频解码、CPU encoder、实际 sampler/scheduler 等。无 reference/first-last/guides 的 smoke 不授权其它控制。4 步只能证明完整运行链，不能宣称高质量或所有功能完成。
5. **同一 ledger**：`configure_capacity`、`configure_pool`、`configure_budget` 是独立、显式的一次性运营配置；常驻进程只核对，不能每次启动重新扩大预算/卡数。第一次最多 1 卡；若要生产两卡扩容证据，预先明确 max=2，新增实例整段 TTL 的费用必须逐次预留。
6. **启动和扩容**：冷启动 approval 绑定 exact policy hash、配置、quote/qualification expiry、deadline、launch manifest。先 `ColdStartCoordinator.tick` 处理其唯一首台 intent，随后读取 matching queued demands 由同一 `ScaleCoordinator` 决策第二台。统一 leader ID/锁，不让冷启动与常规扩容轮番改变 policy/launch hash、重置连续观察计数。最小实现可以先只做 warm 单卡生产 E2E，随后另验收 cold-start，不能把两个阶段混称已通过。
7. **不重用模糊状态**：每 intent 持久绑定 loopback port；不要按每次查询返回的数组下标分配端口。worker/fleet marker、来源 SHA、已提交 task ID 都在持久 `/control`。重启时 `fleet_recovery_required` 留待明确恢复，不能删除 marker 后自动重跑。
8. **剩余时长**：租机前核对 provider TTL/price/cap；bootstrap 剩余时间检查包含 190GB 下载、环境安装、smoke，再给实际请求留足时间。无依据不能把历史 1200s cold-start 当服务承诺。剩余时长不足只拒绝新任务；不取消已提交未知任务来假装安全归零。

建议控制器进程内顺序：读现有 PG 状态→reconcile 所有本池已知 intent→tick 对应 BootController→检查实际 ready/busy worker 心跳→激活 waiting_capacity→根据 matching queued demand 作扩容决策→输出只含 ID/state/count 的状态快照。`ScaleCoordinator` 自带 fenced leader，但 bootstrap/本地端口与子进程仍需整个服务的唯一控制器锁。

两条路径的关键区别：`ColdStartCoordinator` 的一个 approval **终身只创建一个 bootstrap intent**；不能靠重复调用同一个 approval 实现第二台或销毁后重新常驻租用。第二台采用明确允许的普通 scale turn；本次结束后审批失效，持续生产自动付费另行开启。

## 4GiB CPU 主机约束

当前 mem_limit 为 API 2GiB、PG 512MiB、Caddy 128MiB，另有 Docker/OS、SSM 及初始化进程。新的 controller 除管理线程外还含每 GPU 一个 Python worker、媒体完整解码校验、上传准备和 artifact collection。不能把它当零 CPU/零内存的转发器。

`media_process.py` 的 2.25GiB 是每进程虚拟地址空间上限，**不是 RSS 预估，也不是两 worker 合计上限**。API upload 的 ProcessingAdmission 也是进程内，不能约束另外的 Fleet 子进程。

- 最小生产一片验收可先限定 1 GPU/1 worker/小规格，给 controller 明确 cgroup 内存/PID/CPU 上限，并测峰值、OOM/restart 与下载/校验阶段。上限应由真实容器测量确定，本次未证明 4GiB 可以同时稳跑两个后处理进程。
- 两 GPU 并行生产若仍用本机 local store，优先让 CPU 主机具备足够内存余量（例如 8GiB 再测），或补跨进程媒体准入/分离 artifact 后处理；不能只把内存限制加总超过主机就算设计完成。
- 本轮 render 仍为 0，不加入 CPU roughcut worker。未来远端 render 或 S3/R2 是单独验收。
- 同步观察 PG 连接总数：Repository 每进程独立 SQLAlchemy pool；两个 worker + controller + API 都算连接消费者，现 PG max_connections=64。先测并维持余量，不擅自认为只有 API 一份池。

## 部署/验收顺序

1. 根任务先收尾隔离本地两卡实验，或明确划定互斥的预算份额。不要在两个账本同时控制同一 pod，也不要仅复制 pod ID 就把现运行机器认作生产资源。
2. 部署方在授权进程内将已选中央 Lium profile 写入固定 SM secret，核对返回 ARN/version；仅向 controller 角色放行 GET。记录 metadata 到中央 inbox，保留 Windows 原配置。
3. 准备 CPU SSH key/known_hosts、公有 bootstrap/manifest、只读 policy/approval、持久 control work dir。镜像与源 revision 固定；所有路径先核对存在，bind 禁止自动创建。
4. 修改专用 Compose 验收 profile、精确 checker 与 tests。API：generation=1/backend=comfy-worker/执行 policy 文件；render=0、password auth、原local storage与internal网络保留。controller：相同 DSN/data/policy，启用仅本次有限 scope，database+edge、无published port。
5. 先以 provider/boot smoke 完成硬件与文件核验，再建立当前 policy。启动 Fleet 后从生产 PG 确认 exact GPU UUID、model/configuration/recipe、有效心跳，不能只看 SSH 通或 Comfy `/queue`。
6. 使用 **真正 HTTPS 生产 API** 下的 superdan session/PAT：创建验收故事→章节/场景/镜头→POST generation-plan→检查 blockers/预算→POST jobs 原始 Idempotency-Key→poll 原 job→GET artifacts→下载/校验→guided artifact.adopt/shot.select（音频另 adopt）。不要把 TestClient 本地调用当公网验收。
7. 网页打开对应云故事，确认版本升级、视频可播放/下载、独立 FLAC 存在；未保存草稿仍由用户决定是否加载云更新。记录生产 project/job/artifact ID、worker/GPU ID、实际时长/配置、产物 SHA 和生成范围；不用截图代替 artifact 成功证据。
8. 若验收扩容，须生产同一队列的 backlog 导致第二条 create intent，两个独立物理 GPU 的 attempt 有重叠运行时间；仅两台 RUNNING 不算并行，手工假 demand 不算自动扩容。
9. 完成后关闭新准入/本轮 approval，不把 controller 直接关掉弃管。继续 reconcile running/unknown、收齐产物，然后 drain；供应商空队列 + 无活跃/未知 attempt 才走 DELETE。以 exact removed statement 核实两实例销毁，账单缺失标记 pending，不当作零费用。
10. 最后恢复生产 generation gate 到关闭或明确展示“本轮验收结束”；保留作品可读。有限验收不等于用户已授权持续自动租机。

## 已知恢复限制与操作禁区

- `BootController` 的 start marker、smoke marker、fleet marker 对未知提交采取保守停止，避免重复收费；但这不是自动恢复所有故障。恢复方案还需确认存活子进程、上游 queue/history、当前 attempt/receipt。
- `ScaleCoordinator._drain_or_destroy` 在 worker lease 已失效或 attempt 未决时拒绝主动销毁，即使上游刚空闲；provider TTL 是独立最终边界。不能删 ledger/worker 行来解除阻塞。应继续协调未决请求或等待 TTL 后核验账单。
- `healthz.cloud_creation_enabled` 当前固定 false，描述 API 本身不租机，不代表独立 controller 已关闭。控制器须有单独非秘密 readiness/status 与预算/期限状态。
- 生产 enable 需要 API、policy、worker settings、capacity/global/pool/budget、cold-start approval 等一致。只改 `SIXNINE_GENERATION_ENABLED=1` 不会自动获得可运行 GPU。
- 当前 runner `.platform-gpu-live/live_control.py` 是实验脚本，带 Windows 路径、临时本地 tenant/SQLite、人工 qualification demand 和文件 flags；不要原封不动部署为常驻生产服务。

待根任务执行/核实：SM 导入与IAM/IMDS、CPU峰值、精确生产controller+Compose策略、剩余共同预算、真正公网job→GPU→artifact证据、全实例销毁与账单。本文不声称这些已经完成。
