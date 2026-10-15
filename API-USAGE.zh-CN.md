# 映序与 H3 的实际 v1 API

这份文档说明 `studio_platform/api.py` 的代码契约。映序 / Inkseq 的公网入口为 `https://inkseq.com/v1`；实际已发布版本、运行状态与验证范围须分别看发布回执、能力接口和当次预检，不能由文档推断 GPU 可用。本机预览可经 `http://localhost:8850/v1` 代理到8845；模拟模式与真实生成必须明确区分。域名迁移后网页需重新登录一次；已有 API Key 的权限和期限不变。Agent 应使用新域名及已核验的连接迁移入口，不自动跟随任意跨域重定向。

映序网页直接使用这一套API与项目文稿。其他小说或制作服务可以作为受限服务客户端接入；不需要把ComfyUI暴露到公网，也不需要拥有GPU云账号或对象存储主密钥。

## 身份与项目归属

网页使用HttpOnly会话Cookie；正式模式必须密码登录。`superdan`、`supervan`的项目、素材、任务和下载按服务端认证身份隔离。网页API客户端的fetch请求加`X-Expected-Account`，服务端发现Cookie实际账号改变会在操作前返回409、`code=account_context_changed`；前端应保存旧账号草稿并重新核对身份。响应头`X-Authenticated-Account`是非秘密账号名，可核对迟到响应。原生下载链接及video/audio媒体地址不携带自定义请求头，依靠Cookie及服务端owner授权；换账户时前端卸载旧媒体。

机器客户端使用 `Authorization: Bearer ...`。现在网页账户可在「Agent API」创建个人 API key：绑定当前 owner，明确选择现有项目列表，或「本人全部项目（包括未来新故事）」；用户与供应商的 API key 是不同身份。key 的值只在创建时返回一次，数据库只存 SHA256 与非秘密前缀/权限/时间元信息；列表不返回值，撤销立即阻止后续请求。有效期 1–365 天，默认 90 天，最多 50 个有效 key。生产密码更改、账户停用也会阻止旧个人 key；local-test 创建的 key 不能带到 password 模式使用。

可授予 `projects:read`、`projects:create`、`projects:write`、`assets:read`、`assets:write`、`jobs:read`、`jobs:write`。创建新故事必须同时有 `projects:create` 和全部本人项目范围；`projects:write` 可编辑授权文稿。所有下载与生成仍校验 owner、项目与各自 scope，不因选择全部项目跨到账户外。`GET/POST /v1/api-keys` 和 `DELETE /v1/api-keys/{id}` 仅允许网页登录会话，任何 Bearer key 都不能生成新 key、改权限或撤销 key。机器 key 不放进公开 JavaScript、localStorage、日志或 URL；使用受信任 Agent 运行环境/秘密库。

正式密码模式还支持网页登录会话 `POST /v1/auth/password`，body 为 `old_password/new_password`。新密码至少12字符、最多72 UTF-8字节；验证旧密码，成功返回 `{changed:true,reauthenticate:true}` 并清Cookie，撤销本人旧会话与个人API key，随后重新登录/创建key。其他账户不受影响。旧密码尝试按账户限5次/5分钟；Bearer key不能调用，local-test不提供改密码。密码只在HTTPS请求和运行进程内传递，不写日志或文档。

旧操作员 `python -m studio_platform.manage register-client --help` 静态客户端仍兼容：原有项目范围与五类只读/生成权限不变，不自动升级为创作权限。创建个人 key 不需要分享网页 Cookie 或操作员权限。上述存储用量接口仍不对机器身份开放。

本项目供应商配置仍按中央 `C:/Users/danmo/Desktop/AI-Registry/API_USAGE.md` 加载。上述外部客户端token没有被本轮自动创建或导入中央profile；不要把资源ID当model ID或凭空写一个可用profile。

## 已实现端点

