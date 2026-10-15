# Sixnine 工作方式

当前规则集中在 [AGENTS.md](AGENTS.md)（唯一必读入口）和 [WORKFLOW.md](WORKFLOW.md)（按需查阅），不再维护重复流程。

- 按 Low / Medium / High 风险选择验证与审查；低风险不需要独立审查、GPU 或私有证据文件。
- 同批改完再跑受影响检查；没有明确方向超过两小时，五行说明并询问。
- 面向用户的交接使用中文，约十行以内；详细证据和审查放 PR。
- 测试 GPU 每个 Australia/Sydney 自然日累计不超过 US$20，保留价格上限和空闲停止，测试结束关机；#195 后只在 staging 测。supervan 自开的生产机器和任务不动。
- 中途决策记在 [owner-changes.md](workflow/owner-changes.md)，最新 CR 优先，冲突旧规则直接替换。
- 每次交接报告 issue 创建到生产可用的耗时；未上线就注明待完成。

旧版规则只作为历史保存在 `docs/archive/workflow-20261008/`。
