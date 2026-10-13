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
3. push/PR 只检查，不发布。`test` 为稳定汇总检查。完整本地 Git diff 决定范围；新文件、删除、未知路径或缺少基线走全检查。不是按任意 `.md` 后缀跳过检查，公开 Skill 是运行资源。
4. 本地准备发布：提交好批次后运行 `python tools/prepare_release.py --kind frontend` 或 `--kind platform`，可加 `--commit` 指定完整 HEAD。入口核对 main、目标仓库、源码干净和远端 SHA，push 一次，只取消同仓库/同 SHA/同 workflow 的未完成普通 push 检查，再 dispatch 一次准备任务。不会同步、commit、安装包、批准或部署。显式 DOCUMENTS 白名单内未提交文档可保留，例如用户的 `SCALING.zh-CN.md`；其他受审阅源码/配置和未跟踪文件必须先处理，ignored 私有文件不扫描。也可在 Actions 手动选择 `deploy=true` 与 `release_kind`，但不要与本地入口重复触发。
5. 通过独立操作身份核对测试版本与 manifest 摘要，写入 host 对应批准目录，再 dispatch `approved_frontend_commit` 或 `approved_commit`。部署身份不能自批准或安装 root helper。

平台准备会完整验收确切版本；前端准备只验收前端及发布边界，不运行全套 PostgreSQL 或重建 Python 镜像。不要同时为同一批次重复触发准备任务；已通过且未受修改影响的检查不反复执行。用户追加发布路径修改时，旧产物不得冒充最终版本；完成新增修改、针对发布边界验证与独立审查后，再准备最终确切版本。pip/npm 和 BuildKit 缓存用于加速依赖与镜像层，缓存命中不代替验证。

本地入口在 ignored `.release-prepares/提交号-类型/receipt.json` 中先记意图、再执行变更。取消或 dispatch 超时/回应不明会停止；同一意图再次运行会拒绝，不自动重发。通过 `gh run list --repo inkseq/h3-studio --workflow ci.yml --commit 完整SHA` 及收据核对，保留原意图，不靠删除收据重试。返回的 run ID 只表示观察到确切 SHA 的新准备任务，不表示检查、发布或部署完成。GitHub dispatch 没有直接返回 run ID；出现零个或多个候选时需人工核对。如果普通 push 检查已经完成，当前入口不复用其测试结果；为避免重复，应让本地入口负责该批次第一次 push。PR、其他 SHA、已完成检查及发布/部署任务不会被取消。

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

For a backend-only batch that must reconnect the same native runs, `tools/prepare_release.py --kind platform` accepts an explicit prior `--retain-native-sources-run`, full `--retain-native-sources-commit`, and `--retain-native-sources-index-sha256` together. These inputs are valid only for platform preparation. The workflow verifies the successful main preparation run and exact unexpired artifact. Packaging reads the old source archive as data and validates every selection, index, engine manifest and runtime document. Retained selections must remain an ordered prefix; every old four-file byte and entry hash stays unchanged. There is no latest-run or model fallback.

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