| 操作 | 路径 | 主要约束 |
|---|---|---|
| 查看模型控制与输入限制 | GET `/v1/capabilities` | 用准确recipe/model/能力版本，不假定所有执行池可用 |
| 列出项目 | GET `/v1/projects?limit=100&offset=0` | limit 1–100；授权过滤发生在SQL分页之前 |
| 读文稿 | GET `/v1/projects/{project_id}` | v4文稿、服务端version与更新时间 |
| 创建/保存文稿 | POST `/v1/projects`；PUT `/v1/projects/{project_id}` | 创建接受 title/logline/id、可选 workspace=freestyle 或旧 project v4；机器创建需 projects:create+全部本人项目；保存需 projects:write+expected_version |
| 原子创作操作 | POST `/v1/projects/{id}/actions` | expected_version + 1–200 actions；可带稳定 Idempotency-Key；失败整批回滚 |
| 节点操作 | GET/POST `/v1/projects/{id}/entities`；PATCH/DELETE `/v1/projects/{id}/entities/{entity_id}` | GET可按type/parent_id筛选；POST entity、PATCH patch都带expected_version；DELETE用query expected_version、显式cascade |
| 版本轻查询 | GET `/v1/projects/{id}/meta` | id/title/version/updated_at；发现新版本后显式读取，不盲目覆盖网页草稿 |
| Agent说明 | GET `/v1/agent-guide`；GET `/v1/guided-schema` | 实际操作顺序、类型、scope及未实现能力 |
| 导出文稿/镜头表 | GET `/v1/projects/{id}/export?format=json或csv` | 同账户/项目授权；JSON可导回v4；CSV转义公式起始符 |
| 导出字幕 | GET `/v1/projects/{id}/chapters/{chapter_id}/subtitles.srt` | 当前时间线的字幕须经过显式确认 |
| 个人API key | GET/POST `/v1/api-keys`；DELETE `/v1/api-keys/{id}` | 仅网页登录；创建响应{api_key,key}，list {api_keys,available_scopes}；不落明文 |
| 上传素材 | POST `/v1/assets` | multipart中恰好一个file、一个client_project_id、可选一个稳定client_asset_id；拒绝重复/未知字段，实际解码/校验 |
| 读素材列表/状态 | GET `/v1/assets?client_project_id=...`；GET `/v1/assets/{id}` | 只在ready后可用于计划 |
| 创建音视频选段 | POST `/v1/assets/{id}/derivatives` | JSON start/end秒；保留原件，不在生成请求里偷偷裁剪 |
| 恢复已接收素材处理 | POST `/v1/assets/{id}/resume` | 同一asset；不等于浏览器断线按字节续传 |
| 创建H3预检计划 | POST `/v1/generation-plans` | 输出实际参数/尺寸/片长、blockers、期限及预算性质 |
| 读取网页同一生成草稿 | GET `/v1/projects/{p}/shots/{s}/generation-draft` | 返回 draft、issues、project_version、shot_version、web_url；不创建任务 |
| 从已保存草稿预检 | POST `/v1/projects/{p}/shots/{s}/generation-plans` | expected_version为项目版本，可选capabilities_version；显式选段可生成派生素材，仍不提交GPU任务 |
| 创建章节粗剪计划 | POST `/v1/render-plans` | 从服务端文稿编译已采用视频/音轨时间线 |
| 确认任务 | POST `/v1/jobs` | body只有plan_id；必须带稳定Idempotency-Key |
| 查任务/列表 | GET `/v1/jobs/{id}`；GET `/v1/jobs` | 可按client_project_id；limit 1–100，offset非负 |
| 取消 | POST `/v1/jobs/{id}/cancel` | 已提交的任务需确认上游取消，不能立即算免费/释放槽 |
| 批次 | POST/GET `/v1/batches`；GET `/v1/batches/{id}` | 列表limit 1–10、offset，返回逐项状态摘要及has_more/next_offset；单批详情含job结果；重复请求不重做已接受项 |
| 批次恢复/取消 | 恢复重发POST `/v1/batches`；取消POST `/v1/batches/{id}/cancel` | 恢复必须保持原body与Idempotency-Key，只有可恢复步骤继续 |
| 项目任务总览 | GET `/v1/activity-summary?client_project_id=...` | 全部状态聚合，与列表已加载页数分开 |
| 结果清单 | GET `/v1/jobs/{id}/artifacts` | 类型、SHA256、大小、私有content/download URL |
| 下载结果/素材 | GET或HEAD `/v1/artifacts/{id}/content`、`/v1/assets/{id}/content` | `?download=1`附件；Local支持Range；始终鉴权 |

