# 有限生产自动扩容控制器

`studio_platform.production_scaler` 是可持续运行、默认关闭的生产控制入口。它读取与网站相同的 PostgreSQL 和本地对象库，由明确审批的有限周期控制最多两台单卡 Lium GPU。实现与离线/隔离 PostgreSQL 测试完成不等于已通过生产供应商扩容验收；实际机器、HTTP 任务、并发时间和账单证据必须单独记录。

本轮不是新的 US$50 授权。操作员须先核对原实验账本和剩余最坏占用，再设置既有总账、池上限和本周期额度。控制器不创建预算、不提高额度、不自动充值或续期。单任务内部预留不是额外的供应商收费。

## 入口与固定配置

默认命令只验证配置，不读 stdin、不连接数据库、不加载密钥、不调用供应商：

```sh
python -m studio_platform.production_scaler --config /control-config/scaler.json
```

显式运行需同时有 `config.enabled=true` 和 `--enabled`。生产 host launcher 从固定 AWS Secrets Manager ARN/VersionId 在内存读取凭据，通过一次关闭的 stdin 管道传入：

```sh
python -m studio_platform.production_scaler --config /control-config/scaler.json --enabled --credential-stdin
```

不要在 shell、参数、文件或日志中编写密钥。stdin envelope 为 `secret_arn`、`version_id`、`payload` 三字段；payload 使用中央 profile 元信息 `schema_version/service/profile/base_url/primary_key_variable/api_key`。ARN/version 必须与配置完全相同，服务固定 `lium/lium--rig-root`、`https://lium.io/api`。最多 24 KiB、重复/未知字段拒绝；复用现有 AwsLiumLoader 元信息校验，但注入纯内存读取器，不向 AWS 发请求。GPU、工作子进程均不继承此管道。没有 `--credential-stdin` 时可使用已审批的 AwsLiumLoader；生产 Docker bridge 路径优先用 stdin，不要求放开 IMDS 或给容器其他 Secrets Manager 权限。

全部配置字段以 `FiniteConfig` 为权威。无密钥的配置须受保护、完整提供默认 `interval_s`，并固定：

- 身份：`version=1`、`enabled`、`cycle_id`、`tenant=sixnine`、`owner`、`project_id`、独占 `pool/configuration_id`、`capacity_approval_id`、既有 `budget_account_ids`。
- 时间：`created_at`、`hard_deadline`，最长四小时；`drain_margin_s`、`collection_margin_s`，不允许运行时延长。
- 路径：`work_dir=/control`、`data_dir=/data`、`source_dir=/bootstrap-source`、`ssh_key_file=/worker-identity/key`、`known_hosts_file=/control/known_hosts`。SSH 私钥仅独立只读挂载，Linux 不允许 group/other 权限；不是 API 凭据迁移对象。
- `trust_first_host_key` 必须显式选择。为新租节点允许 TOFU 是首次信任权衡，不是已验证主机指纹；后续同位置 host key 变化仍拒绝。启用 TOFU 的 known_hosts 必须位于该周期 control 目录。
- `source_sha256` 精确绑定 `bootstrap_cloud.py/model_manifest.json` 原始字节；模型与 Comfy revision 另经固定 manifest 核验。只挂这两个公共源码，不能挂整个项目、`.env` 或用户资产。
- `execution_policy_sha256` 是 `repository.request_hash(policy)` 的规范 JSON 摘要，**不是 policy 文件字节 SHA**；还绑定 `qualification_evidence_id`。
- `port_start` 和持久 `cycle-state.json` 分配一 intent 一端口，排序变化/重启不改绑定、不复用已销毁 intent 的端口。
- 完整 `scale_policy`、顺序明确的 `launches` 与一一对应 `manifests`：最多两个不同 executor，各单卡/单 slot；必须为完整 TTL 预留报价上限。供应商不提供原子价格上限时，只能承认预查报价限制，不能声称绝对最终账单封顶。
- `secret_arn/secret_version_id` 仅为固定元信息。未知字段（包括 `api_key`）不能塞进配置。

此文记录历史 Comfy scaler 契约；当前域名为 `https://inkseq.com`，当前存储与调度以已批准发布和配置为准。原版本要求生产 `postgresql+psycopg`、密码认证、同 `/data` local store、comfy-worker。入口拒绝 SQLite；测试注入的 Repository 才可用 SQLite。该历史版本不实现在线改存储 provider 或跨 provider 双读。

## 调度与资格

首次创建必须来自已有 ColdStartCoordinator 的真实 `waiting_capacity` 任务，无人工假积压。原 job 激活为 queued，不创建第二份用户任务。随后只投影同 tenant/owner/project/pool/config/policy 的真实 PostgreSQL queued 记录，不把完整项目快照加载为扩容观测。最多观察 4096 个候选；超过窗口会停止新创建并进入保守清理，不代表支持任意队列规模下的吞吐保证。

