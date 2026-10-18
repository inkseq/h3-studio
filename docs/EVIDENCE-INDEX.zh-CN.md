# 本轮可核对的验收索引

截至2026-10-04 02:18 UTC。最终运行与前端源码是 `b588b60d00091d677b5936b39c01c0b27ba6b6ba`；最后60分钟固定版本观察已经通过；四组固定观察各自校验并确认相关进程退出。下列点号目录是本机验收产物，不随源码提交；存在于同盘不等于已有离机备份。

## 先看哪份

- 用户：[五分钟走查](START-HERE.zh-CN.md)、[交付报告](DELIVERY-REPORT.zh-CN.md)。
- 实现：[架构](ARCHITECTURE.zh-CN.md)、[27轮迭代](ITERATIONS.zh-CN.md)、[API契约](API-USAGE.zh-CN.md)。
- 上线：[生产就绪复核](../deploy/platform/READINESS-REVIEW.zh-CN.md)、[秘密供给](../deploy/platform/RUNTIME-SECRETS.zh-CN.md)。

## 最终源码与发布镜像

| 证明什么 | 本机相对路径/外部运行 | 范围 |
|---|---|---|
| Windows全套 | `.platform-preview-v2/regression-final-aging.log` | 740项收集，728通过、12跳过；Linux资源限额另测 |
| 本机隔离PG与媒体组合 | `.platform-preview-v2/regression-pg-final-aging.log` | 458项，456通过、2跳过；媒体测试用SQLite |
| 固定提交GitHub CI | [运行37167471819](https://github.com/apedintensor/h3-studio/actions/runs/37167471819)；`.platform-preview-v2/ci-b588b60-receipt.json` | Linux740/7skip，PG组合458/2skip，前端214、96文件快照；部署跳过 |
| 不可变镜像与六文件包 | `.platform-final-release-20261004/b588b60d00091d677b5936b39c01c0b27ba6b6ba/FINAL-RECEIPT.json`；同级REPORT | Linux244/244，无核心overlay；1GiB HTTP粗剪字幕音频/逐帧下载链路、独立HTTPS/PG双账户/重启通过；不是公网或GPU |
| 最终固定60分钟CPU模拟 | `.platform-fixed-delivery-soak-20261004/manifest.json`；`source/.platform-soak-delivery-60m/status.json` | 01:16:33至02:16:37 UTC，24/24、无失败、0次计划重启，status=passed、completed_at与进程退出均已核对 |
| 本轮临时PG容器清理 | `.platform-preview-v2/test-pg-cleanup.json` | 01:25核对精确ID/label/tmpfs无持久挂载后移除，确认不存在；预览SQLite不受影响 |

最终image ID为 `sha256:716ac5aa2bea05d13e40ae65e0d862a0b1a7454c0bbdd8bc0672f0eb3409e011`；manifest SHA256为 `59272362ee09171964df66a0c9c2926d30926f70cba016c75771f9c25784b628`。包恰好六个文件，报告/日志/用户资产不进入bundle。镜像只含代码、依赖与许可字体；目标主机独立root发布控制器需另行安装批准。

## 真实用户流程与复现

| 证明什么 | 路径 | 不证明什么 |
|---|---|---|
| 工作台与成片入口 | `.platform-preview-v2/studio-guide-final.jpg` | 截图不代替媒体完整解码 |
| 实际浏览器下载 | `.platform-preview-v2/browser-download-verification.json` | Chrome工具取得并校验实际MP4文件，不是操作系统另存为弹窗 |
| 剪辑入出点与声音位置 | `.platform-preview-v2/trim-browser-verification.json` | 262帧CPU成片，不是新H3推理 |
| 最终控制/模型/网页契约审查 | `.platform-review-final-controls/REPORT.zh-CN.md`、`control_proof.py`、`result.json` | 纯函数与源码证明，不是所有GPU/供应商控制组合已验收 |
| 4.9万任务容量与公平性 | `.platform-review-queue-capacity-20261004/REPORT.zh-CN.md`、`results.json`、`fairness-after.json` | 大积压属于027修复前；修复后22-job复现另记，不是GPU吞吐或百万队列证明 |
| 机器身份与私有云下载 | `.platform-review-machine-download/check.py` | 合成身份与fake云传输，不是真实云账户，也不是所有SDK的重定向行为 |
| 满缓存重试问题 | `.platform-review-render-cache-20261004/REVIEW.zh-CN.md` | 原45d3的失败复现，后续b879修复；最终b588回归覆盖 |
| 下载并发/慢连接 | `.platform-network-capacity-20261004/REPORT.zh-CN.md` | 历史25525ff本地45秒数据，不是最终版公网SLA |

## 历史版本必须分开读

- **六小时滚动运行**：`.platform-soak-overnight-20261004/status.json`，18:07:21至00:07:29 UTC，36/36、5次计划重启、无失败。期间代码更新，不能叫最终版本六小时。
- **45d3固定两小时**：`.platform-fixed-soak-20261004/manifest.json`及`source/.platform-soak-frozen-2h/status.json`，00:07:01至02:07:03 UTC，48/48、1次计划重启、无失败；进程退出已核对。
- **b879固定75分钟**：`.platform-fixed-final-soak-20261004/manifest.json`及`source/.platform-soak-final-75m/status.json`，00:45:18至02:00:22 UTC，30/30、1次计划重启、无失败；进程退出已核对。它不覆盖该版本已知MPU/短镜头缺陷。
- **027固定75分钟**：`.platform-fixed-release-soak-20261004/manifest.json`及`source/.platform-soak-release-75m/status.json`，00:58:05至02:13:09 UTC，30/30、1次计划重启、无失败、进程退出已核对；不覆盖后来的aging变更。
- **45d3最大素材实测**：`.platform-final-release-20261004/45d3cccb7d32f91db18dfbbb1efe09c1e8c91c4b/FINAL-RECEIPT.json`，5760²/15秒，归一化写对象37.410秒，含读回完整解码49.299秒，3GiB/2CPU峰值约1.94GiB。相关核心文件相同不等于在b588重新测过。
- **b879失败记录**：[CI37165888551](https://github.com/apedintensor/h3-studio/actions/runs/37165888551)及`.platform-final-release-20261004/b879864ede96d89c01a7a5d92069217fae81b833/INTERIM-RECEIPT.json`。并发ListParts及短镜头时间基是真实缺陷；后来的修复overlay没有改写原失败记录。
- **027已通过基线**：[CI37166508489](https://github.com/apedintensor/h3-studio/actions/runs/37166508489)，`.platform-preview-v2/ci-027039a-receipt.json`和该commit镜像目录。随后又改aging和前端条件说明，最终证据见上表。
- **GPU停机历史**：`gpu-shutdown-receipt.json`记录本轮旧Lium实例DELETE后API核验不存在及当时费用快照；不是最终供应商账单。本次十小时没有重租或付费生成。

## 复核原则

生成来源、真实CPU媒体处理、GPU推理分别记账。模拟水印保留；真实H3历史样片保持各自日期和配置。通过项不能覆盖跳过项，历史负载不能自动归给新版本，不同运行时段不相加成最终版本长测。源码重建能力不能代替素材、作品、未决上传与账务证据的独立备份。

最终汇总收据为 `.platform-preview-v2/final-soak-observations.json`，含四个独立commit、源码归档SHA、实际起止时间、计数、0云/GPU调用和精确来源范围的进程退出检查。预览健康检查为mock生成、CPU粗剪开启、云创建关闭；该收据不授权生产上线。