Swagger/OpenAPI由服务生成，访问仍需本账户身份。表中的模型支持是H3版本化配方的控制契约，当前未接通的Engy/Boyesir、图像生成、音乐生成、Marble不会因为有下拉框就自动可用。

上传文件完整接收后，若CPU预处理名额等待30秒仍不可用，接口返回503和`Retry-After: 5`。原件及同一asset收据保留，素材列表会显示繁忙原因；客户端应查询原client_asset_id对应的素材并调用其`/resume`，不要创建另一个上传ID。恢复仍会鉴权且可能继续繁忙，不会自动创建付费生成。多轨原件不改写；模型参考副本仅采用首个受检真实视频轨和首个音轨，metadata.notes会说明这一选择。

上传前读取 `/v1/capabilities` 的 `upload_constraints`。当前服务按 `studio_platform/media.py` 实际解码校验：静态图片扩展名为 `.png/.jpg/.jpeg/.webp`，视频为 `.mp4/.mov`，纯音频为 `.wav/.mp3/.flac`，实际格式必须匹配扩展名。图片和视频**每边 256–5760 像素，宽高比 0.4–2.5**；320×180 视频会因短边不足256而拒绝。音视频**上传原件**可为0.1–3600秒；**模型参考选段**需2–15秒，并满足同类总时长；**当前部署**的 `execution_support` 又可更严格。不要把上传通过当作当前GPU能够执行。HTTP422或内容验证失败的收据，应先查原收据与限制；反复 `/resume` 不能修复错误尺寸或格式，也不能通过换上传ID解决。保留原件，经明确要求修正源素材，不自动缩放、裁剪或转码。

## Agent 快速创作：一次视频，与网页共用草稿

网站 `/for-agents/guide.json` 的 `quick_creation`、`examples` 给出可发现的完整示例；`/for-agents/SKILL.md` 说明安全调用与恢复规则。只想生成一个视频时，不必让用户建立章节：

1. 带稳定 `Idempotency-Key` 创建 `POST /v1/projects {"title":"我的短片","workspace":"freestyle"}`。返回普通项目 envelope；`project.journey.reviewShotId` 是自动建立的镜头。仅新建草稿，不启动生成。
2. 如有参考，上传到该项目，并等待原收据 `status=ready`。每次逻辑上传保存稳定 `client_asset_id`；不确定结果时按它查原收据并恢复，不换 ID 重传。
3. 读取当前项目版本，向 `/v1/projects/{p}/actions` 提交下面的配置动作。这里只保存草稿，服务端会维护网页中对应输入分区、提示词与控制值。

```json
{
  "expected_version": 1,
  "actions": [{
    "op": "shot.configure_generation",
    "shot_id": "returned-shot-id",
    "recipe_id": "h3-base-fl2va-v1",
    "prompt": "阳光中一片叶子轻轻晃动，镜头缓慢推近。",
    "controls": {"duration": 5, "seed": "42"},
    "inputs": {"first_frame": {"asset_id": "ready-image-receipt"}}
  }]
}
```

替换实际 ID 和当前 `expected_version`，为这次逻辑编辑保存独立幂等键。`controls` 合并字段，省略字段和输入槽位会保留原值；列表 `[]` 清空，首尾帧 `null` 清空。只换配方不会悄悄删除不兼容的参考，需核对保存输入及随后预检错误；模式不兼容可能在预检返回HTTP422，不保证已出现在草稿 `issues`。完整结构以 `/v1/guided-schema` 为准。