数据库 fenced leader 与已有全局/池账本防止重复创建；单进程独立 leader ID，主机 control 文件锁防止同容器目录并行控制。意图与完整 TTL 预留在 provider POST 前提交。丢创建响应只对账，unknown 不变免费容量，也不触发替代租赁。每份 manifest 在一个周期最多使用一次；用完两份不无限重新租。

每台 GPU 按固定 BF16/CPU encoder 模式下载固定 revision，并跑 **FL2VA、请求 5 秒、768P 16:9、50 步、有声** 的独立资格。不能把历史默认四步 smoke 冒充此资格，也不借用其他 GPU 的结果。五秒请求的 H3 原生输出是 124 帧 / 24 fps；正式操作员策略应把原生时长上限精确设为 `124/24`，避免将较长的小数秒请求混入五秒资格。入口兼容性校验的六秒上界本身不代表六秒资格。策略限 FL、无参考输入、无 guides/首尾帧、已验控制组合；Ref2VA 和其他控件须另行资格与适配。

资格 POST 也受独立租期检查：首次提交前重新读取该实例实际安全截止，须至少保留 **1200 秒 + collection_margin_s**。该二十分钟是此 FL50 范围的保守准入余量，不是速度承诺；下载太晚则停止本轮并进入 drain，不尝试抢在 TTL 前提交。已经提交但结果未知的资格任务仍继续对账与收集，不能因余量变短丢弃它。

资格完成才登记可执行工作机。fleet 子进程与 SSH tunnel 在同一 CPU 容器，持有网站相同的 PG 与对象库；GPU 只有私有 ComfyUI，不持 PG、对象库或 Lium API 凭据。两台 GPU 可以同时生成，但资格收集与业务结果收集共享 `/control/collection-lock`，同一时间只运行一路 CPU 编码/校验；等待收集的子进程持续续租账本。1 GiB 容器预算仍须在实际两子进程环境验收，不把单 collector 512 MiB 证据当两路资源证据。

## 停止、恢复与计费

```sh
python -m studio_platform.production_scaler --config /control-config/scaler.json --request-drain
python -m studio_platform.production_scaler --config /control-config/scaler.json --status
```

SIGTERM/SIGINT 与 request-drain 都只停止新准入和创建。控制器撤销本周期 cold approval，推进未执行等待的失败/释放，其他未提交 queued/claimed 等走已有安全取消逻辑；已 POST、未知结果或 recovery_hold 不伪终结、不释放预算。现有子进程继续 reconcile/collect，不能因 grace timeout 关闭 SSH 或强杀线程。

每次新提交再次检查 scope、政策、时长和实际租期。截止门槛统一使用实际 provider 安全截止和配置 drain margin；idle 子进程退出后以新鲜账本与上游空队列退休，再销毁。只有对应 provider removed 事实可以认定实例停止，空列表/404/DELETE 接受不够。

`--status` 是新的只读 PG 查询，无 provider、SSH、登记或 DDL。返回 `all_destroyed/ledger_safe/active_job_ids/active_jobs_truncated/billing_pending` 等；当前 job 已终结但其 attempt 仍未解决也会阻止安全完成。`controller_exit_required=true` 要求 host helper **另核实原控制容器自然退出码 0**，不允许只看到 status 就 kill。状态不可用、未知、过期均保留 active barrier。`billing_pending>0` 表示发票未定，保留预留，不等于零费用。

控制器是持续进程，但不保证任意崩溃后自动恢复已存在 fleet：检测到持久 `fleet_starting/fleet_started` 而没有本进程子进程句柄时会要求恢复核对，关闭新准入并保留账本，不盲启第二个 GPU 控制者。对未知上游/失联节点可一直等待对账；供应商 TTL 是独立最后边界，不是正常安全停机方式。修复或重启不能延长原周期，也不能删 receipt 重新租。新周期必须操作员明确配置新的独占池/周期并复用核清后的剩余总额度。

## 验证记录

`test_platform_production_scaler.py` 覆盖真实 PG/SQLite 持久控制合同、冷等同 job 激活、真实队列导致第二意图、稳定端口、scope 拒绝、预算不自抬、丢响应不重发、停止时取消未执行等待、资格失败收口、保留已启动 attempt/费用、凭据异常静态输出、完整资格 request、child factory、以及 deadline 前 idle child 退休。

本地 PostgreSQL fixture 只绑定 loopback，使用合成凭据、独立 schema 与 FakeProvider；结束核对准确容器 ID/label/tmpfs 后移除。它证明事务与控制合同，不证明供应商可用、真实模型质量、GPU 吞吐或公网扩容已经成功。

2026-10-04 操作状态：用户暂停新的生产扩容实测，先处理 onboarding。新周期 operator setup、预算配置、旧生产 worker 退休和新 GPU 创建均未由本实现执行；旧账本保持原状。后续恢复仍需独立核对已销毁实例的 pinned removed statement、旧控制容器自然退出证据、现有 PostgreSQL 未决任务，以及累计 US$50 内的剩余额度；本文件不构成新的租赁授权。
