# Boyesir 视频 transport adapter：离线契约

核查与验收：2026-10-04，Australia/Sydney。代码在 `studio_platform/boyesir_backend.py`，测试在 `test_platform_boyesir_backend.py`。**默认关闭、未注册到 API / capabilities / WorkerRunner / UI，不能当作已接通生成服务。** 本轮只有公开文档 GET 和假 HTTP 响应；没有解密真实 key、上传素材、查账户、收费生成或启动云资源。

## 明确支持的声明范围

以下是本次[供应商公开文档](https://boyesir.com/docs)里的接口声明，未通过真实生成验证；像素、权重、量化和音频输出不能从显示名推断。

| 准确上游 model ID | 分辨率字符串 | 整数秒 | 图 / 视频 / 音频参考上限 |
|---|---|---|---|
| `bh-minimax-h3-pro-768p` | `768p` | 4–15 | 9 / 未确认，适配层拒绝 / 3 |
| `bh-hailuo-h3-2k` | `2k` | 6–10 | 9 / 3 / 3 |

音频参考须搭配图片或视频。首尾帧虽是通用字段，但逐H3型号契约不足，当前拒绝；seed、steps、guides、VAE、精确宽高等同样拒绝。`ratio` 和 `output_audio` 必须显式填写 `provider_default`，表示用户接受上游决定；不接受把既有16:9、静音或要生成音频的要求悄悄删除。分辨率仅是供应商档位，不把768p推算成某组宽高。

`lec-minimax-h3-768p`、`minimax-h3-768p`、`lec-minimax-h3` 都与上述两型号分开，当前标为blocked。中央资源 `boyesir-lec-minimax-h3-768p` 的图片参考/4、8、10、12秒成功记录只是历史证据。公开文档现在列该型号1–15秒，并不扩充我们已经验收的能力。

上游 `POST /v1/videos/generations` 返回task ID；`GET /v1/tasks/{id}` 的 `succeeded/result.videos[]` 提供结果。该异步协议、允许结果域及402无费用语义来自同一官方文档。通用素材数量截断/拒绝说明存在冲突，所以适配层在本地严格校验，不交给上游截断。[公共报价](https://boyesir.com/api/model-pricing)不作为实际账单、可用性或退款证明。

## 唯一请求格式

这是新的独立配方，不是现有H3请求剥掉“不兼容字段”后的子集。字段精确匹配，未知字段拒绝；没有自动默认、model alias、fallback或升/降分辨率。

```json
{
  "recipe_id": "boyesir-video-v1",
  "provider_request": {
    "model": "bh-minimax-h3-pro-768p",
    "prompt": "@图片1 是主角，镜头慢慢推近",
    "duration": 8,
    "resolution": "768p",
    "ratio": "provider_default",
    "output_audio": "provider_default",
    "media": [
      {"asset_id": "approved-asset-id", "kind": "image", "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
    ]
  }
}
```

示例SHA只是结构占位，不是真素材。job另外携带由可信账本确认的 `id/tenant_id/owner_id/project_id`，tag为真实attempt ID。媒体顺序保持，分别映射images/videos/audios，prompt不改写。提示词最多32000 UTF-8字节、参考总数≤15是本站保守限制，不是上游最大值。

## 输入素材：必须先过受信授权适配器

构造参数 `media_resolver` 提供 `resolve(tenant_id, owner_id, project_id, asset_id, kind, model_id)`，只允许返回进程内 `ApprovedMedia`。它必须自行查询本站素材/访问授权、确认版本SHA、取得对这家供应商公开读取该素材的授权，并保证URL已适合该型号。不能拿用户任意URL或“知道某个URL”当所有权证据。

`ApprovedMedia` 的租户、owner、项目、asset ID、kind、model ID和SHA必须全部匹配请求；URL仅允许管理员配置的精确HTTPS输入域、无userinfo/端口/fragment。有效期默认至少再有900秒（可显式配置），是本站策略，不是供应商完成时间承诺。URL只放prepare对象内存里，repr隐藏且拒绝pickle；不会写入job、manifest或日志。调用方不能主动序列化这些对象的字段。

**当前没有生产MediaResolver实现**。供应商逐素材时长/尺寸限制、保密素材可公开读取的授权、有效期、数据出口策略仍待核实；未满足时应该拒绝授权，不能仅按计数放行。适配层不调用收费素材上传接口，也不把需要本站Cookie的素材下载地址交给上游。

## 共用任务账本，最多一次创建

必须注入 `SubmitGate`；缺少时即使 `enabled=True` 也NotReady。它是共享持久账本的接口，不是第二套SQLite，也不是进程内去重集合：

1. `consume(binding) -> bool`：原子核对租户、owner、项目、job、attempt、request SHA、lease/fence和预算预留，并在返回True之前持久化提交意图。一个attempt跨进程只能一次True；异常视为commit未知，不发POST。
2. `record_accepted(binding, task_id)`：收到成功task ID后保存到原attempt；响应或落账失败均为 `SubmissionUncertain`，不得补发POST。
3. `lookup(tag) -> SubmissionRecord | None`：从同一账本恢复原binding/task ID，拒绝查询别人的task。没有task ID的未知提交只能保留unknown；没有上游查重/列表契约，不假装能自动找回。

`SubmissionBinding` 只有身份、准确model ID和请求摘要；没有prompt、URL或key。生产实现应与现有 `TaskQueue.begin_submission/record_submitted` 协调：**提交意图只能由一处负责**，不能让当前WorkerRunner先begin，再把同一动作调第二遍。测试专用ExistingQueueGate直接使用原TaskQueue，在真实本机PG隔离schema中证实两个调用竞争只发一次POST，超时后原预算保留、不能再次generate。

prepare另用每个adapter实例独立的进程内HMAC绑定实际POST字节、binding与expiry。替换Prepared的model、prompt、媒体URL、owner或有效期会在consume之前拒绝；这不是可自行更新的普通body hash。Prepared不支持跨进程持久化，重启必须重新prepare原请求，再由同一持久gate决定是否允许提交。此封印不替代gate从权威账本核对原请求。

API创建传输的自动重试为0。只有官方明确说明不收费的402视为 `SubmissionRejected`；其他非2xx、重定向、超时、无效JSON、缺失/无效task ID都保持unknown。取消接口没有契约，`cancel()`返回False，不发取消请求。上游failed不会被写成已退款；实际费用resolver默认None，仍待账单核对。

## 结果获取与公开边界

- 轮询只返回安全Outcome，不返回原始error、URL或响应。支持queued/processing/succeeded/failed，其余状态unknown。
- fetch重新查询原task，当前仅接受恰好一个video结果；多个输出不静默丢弃。只允许 `boyesir.com`、`gf.boyesir.com`、`hub.boyesir.com` 三个精确HTTPS域，不随redirect。
- 下载独立请求，不带API Authorization、Cookie或Referer，不共享会话认证。底层直接使用httpx transport，避开Client INFO日志打印签名URL；还用contextvar对当前请求/读流/关闭期间的httpcore日志脱敏，因为DEBUG response headers可能含Location或Set-Cookie。其他线程及作用域外日志不变。测试直接调用当前依赖的真实httpcore.Trace核对；升级依赖须重新审查logger列表，不应另装会输出HTTP敏感信息的调试hook。
- 显式timeout默认每次网络等待10秒、传输总预算180秒、结果≤512MiB；拒绝压缩Content-Encoding、无效/超大Content-Length、空或截断结果。逐chunk心跳与时间/体积检查，临时文件只在可信目录，原文件不被失败下载覆盖。
- fetch只返回原视频文件路径，不伪造独立音轨。**这些字节尚未成为可发布作品**：后续collector必须真实解码、核验媒体、判断是否有音轨，并使用现有持久staging/output quota与ArtifactWriter再发布。当前H3 collector的固定尺寸/音频要求不适用，尚未集成。

URL仅在一次进程内调用存活，下载失败可重查原task再下载，不能重生成。崩溃遗留的暂存文件需要现有staging配额与恢复流程接管；本adapter不做递归清理或定时删除。

## 中央凭据与正式启用前置项

唯一路径是中央 `load_api("boyesir", profile="boyesir--boyesir-windows-dpapi")`，要求base_url精确为 `https://boyesir.com`；缺失或冲突拒绝。只在显式enable、gate存在且prepare通过请求检查后按需加载；没有.env或全局环境覆盖。测试全部注入假loader，没有加载真实key。Windows绑定DPAPI不能直接在Lightsail解密，远程凭据授权入口仍未完成。

尚未完成：生产SubmitGate/MediaResolver、API plan与预算/货币换算、未知上游费用对账、provider-default动态输出验收、独立API并发槽与限流、真实凭据/逐模式小额验收。`capabilities()`固定标 `integration_ready=false`、`online_verified=false`；不能据此把UI按钮标为可用。供应商型号不能证明上游BF16、checkpoint身份或与自托管质量等效。

## 本轮验收

`.venv\Scripts\python.exe -m unittest test_platform_boyesir_backend -q`：19项通过。先临时SQLite，再显式授权本机PG隔离schema；无供应商网络调用。覆盖关闭与缺gate、准确profile/端点、未知字段/型号/输入数量、跨owner/project/model/SHA拒绝、Prepared篡改、并发提交、未知POST和落账失败、重启查询、402/其他错误区别、任务ID回显矛盾、下载域/认证隔离/重定向/大小/慢流/心跳/硬链接、签名URL与密钥不进入repr/HTTPcore DEBUG日志。

本轮不修改根API、worker、capabilities、生产settings或中央正式目录；接入变化只向中央inbox独立提交。正式启用应另有明确的线上测试授权。