`inputs` 分成 `images/videos/audios/first_frame/last_frame/guides`，全部使用同项目**上传收据 ID**，包括 guide 的 `media_id`；无需手拼网页实体 ID。普通列表项为 `{asset_id,purpose?}`，视频另有 `include_audio`。音视频或锚点可以显式传 `source_range:{start,end}` 秒；同一关联省略此字段保留选段，`null` 清除选段。不要为了通过限制擅自剪短或静音。

4. GET `/v1/projects/{p}/shots/{s}/generation-draft`，核对 `draft/ issues/ project_version`；读取 `/v1/capabilities` 的实时约束及仅对未设置字段生效的 `deployment_preset`。随后 POST 同路径的 `/generation-plans`，body 为 `{"expected_version":当前project_version}`，可加当前 `capabilities_version`。服务端从已保存文稿编译，不需要手算 source_hash 或 shot_version。存在明确选段时可能创建派生素材，需要 `assets:write`，不代表已经生成视频。
5. 检查计划实际参数、阻塞、费用与期限；仅在用户已经授权的范围内，用单独持久幂等键 POST `/v1/jobs {"plan_id":"..."}`。未知结果继续原计划/原键；不另建任务“重试”。
6. 查原 job，成功后取 artifact 清单并核对下载 SHA256。用 `artifact.adopt` 加入候选（`select:false`）；只有明确要采用时才 `shot.select`，传采用后文稿的**实体 ID**。音频仅以实际清单为准，不凭音频开关承诺一定存在 FLAC。
7. 返回 `https://inkseq.com/freestyle?project={p}&entity={s}`。用户仍需登录有权限的账户；可看到同一份草稿、输入、控制与结果候选，再局部修改重做。其他章节和旧候选保留。

直接调用通用 `/v1/generation-plans` 仍受支持，但它不把请求 prompt/controls 自动写回网页。需要“Agent 做了什么，网页就能继续改什么”时，应使用上述保存草稿路径。

随 Skill 提供的 helper 支持 `request`（含 PATCH）、`upload`、`resume-upload`、`poll` 和 `download`。`resume-upload --project P --asset-id 原client_asset_id` 只定位并恢复原收据，不重传文件；`poll --job ID --max-wait 600` 只做有界 GET，遵守 Retry-After，`waiting` 表示需要稍后继续原任务，绝不自动重新生成或采用。输出文件拒绝覆盖；网络错误后已有部分下载可以保留检查，改输出文件名重新下载同一个 artifact。

只有专用 `download` 可以接收可信同源 content 响应的一次 307 签名 HTTPS 跳转：目标须为公网443端口，DNS全部结果经核验，连接固定到核验后的 IP，TLS仍验证原主机名；使用不含 Authorization/Cookie/Referer 的独立客户端，拒绝二次跳转。通用 request 继续拒绝重定向，签名URL不输出到日志或收据。辅助脚本的成功退出不表示任务成功，必须读返回状态。

## H3请求与确认

现有两个配方：`h3-base-fl2va-v1`首尾帧/文生音视频、`h3-base-ref2va-v1`参考生成。平台能力返回的`model_id`为`MiniMax-H3-Base-BF16`；这是本平台模型标识，不是远端供应商API的通用model参数。请求通过`recipe_id`选择配置，不接受顶层`model`字段；Comfy实际权重文件映射见`comfy_workflow.py`。它没有被静默替换成VDN或供应商小模型。

先检查 `/v1/capabilities` 每个配方的 `execution_support`，再创建预检。`implemented` 和模型最大输入限制不等于当前部署已开放；实际范围由 `execution_support.constraints` 给出。`status=runtime_required` 表示运营已允许在该范围内排队，新 GPU 启动后仍须完成对应套件的真实验证，才可执行用户任务；这不是已实测成功的声明，也不保证当前有可用 GPU。`qualified` 也仍需预检当下的预算、容量及有效窗口；`not_qualified`、`disabled` 或 `unavailable` 时可保存文稿和素材，不应提交生成。`input_limits` 的数量按参考与锚点的同类唯一素材计，首尾帧另计但仍受图片像素及全局文件数限制；音视频时长限制分别约束同类素材总和，原选段与模型副本均须满足。

