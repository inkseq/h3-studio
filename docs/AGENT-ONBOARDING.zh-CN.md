# 把映序交给 Agent，回到网页继续创作

2026-10-04。范围：公开发现与使用说明、用户授权入口、同一故事的编辑动态和内容定位。没有恢复 GPU 租赁、自动扩容或付费生成。

## 使用路径

1. 打开映序，点击顶部「连接 AI」。登录并创建/打开一个云故事；本机作品可以先复制到云端。
2. 保存云版本，点击「复制给 AI 的工作说明」，交给 Codex。说明只含公开文档、项目/可选实体位置及调用约定，不含故事正文、素材或 Key。
3. 「为这个故事配置 Key」进入已有账户 Key 管理。默认建议当前故事、7 天、编辑与上传；可明确选择生成权限。创建仍由用户确认，完整值仅创建后显示一次。Key 放入 Agent 的安全凭据配置，不放聊天或链接。
4. Agent 读公开文档及 Skill，使用自己的 AI 编写内容，再通过鉴权 API 保存、上传、预检和提交已授权任务。真实可执行能力以 `/v1/capabilities` 与预检结果为准；网站链接不等于访问或付费授权。
5. 故事侧栏「创作动态」显示网页/API Key 的已提交修改、受影响章节与镜头，以及原有任务状态。点击具体内容继续编辑、调整参考/参数或审核候选。重新生成使用已有预检/确认流程，不会因点击记录而自动提交。

## 公开入口与私有数据边界

- `/for-agents`：无 JavaScript 依赖的 HTML 入门说明。
- `/llms.txt`：机器可读索引；不保证所有 Agent 自动识别或安装 Skill。
- `/for-agents/guide.json`：权限、流程、端点和边界说明。
- `/for-agents/SKILL.md`、`/for-agents/skill.zip`：Skill 与受限两文件下载包。
- 首页 HTML 元信息和 HTTP Link 响应头可发现上述入口。
- 只有精确发现路径的 GET/HEAD 公开；`/v1/*`、OpenAPI、项目、活动、文件仍须鉴权。分享 URL 不携带登录态。
- 定位链接：`/?project=ID&entity=ID`，或 `/?project=ID&panel=activity`。先确认身份、打开故事，再定位；旧草稿遵循已有恢复/冲突选择。
- 服务当前没有内置 AI 编剧、图像/音乐生成、Marble 和多人实时编辑；不能将 API 可编辑故事描述为这些模型已接通。

## 活动契约

`GET /v1/projects/{project_id}/activity?limit=50&before_version=N`。每个成功文稿版本一条事件，和文稿及幂等回执同事务写入；失败、409、重放不产生假事件。返回受控摘要、操作类型、调用身份显示名、版本、UTC 时间和最多 100 个目标 ID。历史文稿不补造事件。

仅允许所有者及匹配项目权限的 `projects:read` Key 读取。记录不含文稿正文、提示词、凭据值、Key ID/摘要或签名链接。API Key 标识来源身份，不推断调用方一定是 AI。

动态打开期间每 15 秒刷新，页面隐藏时跳过；使用版本游标翻页。若两次刷新之间新增超过一页且窗口不重叠，提示并重新打开最新窗口，允许继续加载中间历史。账户/故事切换后的旧响应会被丢弃。云端新版只提示，不直接覆盖网页草稿。

## 代码与验收

- 后端：`studio_platform/agent_discovery.py`、`project_activity.py`、`guided.py`、`api.py`。
- 前端 canonical：`../video-studio-design/studio-app/src/AgentConnect.jsx`、`AgentLink.jsx`、`ProjectActivity.jsx`，以及对应模型/测试。`yingxu/` 是同步生成的源码快照。
- 公开发现、权限、事务、幂等、目标定位、旧库升级测试覆盖于 `test_agent_discovery.py` 与 `test_platform_project_activity.py`；后者加入 PostgreSQL CI。
- 本地前端 236 项通过，Vite 构建通过；定向后端 62 项通过。
- 本地全套 936 项中 12 跳过，已有媒体下载超时测试在 80ms 截止前未进入发送导致 1 次失败；单独重跑整个 admission-review 模块 8 项通过。发布仍须等待确切提交 CI / PostgreSQL 验收，不把这次时序失败隐藏为全绿。
- 隔离浏览器走查：API/PAT 写章节与镜头 → 网页看见署名动态 → 点击回到镜头和完整 H3 控制；网页未同步草稿与 Agent 并行修改时保留草稿并提示新版本。无模型推理。
- 生产版本、CI/CD 和匿名在线核验以本轮发布回执为准。
