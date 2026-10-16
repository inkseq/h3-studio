# 开发、发布与 GPU 运行分开

本项目处于初期迭代：先完成一批相关修改，再统一验收与发布。开发中只运行定位问题和关键边界所需检查；不为每个按钮单独推 main、跑全套 CI 或切换生产版本。源码事实与线上状态分开：上线回执在本地 `.platform-demand-live/`，本文描述代码契约。

## 三条独立路径

| 改动 | 检查与发布 | 对正在运行的服务 |
| --- | --- | --- |
| 文案、布局、前端交互 | Yingxu 行为测试、构建、快照及前端发布边界检查；独立静态包 | 原子切换 HTML 指针；API、数据库、GPU 控制器无需重启 |
| 已确认与 worker 兼容的 API 展示/发现代码 | Python/API/数据库与镜像检查；批准平台包 | 只更新 app；控制器继续使用启动时固定镜像 |
| 任务协议、准入、账户/数据结构、计费、调度或依赖 | 批次关键回归及完整发布门槛 | 不兼容时拒绝热更；等待任务和账单安全收尾后迁移 |

`api.py`、schema、存储、编译和依赖均保守计入 worker 指纹。当前仅排除 `frontend.py`、`agent_discovery.py` 和分发 Skill 文件。开发与公开试用阶段，经完整源码和配置独立审查的 app 更新可以保持现有控制器、队列和租赁；不因新增可选起名凭据与 app 出网就要求取消用户任务。现有精确版本批准路径允许明确记录的 app 配置差异：只新增 Google 起名的环境路径、私有秘密挂载和 app 的 edge 网络；数据库、Caddy、初始化、其他挂载、控制器配置与账本不变。其他配置或协议变化仍需要对应迁移方案。单 app 替换可能有短暂请求重试。

## 日常批次

1. 编辑 canonical 前端 `../video-studio-design/studio-app`；通过 `python tools/sync_yingxu_source.py --write` 同步发布快照。不要直接维护两份前端。
2. 开发中按需预览、做语法及受影响关键路径检查；批次完成后再提交。
3. PR events check only. A relevant merge to main publishes immutable tested artifacts and automatically deploys staging after the stable `test` aggregate passes. Documentation-only changes do not rebuild or deploy. Platform candidates include their matching frontend/platform/migration manifests; frontend-only changes retain the independent static path.
4. `python tools/prepare_release.py --kind frontend|platform` verifies/pushes once and observes that main run. It no longer cancels push CI or dispatches duplicate preparation. Its retained `.release-prepares/` intent forbids uncertain replay. Reviewed repository variables select retained native sources; preparation arguments cannot trigger a second build.
5. After live staging acceptance, normal production rollout uses only `promote_commit=FULL_SHA`. It downloads the accepted existing objects without Docker/npm rebuild. The deprecated `approved_commit` and `approved_frontend_commit` workflow actions refuse; original fixed deployers remain available for separately reviewed rollback operations.

## Isolated staging and exact-artifact promotion (#195)

Source/configuration is not a deployment receipt. Staging is `https://staging.inkseq.com`; production is `https://inkseq.com`. Once staging is accepted, all development/GPU testing uses staging and supervan manages her own production capacity.