下面是请求结构示例，ID必须换成当前账号真实已有的项目、镜头和ready素材。这里没有执行生成，也没有给出凭据值。seed 用十进制字符串传递，上限读取当前能力的 `controls.seed.maximum_decimal`：已固定的 WanGP 为 `4294967295`，Comfy 保留 uint64 范围。超范围值会拒绝，不会静默截断。

```json
{
  "client_ref": {"project_id": "existing-project", "shot_id": "existing-shot", "shot_version": 1},
  "recipe_id": "h3-base-ref2va-v1",
  "prompt": "描述当前镜头中的人物、动作、环境与镜头运动",
  "inputs": {
    "images": ["ready-image-id"],
    "videos": [{"asset_id": "ready-video-segment-id", "include_audio": false}],
    "audios": ["ready-audio-segment-id"]
  },
  "controls": {
    "duration": 5,
    "resolution": "480P",
    "aspect_ratio": "16:9",
    "seed": "424242",
    "steps": 50,
    "generate_audio": true
  }
}
```

先POST计划并展示返回的effective_request/output_spec/estimate/blockers。预算预留额不等于最终供应商账单。`status=ready`表示可接受确认；`execution.admission_state=waiting_capacity`表示确认后先等开机和模型验收，不保证马上生成。无已批准策略、报价、预算或能力时会blocked。

用户确认后POST `/v1/jobs`，body为`{"plan_id":"returned-plan-id"}`，Idempotency-Key由客户端生成并持久保存，例如一个新UUID。网络超时重试必须保持**同一plan与同一key**；不能每点一次生成新key。若修改文稿/角色/参考，先获取新计划，旧结果保留为旧版本候选。

模型配方的结构上限为9图/3视频/3音频、普通参考总数12；每份音视频选段2–15秒，视频选段合计与音频选段合计分别不超过15秒；时间锚点最多8个。这些不是当前部署的可提交上限，执行策略可以更严格，请读取上述 `execution_support`。上传单文件默认512MiB。首尾帧、锚点、原声开关和更多控制以能力接口为准；音频VAE分块当前不可用，会明确拒绝而非忽略。

## 状态、恢复和下载

章节粗剪使用`POST /v1/render-plans`，请求包含`client_ref.project_id/chapter_id`、`resolution`（480P或720P），可选`aspect`（16:9、9:16、1:1、4:3、3:4，缺省沿用项目画幅）及`burn_subtitles`（默认false）。镜头选段、声音和字幕正文从当前账户已保存的项目文稿读取；不能把任意FFmpeg/ASS/字体路径传给接口。开启烧录前须人工确认这一版字幕与时间线，返回的`timeline.subtitles`给出实际24fps帧/秒范围。新计划为render v3，需要对应配置的合格CPUworker；历史v1/v2不能被静默改写。详细限制见[字幕契约](CAPTION-BURN-IN-CONTRACT.zh-CN.md)。

典型路径：waiting_capacity → queued → claimed → submitting → running → collecting → succeeded。未知提交为submission_unknown；此时继续查同一个job，不新投。cancel_requested需要上游核对。recovery_hold表示灾难恢复或状态与attempt证据矛盾/缺失，需操作员核对；不允许从网页恢复成新的付费执行。

