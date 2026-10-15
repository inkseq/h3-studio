# 限时生产 GPU 验收 worker

这是**复用已经验收、明确交接的一台现有 Lium GPU**的生产路径。它不租机、不接 Lium API、不下载模型，也不持 Lium Key。原本地 controller 继续唯一持有供应商 TTL/销毁责任。`LIUM-PRODUCTION-INTEGRATION.zh-CN.md` 中完整生产租赁/SM/controller 方案留作后续设计；第一阶段无需激活它。

本次实现 `studio_platform/production_worker.py`，没有修改默认 cpu-only Compose/check_config。部署方另以受审查的有限 overlay 接同一个生产 PG 与 `/data`，生成结束后恢复原 API 准入。

## 命令

```text
python -m studio_platform.production_worker --config /worker-config/worker.json
python -m studio_platform.production_worker --config /worker-config/worker.json --enabled
python -m studio_platform.production_worker --config /worker-config/worker.json --request-drain
python -m studio_platform.production_worker --config /worker-config/worker.json --status
```

第一条只读/验证非秘密配置，不读运行环境/DB/SSH。启动必须 CLI `--enabled` 和配置 `enabled=true` 同时满足。`--request-drain` 写持久 flag 并对 exact PG worker 调用锁内 drain；`--status` 为独立只读命令，可通过 `compose run --rm --no-deps ...` 在原 worker 容器退出后执行。不以 container Running 或持久 JSON 自称安全。

## 配置字段（只允许这些；没有凭据值）

| 字段 | 类型/来源 |
|---|---|
| version / enabled | 整数1 / 显式布尔 |
| handoff_id | 本次唯一稳定 ID，1..80字母数字横线下划线 |
| work_dir | 绝对路径，建议 `/worker`，持久绑定目录 |
| ssh_key_file | CPU专用密钥绝对路径 `/worker-identity/key`；只读、仅UID10001可读，不把私钥内容放配置 |
| known_hosts_file | 绝对路径 `/worker-identity/known_hosts`；由已验收交接报告的真实SSH host public key制成。非22端口使用OpenSSH对应 `[host]:port` 格式 |
| host / ssh_port / local_port | 交接报告的精确公网IP、SSH端口、CPU容器内独占loopback端口（例如18881） |
| boot_identity | 原 GPU `/workspace/h3-studio/sixnine-bootstrap-identity.json` 的完整非秘密对象：intent_id、instance_id、configuration_id、sources（bootstrap_cloud.py/model_manifest.json各SHA256） |
| gpu_uuid | 已验收的唯一物理GPU UUID |
| worker_id / pool | 本次生产独占worker/pool ID；不得和本地验收混用 |
| hard_deadline | 交接报告核验过的**保守safe_deadline UTC epoch**；不能取比供应商实际TTL更晚的原globaldeadline |
| drain_margin_s | 停止新claim提前量，整数120..3600秒 |
| collection_margin_s | 提交前给下载核验预留的秒数，整数30..900，默认120 |
| qualification_evidence_id | 已有真实运行证据ID，必须和execution policy精确相符 |
| owner / tenant | 固定 `superdan` / `sixnine`（可省略使用默认） |

配置必须绝对路径且不可由组/其他用户写。remote handoff marker 和持久 `acceptance-state.json` 都先于 Fleet 启动；任一已有状态要求明确人工恢复，不自动删除、不自动再启动。同一进程持有本地唯一控制锁。SSH 未知/变化host key直接拒绝，没有AutoAdd/ignore选项。

字段模板在 `deploy/platform/production-worker.example.json`。其中IP、boot身份、GPU、证据、期限都是占位符，必须由已核验handoff元信息替换；模板 `enabled=false`，不能直接用于连接。严禁仅把它改成true尝试猜测机器。

此文记录历史 Comfy worker 契约；当前域名为 `https://inkseq.com`，当前存储与调度以已批准发布和配置为准。原版本运行环境从 `Settings.from_environment` 读取，与API共用：`SIXNINE_DATA=/data`、同生产DSN_FILE、PUBLIC_ORIGIN、AUTH_MODE=password、GENERATION_ENABLED=1、EXECUTION_BACKEND=comfy-worker、STORAGE_PROVIDER=local、EXECUTION_POLICY_FILE绝对只读路径。PG必须是postgresql+psycopg；不回退SQLite。policy需same pool/configuration/model/recipe/evidence，资格/费用expiry不得晚于safe_deadline。root应先明确设置有限capacity和superdan预算；worker不会自行扩大额度。

## 真正交接前置条件

原 controller 必须先 drain 原 Fleet，确认其账本无current/unresolved任务与上游queue空，并持久禁用该 intent 再次Fleet启动；保留其TTL/销毁观察。仅出现空queue不意味着可以双控制。本模块启动检查原boot identity、模型/Comfy revision、GPU UUID/显存与5份已校验权重报告，再检查实时空queue，最后建立不可复用的 remote `sixnine-production-worker.json`。

CPU容器需要 paramiko、已有fleet依赖、PG网络和SSH外连；无provider Key、无Docker socket。API和worker共享相同本地对象存储。loopback tunnel与Fleet子进程在同一容器，GPU从不持DB/store凭据。

父/子进程退出处理均只禁止**新生成**；已有提交继续reconcile/collection。新提交还核验：本次owner/tenant、现有ExecutionPolicies、worker slot/drain与 `now + job.expected_runtime_s + collection_margin_s < safe_deadline`。只有5秒已验收50步才能在policy开放相应规格；这不是通过调整worker参数自行扩大资格。

## 安全恢复契约

`/worker/worker-status.json` 仅含身份、时间、状态；不要将它单独作为停止依据。`--status` 当前只在以下条件同时成立时输出 `drained=true`：

- 本地receipt identity与给定配置精确相符，持久drain flag存在；
- 同一PG exact worker/pool/pod/config/GPU匹配，state=draining、drain_requested=1、lease尚有效、current_job_id为空；
- 该worker历史attempt没有仍未终结的job；
- 本次只读SSH现场检查remote handoff identity吻合，Comfy running/pending均为空；
- SSH检查后再次读取PG仍满足。

字段：`version`、`handoff_id`、`hard_deadline`、`worker_ids`、`active_job_ids`、`drain_requested`、`worker_drain_requested`、`worker_state`、`ledger_safe`、`upstream_idle_confirmed`、`drained`、`observed_at`、`phase`。未知、配置冲突、过期lease、SSH失联、pending/running/未终结任务均不得stop/kill。unknown不会变成免费/已销毁。

恢复流程：API先恢复禁用新生成→request-drain→独立status有限轮询→只有drained才允许自然退出/发送TERM。不要自动SIGKILL。原Lium controller收到完成信号后负责供应商销毁和statement核验。CPU恢复成功并不表示GPU已停止计费。

当前离线验证5条新测试涵盖双开关、identity/忙队列、handoff不重复、实际Repository worker表drain（本轮本地SQLite测试）与注入的上游确认、deadline后继续已提交任务、专用子进程入口。没有在这份实现任务中访问云或生成视频。512MiB worker容器是否足够仍需真实峰值/OOM验证，不据单元测试承诺。