- Separate hosts retain the existing `/srv/sixnine` layout, release.lock, current/previous, retention, worker compatibility and queue-handover checks. New hosts use a root/root0600 `deployment-target.json` containing exactly version1, target staging|production and the exact instance_id. Protected site.env's `INKSEQ_DEPLOYMENT_TARGET` must match. Missing stage identity and incorrect origins/profiles never fall back to production. Explicit retained production compatibility is not a stage bootstrap path.
- The same platform bundle contains `Caddyfile`, `Caddyfile.staging` and `deployment_target.py`, all manifest-hashed. The closed target selects the exact mount without modifying artifact bytes. Staging has no production hostnames, aliases or migration redirect. PUBLIC_ORIGIN and storage identity agree across API/controller/dispatch.
- Stage IAM reads only exact `/sixnine/staging/*` ARNs; databases, Hatchet token/volumes, dstack DB/project/SSH, budgets and account secrets are independent. CR-20261016-18 explicitly authorizes the existing Vast profile `vastai--sixnine-dstack-20261011` at `https://console.vast.ai/api` for staging too. Its provider account/key can reach production allocations: project `sixnine-staging`, stage tags, immutable ownership and exact-ID stop guards separate normal operations, but are not provider credential isolation. R2 still uses only `cloudflare-r2--sixnine-staging-20261015` and private `sixnine-staging-media`. RunPod, production Grafana credentials and title integration remain disabled.
- Keep the existing secret initializer and its fresh<=300s absence checks, persisted idempotency token and unknown-intent recovery for genuinely fresh infrastructure only. Existing staging uses normal hydration and an exact reviewed Secrets Manager update for the authorized shared Vast profile; never reinitialize accounts/DB/queue or relabel the old Team credential. No USD5 transfer, Team funding, new key or funding receipt is required or authorized. Persist the existing Hatchet token before removing temporary stage-only PutSecretValue; recurring hydration is Get/Describe only. Test spend stays within the ordinary authorized daily ceiling, stage budget, max_price and idle stop.
- The test-gated main publication role conditionally writes `staging-candidates/COMMIT.json`. Its closed schema binds the run, commit and exact platform/migration/frontend manifest hashes. Bucket policy permits only that main publication identity to write the prefix; the staging deployment identity can invoke only the fixed stage document+host, not write candidates/helpers or access production. Root-installed `environment_release.py` approves only this exact tuple on the sealed stage host, then reuses existing deployers.
- New documents `Inkseq-DeployStagingRelease` / deployStagingCommit and `Inkseq-PromoteAcceptedRelease` / promoteAcceptedCommit pin version1, accept only Commit, and call `/opt/inkseq-release-staging-v1/environment_release.py` with their fixed environment. Existing production documents retain version2. Workflow concurrency differs between stage and production; each host still serializes platform/static changes. Unknown SSM responses resume the original command ID and target, never resubmit.
- Automatic platform preparation requires all three variables `SIXNINE_NATIVE_SOURCE_RUN`, `SIXNINE_NATIVE_SOURCE_COMMIT`, `SIXNINE_NATIVE_SOURCE_INDEX_SHA256`. The reviewed retained tuple is run38716781153 / acc9402dec03d59e15435e2207a8b640df9f6377 / fcac7928646662cb07298661e620e32765e49ecd37a0afcc287eb706c853acac. This is native-source provenance, not the previous CPU release.

A protected `promotion-candidates/COMMIT.json` records version1, state accepted, commit, canonical candidate_sha256, staging_instance_id, checks (browser_agent/isolation/migration_rehearsal all passed), and this repository's issue evidence link. The independent production root identity verifies actual results and writes that record's SHA to `approved-promotions/COMMIT.sha256`; deployment cannot write it. Promotion rechecks both approvals and reuses the same images, archives, native index and frontend through existing deployers. It never copies stage DB, secrets, budgets or tasks. Frontend-only acceptance may reuse the unchanged backend's accepted migration evidence without another database/model run.

`offline_rehearsal.py FULL_SHA` rehearses schema initialization on STAGING only. The protected original `rehearsal/incoming/production.dump` is retained and hash-checked before/after. Disposable volumes use `--network none`, no published ports, no provider/R2/production-secret mounts, and the exact approved image's `manage init-db`. Only names created by this operation and retained in its intent can be removed. The copy is NEVER public and never starts API/controller/dispatch: production sessions, PATs, jobs, rentals and budgets cannot become a second authority. Live staging uses a fresh independent database and synthetic data. Failure retains the exact intent; no automatic uncertain replay or cleanup of other resources. Do not expose an unsanitized clone.

Initial operations still require independent installation of the reviewed root helper closure, exact stage instance/IAM/SSM binding, fresh DB/queue, DNS/TLS, scoped R2 credential/CORS, and real browser/Agent/GPU data/queue/budget isolation acceptance. Do not claim Vast credential isolation under the shared-key rule. Report resource IDs, monthly cost, actual spend, live usability and production promotion separately from merge/test status.

## 前端版本与回滚

- `tools/build_frontend_release.py COMMIT DIST DESTINATION` 生成 `frontend.tar.gz` 和 `frontend-manifest.json`；`check_frontend_bundle.py` 离线验证；`publish_frontend_release.py` 用 GitHub OIDC 条件写入已有 release bucket 的 `releases/COMMIT/frontend/`。
- host `deploy_frontend.py` 只接受 40 位提交号，要求 `/srv/sixnine/approved-frontends/COMMIT.sha256` 独立批准。固定 SSM Document 为 `Sixnine-DeployApprovedFrontend`，不能执行任意命令。
- host 校验当前 API 独立批准、实际镜像、`sixnine-web-v1` 和完整 `api_compatibility` 指纹。前端依赖尚未上线的后端代码时拒绝发布，不悄悄连接其他版本/API。
- app 只读挂载 `/srv/sixnine/frontend` 到 `/frontend`。`current.json` 只选一个不可变 release index；共享 `assets` 只增不覆盖，已打开页面可继续加载旧文件。HTML 不缓存，带哈希静态文件长期缓存。
- 成功切换后探测站内 HTML 版本；失败恢复旧指针。通过相同部署入口选回已批准且与当前 API 指纹相符的旧前端即可回滚。没有指针时使用镜像自带前端，损坏指针不会静默回退。
- 完整平台更新前归档外置指针，使用新镜像配套 UI；平台失败回滚使用旧镜像配套 UI。外置资源与历史指针保留，后续可重新选择兼容前端。