任务succeeded后取artifacts，使用同身份访问content_url或download_url。MP4与实际返回的独立音频artifact各有大小/哈希；只有清单包含FLAC时才承诺独立声音文件，H3有声任务或CPU粗剪都不应仅凭开关猜文件存在。`result.billing_status=pending`时已校验的成片仍可下载；`result.actual_cost_microusd=null`代表费用未确认，不代表免费。有值时单位为微美元，除以1,000,000才是美元；不存在名为`actual_cost`的返回字段。保存后核对SHA256；不要把临时签名URL作为作品长期ID。如果对象存储返回重定向，客户端不应将Authorization转发给别的主机；签名查询串不得写日志。API下载通过的证据与浏览器最终保存到磁盘是两件事。

轮询建议从2秒开始，持续运行时降到5–10秒；429遵守Retry-After，401重新鉴权，409核对版本/账号/计划，503保留本地稿后重试。错误详情不应被当成可重放的供应商请求。当前没有已部署webhook，不用假定job callback已经可用。

单进程入口同时最多32个HTTP请求；鉴权数据库访问并发8、密码登录并发2，每个owner最多8个请求，私有下载每owner2个/全局4个，上传每owner2个/全局4个。多个机器客户端共享其owner额度；这些是保护应用的初始限制，不是GPU任务并发。流式下载占用到ASGI传输结束或断连才释放，超限返回429及Retry-After。跨进程/多主机入口需要共享限流或反向代理配置，不能把每个进程的上限当作整个集群上限。

映序客户端将GET读取限制为总4并发、其中媒体2并发；只对429按Retry-After有限重试，最多额外2次，账户切换会中止队列和旧响应。POST/PUT等写入、上传和生成确认仍不自动重试。第三方客户端应采用同等节流和持久幂等策略，不能以大量HTTP请求增加GPU吞吐。

登录正文接收总期限15秒；每次响应写入空闲期限30秒，私有媒体响应总期限300秒。超时若尚未发送响应头则504，已发送部分媒体时中止连接，不在视频中拼接错误JSON，也不重新生成。请求结束显式关闭流式文件句柄；声明长度与实际输出不符时拒绝发送假完成标记。已启动的同步磁盘读取必须等线程归还，超时不能安全强杀线程，等待期间仍保留名额和句柄。客户端可重新鉴权读取原文件，Local支持Range继续下载。这是单进程CPU入口的资源保护，不是任务生成耗时上限。

## 验证范围

本轮本地HTTP契约覆盖双用户、受限机器scope、分页、同名项目跨账号操作阻挡、版本冲突、幂等批次、私有Range/附件与哈希。SQLite和隔离PostgreSQL均有测试；CPU粗剪实际编码输出MP4/FLAC。真实H3新GPU池、公网域名、供应商API与云存储在线读写需要分别验收。

## Agent 创作与网页回显（2026-10-04新增）

以下为契约示例，不含凭据值。所有 `/v1` 请求均走同一已认证账户；不要在 body 传 owner。

1. `POST /v1/projects`，JSON `{"title":"雨夜来信","logline":"一个送信人的故事"}`，带稳定 `Idempotency-Key`。返回 `id,version,updated_at,project`。相同 key/body 的重试复用同一故事；修改 body 后用新的 key。若传自己的 `id`，key 只在该项目与调用身份内幂等。
2. 使用返回 id，`POST /v1/projects/{id}/actions`。响应仍是完整文稿 envelope；所有 action 都成功后版本才加一。409 要重新 GET、核对并重做基于新版本的操作，不能丢掉用户修改后盲重试。

```json
{
  "expected_version": 1,
  "actions": [
    {"op":"entity.create","entity":{"id":"chapter-one","type":"chapter","title":"第一章"}},
    {"op":"entity.create","entity":{"id":"scene-one","type":"scene","parentId":"chapter-one","title":"雨夜车站","data":{"script":"人物对白和场景说明"}}},
    {"op":"entity.create","entity":{"id":"shot-one","type":"shot","parentId":"scene-one","title":"走入车站","data":{"seconds":5,"prompt":"缓慢推镜，人物走入雨夜车站"}}},
    {"op":"journey.update","patch":{"brief":{"aspect":"16:9"},"sound":{"mode":"silent"}}}
  ]
}
```

