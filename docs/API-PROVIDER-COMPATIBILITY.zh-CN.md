# H3 Studio：Engy / Boyesir 接入核对

核查日期：2026-10-04，Australia/Sydney。范围：供应商公开文档、未鉴权公共 GET、中央资源/profile 元信息、本项目后端与页面源码。本次没有上传素材、付费生成、验证买方密钥、修改运行配置或启动 GPU。

## 结论与交付状态

Boyesir 已有单独实现、默认关闭的异步视频 adapter。Engy 已确认公开视频路由存在，但创建请求及返回结构没有写入其 OpenAPI，完整参数契约仍待补。两者都不等于自托管 H3 全部控制的直接替代品。

**后续十小时开发补充：`studio_platform/boyesir_backend.py` 已通过19项 SQLite 与隔离 PostgreSQL 离线测试，但 `integration_ready=False`，尚未注册到网页、能力目录或 WorkerRunner。** 它使用共享任务提交闸门、创建不自动重试、未知提交保留预算、输出域名/大小限制和秘密日志过滤；具体契约见 `BOYESIR-ADAPTER-CONTRACT.zh-CN.md`。未上传、付费生成或验证账户。新平台网页使用独立业务API；已销毁 GPU 没有重开。用户在会话提供的 Engy 凭据没有复制到文件、日志或调用命令；本次没有导入中央加密库，也没有验证它的权限。

公共接口证据保存于 `research/provider-public-contract-20261004.json`：记录 UTC 核查时间、公开响应摘要哈希、相关视频路由与精选价格字段；不含认证值或用户素材。

## 供应商契约

### Engy

