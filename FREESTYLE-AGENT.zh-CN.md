# 快速创作与 Agent 共用草稿

快速视频不需要 Agent 先设计章节。`POST /v1/projects` 传 `workspace: "freestyle"` 即可创建单镜头作品；网页与 Agent 共同读写现有 v4 项目，复用上传、任务队列、候选与权限体系。

## 使用入口

用户打开 `/freestyle`，进入云作品，点击 **连接 AI**。复制说明会带当前项目与镜头链接，不包含 API Key。按需要选择“编写与调整”或“编写并生成”，进入现有 Key 管理，默认建议当前作品、7 天。新建其他作品需要单独的 `projects:create` 与全项目授权；当前作品 Key 不自动扩大权限。

公开发现：`/for-agents`、`/for-agents/guide.json`、`/for-agents/SKILL.md`、`/llms.txt`。完整说明与命令见 `skills/inkseq/SKILL.md`、`API-USAGE.zh-CN.md`。

## 同一份草稿的调用顺序

1. 创建快速作品，或读取用户链接里的现有项目。新项目的 `project.journey.reviewShotId` 是目标镜头；不要猜 ID 或版本。
2. 上传素材取得同账户、同项目的 ready 收据，保留稳定的 `client_asset_id`。
3. 用项目 actions 中的 `shot.configure_generation` 保存配方、提示词、控制与分区输入。输入使用上传收据 ID；服务端转换成网页现有实体/关联，不要求 Agent 手拼内部结构。
4. `GET /v1/projects/{project}/shots/{shot}/generation-draft` 获取规范草稿、当前项目/镜头版本、问题及网页链接。
5. `POST /v1/projects/{project}/shots/{shot}/generation-plans`，传当前 `expected_version`，可传 `capabilities_version`。计划从保存的草稿构建；选段使用现有 CPU 派生服务，随后重新核对版本。预检本身不提交生成、不租 GPU。
6. 只有预检 ready 且用户授权生成时，复用现有 `POST /v1/jobs`，以稳定的 Idempotency-Key 提交 plan。未知结果核对原任务，不换 Key 重复提交。
7. 轮询任务，按清单下载并核对 SHA-256；通过 `artifact.adopt` 加入同一镜头候选。默认不替换已采用结果，明确选择才执行 `shot.select`。

`/v1/generation-plans` 的原始 H3 请求入口继续存在，但它不会保存网页草稿。快速创作优先使用上述草稿预检入口，避免网页配置与任务请求不一致。

## 编辑约定

- 省略字段/分区即保留；`controls` 按字段合并。
- `images/videos/audios/guides: []` 清空对应关联；首尾帧 `null` 清空该槽位。不会删除上传文件。
- 改配方不会自动删除不兼容素材；预检要求用户或 Agent 明确处理冲突。
- 支持首尾帧、图片/视频/音频参考、视频原声开关、选段、时间锚点及已开放控制参数。数量、尺寸与时长仍按 capabilities 和执行池预检；原生模型上限不代表当前运营范围。
- 项目修改必须使用当前 `expected_version`；编辑幂等重试使用原 key 与原 body。
- 本机未同步文件、失效选段和跨账户素材不作为有效输入。

## 网页接手

`/freestyle?project=…&entity=…` 定位作品及镜头；加 `panel=activity` 打开动态。链接不授予访问权限。打开另一份作品前保留草稿；远端有修改时提示载入，不静默覆盖本地未保存内容。浏览器轮询结果按 `cloudArtifactId` 识别 Agent 已加入的产物，不重复制造候选，也不自动采用。

## 验收与发布记录

代码验收覆盖创建/编辑幂等、版本冲突、素材隔离、typed inputs、选段/锚点往返、模拟任务/采用、下载跳转认证隔离、前后端请求一致性及网页链接/草稿保护。Node/Python 对照测试直接执行实际浏览器的请求构建函数。

本轮浏览器使用独立 loopback 测试数据，实际检查 API 创建的草稿在快速页显示提示词/清晰度/时长，连接 AI 定位同一作品，远端更新提示及未保存草稿保留。这里的 API/模拟验收不代表新增 H3 GPU 推理或质量测试。

生产发布、线上检查与 GPU 生命周期的最终回执保存在 `.platform-demand-live/QUICK-AGENT-RESULT.zh-CN.md`；该回执存在并记录成功前，不把本地实现视为上线。生产预算、原截止时间及用户任务保护不因本次 API 改进而重置。