实体支持 chapter/scene/shot/character/location/image/audio/video/note/generation。创建字段 `id,type,parentId,title,description,data,order,status`；id可省略由服务生成，章节的parentId为空、场戏指向章节、镜头指向场戏。角色、地点、素材等可位于项目或故事层级。`entity.update` 使用 `entity_id,patch`，不允许修改id/type/version；data仅合并一层，嵌套对象/数组是显式替换。例如修改`data.h3`需带要保留的H3设置；角色`data.looks`、场戏`data.cast`（characterId/lookId）、剧本`data.script`都沿用网页v4结构。更新自动增加实体version，项目同时增加服务端version。

| op | 字段与作用 |
|---|---|
| entity.create/update/delete | entity；entity_id+patch；entity_id+cascade。含子级删除必须显式cascade=true；只删文稿节点，原文件/历史任务不会删除。 |
| link.create/delete | link:{id?,source,target,role}；link_id。用途为identity/location/firstFrame/lastFrame/motion/audio/reference/dependency；类型、重复边、循环受验证。 |
| project.update | patch:{title?,logline?} |
| journey.update | patch合并流程顶层；brief/sound/角色审查/交付设定等嵌套值明确替换 |
| layout.update | patch:{positions?,viewport?}，画布与引导工作台共享文稿 |
| asset.attach | asset_id（上传收据）、entity_id?/title?/parent_id?；可带shot_id、role、select。需projects:write+assets:read，只接受同项目ready素材。 |
| artifact.adopt | artifact_id、entity_id?/title?/parent_id?/shot_id?/select?。需projects:write+jobs:read，验证同账户同项目已完成任务；不接收任意下载URL。 |
| shot.select | shot_id、entity_id（空值解除采用）；视频或图片候选，显式采用后网页和粗剪读取同一选择 |
| shot.trim | shot_id/start/end秒；将选段绑定到当前已采用视频的确切文件身份，实际长度在render预检再核对 |
| sound.set | chapter_id/tracks及可选mode（silent/dialogue/music/mixed）；音轨沿用网页assetId/fileId/shotId/offset/start/end/gain/muted结构 |
| sound.generated | shot_id、gain?（0–1，默认0.7）；先加入同任务视频与独立FLAC并采用视频，建立跟随镜头/选段的同次生成声音绑定 |
| captions.set | chapter_id/cues，每条{id,start,end,text}；清除原确认，保留可编辑正文 |
| captions.confirm | chapter_id/reviewed:true；调用方明确核对本版字幕与时间线，服务端重建确认依据；后续改时间线后必须再确认 |

已有网页功能中的声音、字幕、视频选段、角色造型、镜头和参考都可通过上述文稿编辑表达。H3生成仍需能力查询→预检→jobs确认；章节粗剪仍走render-plans→jobs，不因文稿action自动产生费用。

上传外部 Agent 生成结果时，先 multipart 上传到该故事，再 `asset.attach`（必要时shot_id+select:true）。本站生成结果用 `artifact.adopt`；同次声音通过第二个audio artifact adopt后再`sound.generated`关联。网页读取的就是这些已提交文稿；正在编辑的网页会提示更新，用户显式加载并处理草稿，不静默覆盖。这里只提供同账户不同客户端协作，还没有跨账户团队成员/邀请机制。

复制故事时可 GET/导出 JSON 改项目ID后 POST，章节和文稿可以复用；**媒体仍按原项目隔离**，需要上传到新项目并重新关联，不能复制cloudAssetId冒充新项目文件。尚无服务器端媒体ZIP打包接口；原始文件可通过已授权content/download端点逐个下载。自动LLM编剧、未配置图像/音乐/Marble生成不在本次实现范围，不能仅凭entity类型就声称可执行。

验证：隔离临时数据库与假key，无外网/供应商调用；真实GPU/public部署另看交付记录。本次普通项目备份明确排除个人key认证表，编辑幂等收据属于业务数据；恢复后需重新配置身份/创建key。