- 来源：[公开文档](https://engy.ai/docs)、[网关 OpenAPI](https://api.engy.ai/openapi.json)、[报价](https://engy.ai/pricing)。
- 基址 `https://api.engy.ai`，不是把视频请求交给聊天接口。
- 创建 `POST /v2/video_generation`；查询 `GET /v2/query/video_generation/{task_id}`；列表 `GET /v2/query/video_generation`；下载 `GET /v2/video_generation/{task_id}/content`。
- `DELETE /v2/video_generation/{task_id}` 的存在只证明有删除路由，不能推断运行中取消、退款或终止计费语义。
- OpenAPI 中创建接口没有 `requestBody`，成功响应 schema 也是空对象。准确上游 model ID、素材编码/上传方式、限制、状态字段、幂等、并发与取消计费均未核实。显示名 MiniMax-H3 不能直接充当已确认的请求 model ID。
- 本次 `/v1/models` 公共 GET 返回 8 个文本/视觉语言模型，没有 H3；官网却列 H3 $0.03/输出秒。这是视频契约与目录覆盖范围的缺口，不能据此断言 H3 不存在。
- 中央正式资源：`provider-engy-api`、`offering-engy-minimax-h3-unverified`；`--list` 显示 Engy 尚无已导入 credential profile。推理 key 与 Agent API 的只读账户 token 是不同身份，不能互换。

### Boyesir

- 来源：[文档](https://boyesir.com/docs)、[公共价格接口](https://boyesir.com/api/model-pricing)。
- `POST https://boyesir.com/v1/videos/generations` 提交，`GET /v1/tasks/{task_id}` 轮询，成功读取 `result.videos[]`。这是现有本地异步任务可适配的结构。
- 通用字段有 `model / prompt / duration / ratio / resolution / images / videos / audios / first_frame_url / last_frame_url`；素材需要公网 URL，不能直接使用我们需要登录 Cookie 的下载地址。
- 上传为 `POST /api/ai/upload`、multipart `files`，每文件 50MB；文档列图片/音频 ¥0.01、视频 ¥0.05，上传本身收费。素材保留 12 小时、结果宣称 24 小时，应下载后由本项目保存。
- 逐型号支持必须单独确认：`bh-minimax-h3-pro-768p` 文档列 9 图/3 音频、4–15 秒，报价 ¥0.25/秒；`bh-hailuo-h3-2k` 列 9 图/3 视频/3 音频、6–10 秒，报价 ¥4.6/次。这些是声明与报价，不是本次成功生成证据。
- 历史成功型号 `lec-minimax-h3-768p` 与当前 `minimax-h3-768p`、Pro 型号保持独立，不能静默换 ID。公共报价均列 ¥0.25/秒，最低计费和可生成时长需分别处理。
- 通用首尾帧字段不保证每个 H3 通道都支持。未找到 seed、steps、sampler、guides、VAE 控制，以及生成任务幂等、并发上限、回调、取消的完整契约。
- 中央已存在 `boyesir/boyesir--boyesir-windows-dpapi`；资源 `boyesir-video-api`、`boyesir-lec-minimax-h3-768p`。本次只核对元信息，没有解密验证或调用账户。

文档冲突需在适配前处理：通用素材上限与逐型号上限、截断与拒绝描述不一致；通用分辨率列表漏 768p；`lec-minimax-h3` 文档最低 6 秒，价格接口默认 5 秒。不能让上游静默截断输入，也不能依默认值自动猜参数。

## 对我们后台的具体影响

旧工作台的 `server.py` 限制 `comfy-local`，直接构建并提交 Comfy 工作流，因此不能只替换 base URL。十小时开发新增的 `platform_app.py` / `studio_platform/` 已把任务账本与执行后端分离；owner 隔离、素材库、历史任务与本地下载可以复用。Boyesir 独立 adapter 还需接通受信素材解析、业务配方和 WorkerRunner，不能把传输层完成当作前端已经可选。

建议 adapter 合约为 `capabilities / validate / prepare_inputs / submit / poll / cancel / fetch_result / reconcile`。前端和后端共同按准确型号进行能力校验；未知能力不可自动选用。

| 现有用户需求 | API 接入处理 |
|---|---|
| Prompt、普通图片参考 | 可映射到已确认通道；保留素材编号与语义，提交前检查数量 |
| 视频/音频参考 | 必须选择有对应能力证据的型号，不丢弃任何输入 |
| 首尾帧、时间锚点、参考视频音轨 | 逐项对接；没有契约的项目显示不可用并说明原因 |
| 分辨率、时长、宽高比 | 使用供应商支持的档位，不把自定义宽高伪装成已支持 |
| seed、steps、采样器、sigma、VAE 参数 | 当前公开契约不足；选择这些控制时保留自托管候选，API 不静默忽略 |
| 下载 MP4 和音频 | 服务器抓取并校验，仍走本站 owner 下载；从 MP4 提取的音轨标为派生产物 |
| BF16、特定 checkpoint、Base 非 Turbo | API 型号名称无法证明权重和采样设置，不承诺与自托管完全等效 |

`web/app.js:165` 的全局 controls 与 `:577` 附近提交控件逻辑需按后端拆分。当前 Comfy 用的补帧和音频归一化不能直接强加给所有第三方 API。新契约应保存请求原始要求，避免用户切换后端后表面选项仍在、实际不生效。

## 防重复收费与用户隔离

下列行为是原单进程工作台的升级要求；新平台的持久任务/attempt/预算/收集链已实现并有离线故障验收，旧 `server.py` 的限制仍保留。Boyesir 上线仍需把独立 adapter 接到这套业务链：

1. 创建前持久化 attempt、owner、准确后端/型号、请求摘要与提交意图；创建 POST 不自动重试。
2. 接单与响应不确定时进入 `submission_unknown`，查询或对账，不自动重投或切供应商。
3. 得到上游 task ID 后恢复查询原任务。上游成功但下载失败进入 `collecting`，只重试下载。
4. 本地取消、上游确认取消与退款分别记录。没有取消契约时不能向用户宣称已停止收费。
5. 素材托管按 owner 和过期时间隔离；供应商凭据、原始响应和带签名结果链接不进入公开 job record。结果下载限制域名、重定向、大小，并实际解码校验。
6. 先验证单 worker adapter 与假后端故障，再增加 API 并发。新平台 PostgreSQL 账本与租约已做并发测试；不能直接复制旧 `server.py` 的进程内 worker 来代替它，也不能将本机并发测试写成真实供应商限额验证。

## 凭据与下一步验收

实际接入继续复用中央 `api_registry.load_api`，显式指定 Boyesir 现有 profile，不新建项目 `.env`。Engy 待通过中央安全入口导入并确定 profile；不要把会话里的 key 放入脚本、命令参数或普通文档。

Lightsail Linux 不能直接解密 Windows 用户绑定 DPAPI 库；远端只给后端 worker 提供所需凭据，凭据供应机制需要另行落实。浏览器只调用我们的服务，不持有平台 key。

后续离线验收至少覆盖：未支持参数拒绝、素材跨用户访问、创建响应丢失、重启恢复、取消不支持、下载失败、上游晚到结果；断言不产生第二次收费创建。获授权后才进行逐输入模式的小额真实生成，分别记录能力、质量、速度与费用，不能把 GET 200 或模型出现在价目表当作验收通过。
