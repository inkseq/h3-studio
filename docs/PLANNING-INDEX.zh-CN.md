# Sixnine 规划、当前契约与证据索引

维护日期：2026-10-08（Australia/Sydney）。这是阅读导航，不是部署回执或任务状态库。新会话先读适用 AGENTS → PROJECT-PLAN.md → DECISIONS.md → WORKFLOW.md → 当前 Issue。本页补充本地详细研究；不默认把所有历史报告当作当前要求。先核对远端 main、活动分支文档和最新认领，保留原工作区，不因文档落后而切换或重置它。

## 先分清文档各管什么

1. **为何这样选**：[DECISIONS.md](DECISIONS.md) 以稳定 ID 保存长期选择、来源、原因与重新审视条件；原始决策日期与补录日期分开。决策已接受不表示相关实现或上线已验收。
2. **要做什么、不能破坏什么**：[PROJECT-PLAN.md](PROJECT-PLAN.md) 是现行架构与工作包入口；[GENERATION-CONTRACT.md](GENERATION-CONTRACT.md) 及各专项合同定义行为约束。[中文详细设计](UNIFIED-BACKEND-API-PLAN.zh-CN.md) 和[复用研究](REUSE-AND-MIGRATION-DECISION.zh-CN.md) 是补充背景，不能用旧段落覆盖现行英文合同。
3. **现在做到哪里**：[CURRENT-BASELINE.md](CURRENT-BASELINE.md) 是带日期的源码/生产观察；当前认领、状态和验收查看板与 Issue/PR，发布查确切回执。[早期基础结果](GENERATION-FOUNDATION-RESULT.md) 与 [PR #17](https://github.com/apedintensor/h3-studio/pull/17) 是各自版本的证据，不是本轮进度。[合同示例 JSON](../UNIFIED-API-CONTRACT-EXAMPLES.draft.json) 仍是讨论草稿，不能当可执行 API。

重要长期决策在同一 PR 更新决策记录和相关规划/合同正文，消除冲突表述；不靠追加“最新补充”覆盖旧要求。普通 UI 细节留在规格或 Issue，无需逐个建决策。旧回执保留日期和版本，不能当成续期授权。

下表 Quick Chat 链接固定在保全版本，其合并与验收状态以当前 Issue/PR 和基线为准；跨仓库产品文档当前在公开 `inkseq/inkseq-design`；带日期的旧仓库链接保留为历史来源，当前任务与看板路由查 `workflow/project.json`。本地相对路径用于这个工作区，不能假定单独克隆后端就有这些文件。

## 按职责读取

| 问题 | 入口 | 权威范围和限制 |
|---|---|---|
| 已确认快速创作应该长什么样 | `../video-studio-design/quick-chat-mock/`，AGENTS 的 UI 还原规则 | 用户认可的视觉/交互基准；假 Key/假执行不是生产要求 |
| Quick Chat 保全版本的接口与实现 | [集成契约](https://github.com/apedintensor/h3-studio/blob/fd136fbd5a9b9681c17af12131ed22997b8113e7/QUICK-CHAT-INTEGRATION.zh-CN.md)、[后端实现记录](https://github.com/apedintensor/h3-studio/blob/fd136fbd5a9b9681c17af12131ed22997b8113e7/QUICK-CHAT-BACKEND-IMPLEMENTATION.zh-CN.md)、[UI 修正记录](https://github.com/apedintensor/h3-studio/blob/fd136fbd5a9b9681c17af12131ed22997b8113e7/QUICK-CHAT-UI-RESTORE.zh-CN.md) | 固定版本的本地实现/验收；当前合入、验收和发布状态另查 Issue/回执 |
| 产品数据和完整用户故事 | [Quick Chat 产品系统设计](https://github.com/apedintensor/sixnine-design/blob/2a7d0a5e16c679bbbf82a1f94f9f44fd970d59e0/QUICK-CHAT-PRODUCT-SYSTEM-DESIGN.zh-CN.md) | 产品基线；部分旧段落与最新直接出卡交互不一致，见复用决策第6节，不据旧句子倒退实现 |
| 当前平台实现和旧 API | [架构基线](ARCHITECTURE.zh-CN.md)、[API 用法](API-USAGE.zh-CN.md)、实际源码/OpenAPI | 带日期的实现说明；不把旧上线状态当今天状态，OpenAPI 中未类型化部分仍需补齐 |
| 任务/执行/容量合同 | [Queue](QUEUE-CONTRACT.md)、[Worker](WORKER-CONTRACT.md)、[Fleet](FLEET-CONTRACT.md)、[现行运行入口](RUNTIME-INVENTORY.md) | 保留已有语义；修改时核对代码与新工作包，不另造竞争的队列规则 |
| 双机目标 | [双 GPU 设计](SYSTEM-DESIGN-DUAL-GPU.zh-CN.md) | 目标2、最低可服务1，非已上线声明；预算/实例/故障隔离须验收 |
| 发布与兼容 | [开发发布约定](../DEVELOPMENT-RELEASE.zh-CN.md)、`deploy/platform/` | 前端/API/GPU按范围发布；现有兼容指纹不能随意缩小 |
| 历史生产策略记录 | `.platform-demand-live/NEW-START-STRATEGY-RESULT.zh-CN.md` 及其回执 | 2026-10-05历史证据；窗口到当晚23:45，不证明今天在线或构成续期授权 |
| 早期走查和研究 | [START-HERE](START-HERE.zh-CN.md)、[早期可靠性审查](GENERATION-RELIABILITY-ARCHITECTURE.zh-CN.md) 与历史报告 | 历史背景/证据；新的规划由总规划维护 |

## 唯一源码与数据入口

2026-10-06 已确认选型：用户确认已获得 WanGP 授权，采用上游 WanGP headless runtime，通过薄适配器接入。固定研究版本为 `deepbeepmeep/Wan2GP@0e58385fbde7ff102d276e4a9e490845de76b4ea`；源码 SHA 不替代依赖锁、模型组件 revision 或硬件验收。账户、持久任务、素材、预算和 GPU 控制器继续由 Sixnine 负责；Comfy 保留旧任务恢复和回滚基线，不另开新增功能路线。见 `PROJECT-PLAN.md` 第7节与 GitHub #4。

`本地 .architecture-research/WAN2GP-REVIEW-20261006.md`（受保护研究记录，不随远端源码发布） 保留选型前后的证据；其早期许可缺口已由上述用户确认解决。SGLang/vLLM-Omni/Diffusers 的比较仍可查阅，但只是历史备选研究，不再构成 SGLang 优先实施指令。选定 WanGP 不代表已经安装、接通真实模型或切换生产。

- 后端继续 `platform_app.py` / `studio_platform/`；新部署主线 `deploy/platform/`。
- 前端编辑源：`C:\Users\danmo\Desktop\inference\video-studio-design\studio-app`；`yingxu/` 是生成的发布快照。
- 旧 `server.py/web/` 冻结新增业务的建议已登记，尚未删除或正式退役；部分共享编译器/实验室/测试仍依赖其中内容。
- 不复制数据库、素材、Key 或租赁状态来搭建第二套生产；本地测试使用明确隔离的假执行配置。

## 每个工作包最少记录

目标与不变规则；当前行为→目标行为；涉及模块/文件负责人；请求响应/状态/权限/费用合同；旧数据与客户端兼容；验收方法和环境；发布/回滚与旧路径退出条件。

实现遇到新发现，按 WORKFLOW 在原 Issue/PR 承接；影响长期选择时更新对应决策及合同。历史回执保持原状，新回执引用旧记录并注明版本与时间。已确认用户要求、目标提案、当前代码行为、本地测试、生产历史证据、当前在线核验是不同层次，不能互相代替。

2026-10-07 审计整改：中文规划随 A3 文档批次纳管；Quick Chat/Agent Connect 实现保存在独立 draft 分支，canonical 前端及认可 mock 保存在私有 `apedintensor/sixnine-design`。是否已合并以 GitHub 回执为准；代码保全不表示产品验收或发布。

## 项目管理与跨会话恢复（2026-10-06 已建立）

整体目标仍是同一套业务 API 支持网页与 Agent 完成 H3 创作。近期顺序为 A1→A2→B与D的有界实现→C恢复验收→B3公网真实生成验收；具体状态和依赖从看板读取。[D1 / #18](https://github.com/apedintensor/h3-studio/issues/18) 定义离线适配器与持久回执边界，其验收不能替代真实模型、网络服务或生产路由验收。

[D2 / #22](https://github.com/apedintensor/h3-studio/issues/22) 承接固定模型/依赖、参数映射、私有 runtime 引导/传输、attempt 引擎绑定、旧 Comfy 恢复及容量保护的接入验收；各项已实现或未完成情况查该 Issue 和 [WanGP 接入回执](WANGP-INTEGRATION-RESULT.md)，本索引不复制进度。通过所需恢复测试并具备当次部署/GPU授权，才进入 B3；不能直接把 `#18 → #16` 当完整接入步骤。首个真实验收收窄为单槽、单个已支持 Base recipe；双机和更广参数矩阵后置，不为完成全部长期重构而推迟独立可验收的闭环。

- [Sixnine Platform Delivery 私有看板](https://github.com/users/apedintensor/projects/2)，已关联 `apedintensor/h3-studio`。
- [英文项目规划](PROJECT-PLAN.md)、[决策记录](DECISIONS.md) 与 [英文工作流](../WORKFLOW.md) 是新克隆可用的起点；[中文阅读版](WORKFLOW.zh-CN.md) 提供对应说明。
- [路由元信息](../workflow/project.json) 记录真实 Project、字段与 A–H / 子任务 ID；它不是实时任务状态或授权账本。
- 建立时创建了3个里程碑、8个父级工作包及首批子任务；后续任务数量不在本页维护。Roadmap / Delivery board / Ready to pick up / Blocked work视图用于查询当前工作。
- 建立时为协作者 `yutingk0805` 配置了看板写权限；当前权限需另核实。GitHub 新增内容统一用英文。
- 工作流发布提交：`99755e60d0f1ce1e3fe09295a3de3dafbab4360b`。只提交 9 个工作流文件或入口增量；没有修改/发布应用运行代码，也没有启动 GPU。
- 建立时的下一任务为 [A1 / #10](https://github.com/apedintensor/h3-studio/issues/10)。后续以 Ready 视图、依赖和最新认领为准，不能永远机械选择 #10。

开工顺序：适用 AGENTS → 英文总规划 → DECISIONS → WORKFLOW → 当前 Issue、父工作包和相关 spec。认领时记录会话、分支/工作目录、版本、范围和依赖。结束时在原 Issue 写完成/未完成、证据、上线状态及下一步。所有代码与操作仍遵循用户最新授权；文档、Issue 或看板状态不能续租、重置费用或覆盖其他会话工作。

完全不带项目上下文的新会话不会自动知道看板。打开本项目，或给它仓库/Issue/Project 链接；WORKFLOW.md 提供可复制的启动说明。跨会话承接依赖这些持久入口，不承诺聊天记忆能替代它们。

轻量 SDD 已落在“目标/契约 → 工作项 → 验收 → 交接/发布证据”上。本次没有安装 BMAD/Spec Kit；后续若使用它们，只辅助维护这套规格，不另建竞争的任务状态。