## GPU 生命周期

### Native source retention during a CPU-only repair

Existing dstack bootstrap receipts bind the exact four native source hashes. A fresh tar/gzip build can change those hashes despite identical source members. Do not delete or rewrite a bootstrap receipt, restart GPUs, or approve a replacement source set merely to deploy CPU code.

For automatic main platform staging, configure the complete reviewed repository tuple `SIXNINE_NATIVE_SOURCE_RUN`, `SIXNINE_NATIVE_SOURCE_COMMIT`, and `SIXNINE_NATIVE_SOURCE_INDEX_SHA256`. `tools/prepare_release.py` observes that single main run and rejects explicit retention arguments rather than dispatching a duplicate build. The workflow verifies the successful prior main preparation run and exact unexpired artifact. Packaging reads the old source archive as data and validates every selection, index, engine manifest and runtime document. Retained selections must remain an ordered prefix; every old four-file byte and entry hash stays unchanged. There is no latest-run or model fallback. The lower-level explicit packaging/retention API remains available for separately reviewed preparation.

When an immutable native repair is appended (for example cgroup-compatible Pruned v4 FL/REF), the explicit `--retain-native-sources-from-git` packaging mode checks retained package members and bootstrap against exact pinned public Git blobs. It reads the historical `PRIVATE_FILES` literal with AST only and never imports, executes or checks out historical source. New selections alone use the current small source bundle and bootstrap. The migration manifest records `native_source_commits`, mapping each package SHA to its exact source commit, so later CPU releases can independently validate a mixed set of native generations. Older manifests without this map use their explicit preparation commit. Missing commits, untrusted archive members, modified old profiles/runtime documents, changed retained bytes or incomplete origin maps reject reuse. The strict current-source local retention API remains available. Current CPU images and the new migration manifest remain bound to the new commit; this path does not rebuild or replace the pinned native image, upstream revision or dependencies.

Independent artifact/host approval must also compare the retained four hashes and index against the two original live bootstrap identities. Retaining native bytes does not renew a policy, settle billing, authorize rentals or establish live readiness. After the protected CPU release, recheck the original instances, registered worker identities/heartbeats, exact FL/REF availability and preserved job/attempt/reservation fingerprints.

新 v2 marker 固定 execution commit、镜像身份、worker 兼容指纹、配置、预算周期和控制器身份。API 发布前验证 pin 与真实运行容器，仅同 worker 契约及相同 host bundle 配置允许兼容更新。更新只操作 `app --no-deps`，不重建 db/db-init/caddy/controller。

开发阶段若旧 CPU supervisor 已在内存中加载旧发布代码，先通过一次明确的 `--preserve-unsubmitted-queue` 交接升级该 supervisor，再发布新 app；不因此取消排队测试任务。仅当全部活跃任务都是未创建 attempt、未绑定 worker/lease 的 queued 任务、没有活跃推理或执行 worker 时允许。受保护交接回执记录队列的原身份与不可变请求/执行配置摘要，退役前、使用目标固定镜像核对时及重启前必须完全一致；其他租赁、未知创建记录、预算、期限仍按原规则保留。同版本交接只允许此显式命名模式：确认旧容器正常退出、释放本地所有权后，按其固定 ID 保留并改名归档，记录一次性意图，绝不删除或因结果不明重发。旧 supervisor 的空队列恢复门槛可能因此拒绝收尾；新 supervisor 仍须独立验证干净退出和完整队列证据。普通交接默认仍要求空队列，开启范围只能来自 prepare 的显式授权，successor 不能自行扩大。

控制器发出控制命令仍使用固定 execution release；开启/关闭网站准入及最终恢复 CPU 则在 release lock 内重新读取当前 approved app。这样控制器结束不会把网站切回其启动时的旧镜像。预算、排队任务、租赁账本及原截止时间不随网站发布重置。

旧 v1 运行中的 Python watcher 不会因磁盘文件更新而升级。首次接入需核对空池或等待安全收尾，再由新 helper 启动新 v2 周期。用户任务存在或账单不明时禁止使用“空池迁移”。保持最少 0 台 GPU，有确认任务才租赁，业务空闲 600 秒关机；本改造不改变原预算，也不进行生成验证。

## 实现与复现

