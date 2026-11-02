# 托管 API 与原生执行池的兼容边界

更新：2026-11-02，按 #899 重构后的源码核对。本文不代表新的联网验收、价格确认或生产启用。

| 类型 | 代码入口 | 责任 |
|---|---|---|
| 原生容量 | `providers/registry.py`、`capacity_interface.py`、`dstack_capacity.py` | 租赁与停止原机器，保留 intent、承诺费用和未知状态 |
| 原生执行 | `WorkerRunner`、`control`、`fleet` 及原生 backend | 执行接受过的计划，保留租约、fence、输出和费用记录 |
| 托管视频 | `providers/everyframe.py`、`providers/hosted_models.py` | 精确输入编译、单次提交、查询原任务、收集和核对 |
| 托管增强 | `enhance_backend.py` | 按原增强任务提交、观察和验证，不改写源成片 |
| 共用 HTTP | `providers/http.py` | 注入传输、大小限制、拒绝重定向，不自动重试付费提交 |
| 路由 | `scheduling/decide.py`、`scheduling/rebind.py` 及原 demand/admission 操作方 | 当前配置、权限、预算和输入共同决定可选路径 |

精确模型 ID、支持输入和链顺序见 `providers/hosted_models.py` 及能力契约。同名模型不证明它支持原生 checkpoint、seed、采样、guides、首尾帧或参考音轨，也不证明质量等效。

## 保留的契约

1. 预检拒绝不支持的控制和输入，不静默丢素材或替换用户选择。浏览器素材 URL 不是供应商可直接读取的公开授权。
2. 原任务、attempt、提交意图和资金义务先持久化。未知提交只核对原任务，不重新 POST；下载失败只恢复收集，不重新生成。
3. 本地取消、上游停止、供应商退款和用户积分变化是不同事实。缺少证据不能宣称已停止计费。
4. 合法路由变更保留接受过的报价、预算账户、容量策略快照、deployment profile 和指纹；用户不因供应商切换再次付费。
5. 输出经受限下载、解码/元数据验证和持久保存后才公布。密钥、账户详情、原始供应商响应和签名链接不进入日志或公共任务记录。
6. cassette / 假供应商测试覆盖未知响应、错误状态、下载失败和重复提交防护，不是账户可用、真实质量或供应商限额验收。

中央 AI Registry 是已有资源/profile 的本地入口，远端复用环境既有的受控加载路径。本次文档刷新不加载凭据、创建 `.env`、启用供应商或修改中央注册表。

## 历史调研

本文件此前是 2026-10-04 的 Engy / Boyesir 调研。`studio_platform/boyesir_backend.py` 已不在当前源码中，Engy/Boyesir 也不是执行注册表的可用行。旧 adapter 测试数、价格、型号和线路建议不能继续作为当前实现说明。

旧文档留在 Git 历史，其中提到的查询摘要文件不在当前仓库，不能作为随源码交付的证据。重新接入前须核对中央资源和当前正式契约，再做独立能力/安全验收；旧价格或旧账户成功记录不是今天的授权。方向与未完成项见 [PROVIDER-ARCHITECTURE.md](PROVIDER-ARCHITECTURE.md) 和 [ARCHITECTURE-FOUNDATION.md](ARCHITECTURE-FOUNDATION.md)。