关键模块：`tools/ci_changes.py`、`tools/release_contract.py`、`studio_platform/frontend.py`、`deploy/platform/frontend_bundle.py`、`frontend_release.py`、`deploy_frontend.py`、`release.py` 与 `gpu_scaler.py`。主机 helper 必须由独立操作身份安装，发布 bundle 不能替换 root 执行器。

无网络边界检查：`python -m unittest test_ci_changes test_release_contract test_frontend_release test_frontend_deploy test_platform_frontend test_platform_release test_platform_gpu_release test_platform_gpu_scaler test_platform_aws_deploy`。GPU 测试使用模拟依赖，不代表真实推理验证。

采用的官方依据：[GitHub workflow 路径过滤与触发规则](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)、[Docker GitHub Actions 构建缓存](https://docs.docker.com/build/cache/backends/gha/)、[Starlette 静态文件路径行为](https://www.starlette.io/staticfiles/)。
# Staging 2GiB + 2GiB swap：待实测的资源配置

这是明确选择的 staging 资源配置，尚未在小机器上验收。生产默认配置、3584MiB 物理内存门槛、既有服务上限及同一产物晋级规则不变。配置不能从请求参数或普通环境变量切换；仅现有 staging 主机的 root 保护 `deployment-target.json` 可增加 `resource_profile: staging-2g-swap2g`。保护的 migration paths 同时记录 `INKSEQ_RESOURCE_PROFILE`、固定 `INKSEQ_CPU_CGROUP_PARENT=inkseq-staging-workload.slice` 和实际观测的 `host_memory_mib`。

服务仍保留合计3296MiB的原上限，特别是 dstack768、dispatch128、controller640MiB。所有服务包括 db-init 必须进入同一父 slice，实际父组限制为1280MiB内存和2048MiB swap，合计3328MiB；物理内存最低1792MiB，至少给组外系统保留512MiB。父组比各子组的合计内存上限小，可能发生回收、交换或组内OOM；算术和源码检查不能证明真实任务可用，也不能将swap当成物理RAM。

未来操作按以下有限顺序进行；本批源码不会创建swap、安装服务、缩容或重启：

1. 先完成staging真实GPU验收，将包含#193两项修复的同一验收版本晋级生产，最后才缩容。保存原主机类型、当前/上一版、target/paths/site及boot文件的原始字节和哈希；确认无活跃GPU、任务或worker lease，保留历史和未知账单。
2. 只在现有加密EBS中分配 `/srv/sixnine/staging.swap`：预先空闲至少5GiB，创建恰好2GiB、root:root0600、单链接、完整分配的普通文件，再格式化并启用。分配后仍须保留原发布要求的至少3GiB空闲；不得清除当前/上一版、数据库、素材或回滚文件来凑空间。
3. 通过既有受保护安装流程安装 `deploy/platform/ec2/` 中三个新资源文件，保留正常 `inkseq-staging-secrets.service` 和原drop-in。swap unit、slice及正常密钥hydrator必须在Docker前启动；不运行任何 `--initialize`。预检逐项核对实际swap、文件权限/占用块、unit字节、cgroup v2/systemd驱动、实际MemoryMax/MemorySwapMax及依赖，不能仅提交一份自述成功的收据。
4. 在仍为medium的主机上启用该保护配置，用正常发布入口重建同一候选的CPU容器。先测冷启动，再测单GPU的准备、连续任务和视频/独立音频收集；记录父/子组内存、swap、OOM、重启、耗时及下载结果。运行时核对全部常驻服务的真实进程属于父组；stage operator政策必须最多1台/1GPU。预检通过、假数据测试或medium试验均不能代替小主机验收。
5. 上述实测通过后，才对同一实例停机、改small并启动；读取新公网IP并仅更新staging DNS。正常boot hydrator从现有独立Secrets Manager读取并重建tmpfs，保留DB/Hatchet卷，不依赖首次启动的user-data。重新测真实物理内存、服务/TLS/登录和一条完整GPU路径；所有开发及GPU测试仍限staging。
6. 任一阶段失败先保留证据和任务，不重复付费生成。若已缩容，先把同一实例恢复medium，再恢复原target/paths和同一受保护的当前/上一版。恢复原profile会重新要求3584MiB；禁止在small上删profile来绕过资源验收。恢复容器到默认父组、确认无成员及足够可用内存后才能安全停用swap。原b590仅按其精确helper/checker哈希使用已安装检查器回滚，不能运行产物中的未审查代码。

只读检查不会分配swap或修改服务。主机资源准备、medium单GPU资格、小主机boot/单GPU资格及最终缩容仍是未完成验收项。官方语义参考：[Docker内存与swap](https://docs.docker.com/engine/containers/resource_constraints/)、[Compose cgroup_parent](https://docs.docker.com/reference/compose-file/services/#cgroup_parent)。
