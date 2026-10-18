# H3 / 映序：Hippius、R2 与 S3 存储接入核查

核查日期：2026-10-04（Australia/Sydney）。范围：官方公开文档、官方源码说明、中央资源元信息、本项目离线实现与临时目录测试。**未创建桶、充值、迁移资产、使用真实存储凭据或发送供应商读写请求。GPU 保持关闭。**

## 结论与当前实现

建议生产媒体的首选候选仍为 **R2 Standard**；Hippius 加入可插拔的实验存储后端。当前实际接入由主服务选择独立 `LocalObjectStore`，不因新增后端便切换现有存储。AWS 承载业务服务不要求视频也存 AWS。若计算将来固定在 AWS 同一 Region，再比较同区 S3 的性能、治理与总成本。

“可插拔”指代码适配接口，当前每个部署仍只绑定一个store；没有旧资产跨供应商双读或自动迁移。更换后端需要独立的迁移、归属/字节核验和回滚方案，不能仅修改R2配置便继续读取旧Local对象。本轮没有搬迁本机作品。

Hippius 是实际提供 S3 接口的服务，SN75 身份不构成吞吐、可用性或持久性的线上验收。价格较低值得测试，但不能用补贴或“兼容 S3”代替文件读取、私有访问和故障恢复证明。公开服务介绍说明它属于 Bittensor Subnet 75，官方代码和接口文档提供了进一步依据。[服务介绍](https://hippius.com/blog/migrate-s3-buckets-to-hippius-in-a-few-clicks)

已实现 `studio_platform/storage.py` 和 `storage_config.py`：

| 后端 | 已实现契约 | 仍未完成的验证 |
|---|---|---|
| Local | 临时文件写入、限额/哈希检查、不可覆盖发布、读取/元信息/删除 | 生产主机磁盘备份、容量告警和系统级断电恢复 |
| R2 | 显式 SigV4 客户端、条件 PUT、读取/元信息/删除、短期上传下载签名 | 真实身份、桶权限、浏览器 CORS、跨云吞吐、对象读回 |
| AWS S3 | 同上；必须提供与 Region 匹配的区域 endpoint | AWS 角色/目标桶、区域价格、真实访问与治理策略 |
| Hippius | 随机新对象写入、读取/元信息/删除、下载签名 | 整体实验状态；浏览器直传暂不启用，固定 key 条件创建明确拒绝 |

基础 `S3ObjectStore.put/write_new` 是**单次 PUT、应用硬上限 100 MiB**，不是供应商对象大小上限。新增独立 `MultipartUploadManager` 对 R2/AWS S3 实现服务端分片及持久恢复，当前应用上限 512 MiB；Hippius 不进入这条路径。`AssetService` 已按对象大小显式选择单次 PUT 或该 manager。浏览器断线的按字节续传和浏览器直签分片尚未实现。Local 的 `max_bytes` 由已鉴权业务层传入，真实格式/时长/分辨率仍由 AssetService 校验。

## Hippius 官方契约及保留意见

连接必须使用 `https://s3.hippius.com`、`region=decentralized`、AWS SigV4、path-style。应用应使用指定桶的 sub token。S3 master/sub token 与 Hub 管理 API token、Drive 的恢复种子和钱包不是同一种凭据，不能互相代用；旧仓库曾描述 seed-based 登录，本实现不支持这条路径。[当前 S3 入门契约](https://raw.githubusercontent.com/thenervelab/hippius-s3/staging/llms.txt)

控制台能创建指定桶、只读或读写、带期限的 sub token。控制台单文件上传上限 100 MB；这不等于 SDK 的通用对象上限。S3 按量余额耗尽会逐步进入只读／暂停，因此账户可访问性与余额告警属于上线条件。[控制台与权限](https://docs.hippius.com/use/console/s3)

官方高级指南说明桶默认私有、支持下载签名、展示浏览器 PUT 签名示例，支持视频 Range 播放，尚无自定义域名支持；大文件通过 multipart，页面给出约 5 TiB 的对象口径。**这属于文档支持，不是本账户实测。** 文档所述静态加密不能等同于 Drive 的客户端端到端加密。[高级用法](https://docs.hippius.com/storage/s3/advanced)

官方 compatibility 存在必须保留的差异：普通 PUT 不支持 `If-None-Match` 条件写；生命周期 PUT 可以返回成功但并不持久化；桶 CORS 管理操作未标支持。兼容表的 `GetBucketLocation` 固定返回值也不能用于推断实际地理位置。实现因此不使用 `HEAD→PUT` 假装原子防覆盖，不依赖 lifecycle 清理，不按 S3 桶策略语法自行做租户隔离。[源码兼容表](https://raw.githubusercontent.com/thenervelab/hippius-s3/staging/docs/s3-compatibility.md)

关于浏览器直传：高级指南确实给出示例，而 CORS 管理支持不明；不能据此断言默认 CORS 一定失败或成功。本轮没有对真实网关执行预检或上传。Hippius 暂只开服务端受控上传，`presigned_upload=False`、`browser_upload_verified=False`，保留后续专项验收。

版本与保留语义仍需核对：较早 `main` 兼容表未列 Versioning/Object Lock，当前官方站点和 `staging` 已有相应说明；部分高级权限说明与 staging 的 prefix policy / 删除策略描述也不一致。不能把代码分支的功能自动认定为线上版本。对象锁另有不可逆保留／持续计费影响，本轮不启用。[当前 Object Lock 文档](https://docs.hippius.com/storage/s3/object-lock)、[旧 main 表](https://raw.githubusercontent.com/thenervelab/hippius-s3/main/docs/s3-compatibility.md)、[站点兼容说明](https://docs.hippius.com/storage/s3/compatibility)

## 费用与位置

2026-10-04 官方 Hippius 定价列出按量 **$0.0060/GB·月，即页面口径 $6/TB·月**，按小时扣费，出口包含在内；套餐列出无限请求。页面顶部 Enterprise “$0.0045/GB”与套餐卡“$465/100TB，即 $4.65/TB”不一致，不能选更低的数字作当前合同价。请求计费、具体计划、生效价格与余额应在启用前以账户确认；不引用该页对其他厂商的夸张倍数比较作为本项目成本证明。[Hippius 价格](https://hippius.com/pricing)

R2 Standard 当前公开价为 $0.015/GB·月，A/B 操作分别 $4.50/$0.36 每百万次，R2 出口免费；免费额度是账户级，可能与其他项目共享。Workers、其他计量服务和外部 GPU 的网络费用仍分别核算。S3 按区域、请求和出口计费；本项目未确定 Region，未核实地区单价，不复制历史报价。[R2 定价](https://developers.cloudflare.com/r2/pricing/)、[S3 定价](https://aws.amazon.com/s3/pricing/)

R2 可给 apac/oc 等位置 hint，但不是具体城市保证。Hippius 目前查到的客户端 region 是协议字段，未找到本项目可承诺的物理区域 pinning、延迟或并发 SLA。需要从真实用户网络及目标 Lium GPU 测量。模型权重缓存继续按原中央资源位置管理，不因换对象存储而搬迁。[R2 位置契约](https://developers.cloudflare.com/r2/reference/data-location/)

## 网站与媒体的调用链

业务站点统一为 `www.sixnine.art`，H3 单次创作使用 `/freestyle`；`h3.sixnine.art` 计划重定向到该路径，避免跨站登录和项目分裂。具体 DNS 尚未配置。当前浏览器上传仍通过受限业务API，由服务器解码校验并写对象；下面的浏览器直传是后续启用方案，并非当前已完成链路。私有媒体使用持久 asset/artifact ID，按用户鉴权后生成短期访问能力：

1. 浏览器向业务 API 建立素材记录并取得上传授权；云端直传只对已验证的配置启用。
2. 浏览器/GPU 直接读写对象存储。AWS CPU 后台处理身份、任务和元信息，避免整段视频经过 AWS 再发往外网。
3. 上传 complete 后校验对象大小、内容哈希和真实媒体格式，再将素材置为 ready。`stat().sha256` 对远端只是对象元信息，不能替代实际内容验证。
4. 结果下载失败只重试收集；不要再生成一次。供应商回执/可下载 URL 不等于成片已稳定归档。
5. R2 S3 签名 URL 不能换成自有域名；如需 `media.sixnine.art`，另做 Worker 鉴权读取，正确处理 Range/206/HEAD。Hippius 自定义域名未受支持，不能仅改 Host 或 DNS。[R2 签名限制](https://developers.cloudflare.com/r2/api/s3/presigned-urls/)

用户 A/B 的隔离必须从会话与数据库 owner 关系决定，不能只看 key 前缀，更不能信上传 JSON 的 owner。所有桶保持私有；应用凭据最小权限；TLS 和静态加密必需。公开展示另用显式发布的对象/权限，不将私有桶整桶公开。任何日志、异常上报与分析系统都不得保存带签名查询参数、Authorization、上传/下载响应体中的临时 URL。[R2 加密](https://developers.cloudflare.com/r2/reference/data-security/)

## 可复用 Python 接口

```python
from studio_platform.storage import LocalObjectStore

# 服务端明确指定的新存储根；示例不运行、不指向旧 data。
store = LocalObjectStore(absolute_storage_root)
info = store.write_new(
    trusted_owner_id, asset_id, binary_stream,
    filename="source.mp4", content_type="video/mp4", max_bytes=upload_limit,
)
# 持久化 info.key/size_bytes/sha256/content_type/provider。
with store.open(info.key) as stream:
    consume(stream)
```

- `write_new(owner_id, asset_id, source, *, filename, content_type, max_bytes, expected_sha256=None)` 返回 `ObjectInfo`。`filename` 是最多 64 字符的 ASCII 安全存储名；原始 Unicode 文件名保存在业务数据库。
- `put(key, source, *, ...)` 只接受原子不覆盖创建。Hippius 明确拒绝这项语义；其 `write_new` 使用 UUID 随机物理 key，**不是服务器强制不可覆盖保证**。固定同 key 的重试需要业务账本协调。
- `open(key)` 返回需关闭的二进制流；`stat(key)` 返回对象元信息；`delete(key)` 仅用于已有明确授权的单对象删除，不自动清理资产。
- `make_object_key` / `key_belongs_to` 仅辅助构建和检查命名空间，不替代用户鉴权。key 禁止绝对路径、反斜线、点段、百分号编码、URL 查询、Windows ADS/设备名等；Local 映射为扁平哈希目录，拒绝 symlink/junction/reparse point/hardlink。
- `presign_download` / `presign_upload` 返回 `SecretURL`；只有 `reveal()` 显式取出 URL。repr/str 脱敏，普通 JSON 和 pickle 序列化拒绝；不要将 reveal 的结果存数据库。应用期限为 1–3600 秒，默认 300 秒。
- R2/S3 上传签名要求客户端发送返回的 `required_headers`，含条件写头。**签名本身不实现用户存储配额或强制文件体积上限**；初始化限额、完成校验、未采用对象处理仍由业务层负责。
- `StorageWriteUncertain.key` 是请求可能已写入后的核对目标。保留 key，先做 HEAD/哈希/实际读回，不能捕获后直接重新 `write_new`。SDK 自动重试关闭，没有跨 provider fallback。
- 本实现提供经过核对的三种 S3 provider；其他 provider 通过新的 reviewed adapter 实现 `ObjectStore` Protocol，不能给通用 endpoint 就自动当作兼容。

## 凭据与启用边界

`S3StorageConfig` 必須显式填写 provider、endpoint、region、bucket、service、profile，`enabled=False` 为默认。创建 `S3ObjectStore` 不创建桶；缺凭据不会走默认 AWS CLI 配置或 metadata credential chain。endpoint 带认证、路径、query、错误域名/region 一律拒绝。

`load_storage_credentials` 复用中央 `api_registry.load_api`，只接受明确中央根路径或注入的加载函数；核对返回的 service/profile、base_url 和 endpoint 字段。凭据只能以 `S3Credentials` 在进程中注入，不复制加载器、不读取项目 .env、不改变进程环境。

中央已有 `cloudflare-r2 / cloudflare-r2--rig-root`，资源 `provider-r2-crypto-config`，仍为 configured_unverified。R2 字段引用为 `R2_ACCESS_KEY_ID`、`R2_SECRET_ACCESS_KEY`、`R2_ENDPOINT`。本次中央查询没有 Hippius 正式资源或匹配 profile，**没有创建假的已导入 profile**。代码中的 `hippius-s3` 是适配器所需服务名，真实集中导入须由中央维护方登记并核实字段；本轮没有读取默认 AWS CLI 的凭据。

Windows DPAPI 不能直接复制到 Linux 云主机使用。生产远端身份加载、账号桶确认和预算缺口仍保留；不得从本机配置导入成功推导线上可用。

## 验收证据与后续门槛

离线命令：`.venv/Scripts/python.exe -m unittest test_platform_storage test_platform_storage_multipart test_platform_assets test_platform_api -v`。

2026-10-04：**79 项测试通过**，其中存储 27、multipart 20、资产服务 18、现有 API 回归 14。存储/资产测试禁止外部网络，HTTP回归仅允许Windows asyncio自身loopback管道；只使用独立 TemporaryDirectory、临时 SQLite 和 fake/Stubber，没有操作原 data 或真实云存储。覆盖并发发布、重启读取、安全路径、容量与哈希、两用户/项目隔离、provider/profile 冲突、丢失创建/分片/完成/PUT 响应、进程中断、字节读回、SDK 参数形状、未知请求不重发和常见错误脱敏。boto3/botocore 1.43.108。真实 R2/S3/Hippius 在线读写、CORS、浏览器直传、真实吞吐及多机 PostgreSQL 压力测试均未执行。

启用任一远端之前，用非敏感测试素材执行：私有读取与跨账号拒绝、CORS 预检与上传、Range/拖动播放、上传中断/超限、过期签名刷新、相同 key 不覆盖、未知 PUT 核对、读回校验、持久性观察、当前费用/余额告警。Hippius 另测网关接收与底层发布之间的 pending 状态；不能仅靠 PUT 的 200 返回宣布长期持久化成功。

## 分片与恢复的实现边界

新增 `studio_platform/storage_multipart.py` 使用 SQLAlchemy 持久 `storage_multipart_uploads` 表，绑定 tenant、owner、endpoint、bucket、service/profile 和随机物理 key。同一 request_key 不同输入拒绝；账本先存操作意图再请求供应商，乐观版本更新防止两个进程同时提交同一意图。

`MultipartUploadManager(store, MultipartJournal(engine, tenant), max_object_bytes=512MiB, part_size=32MiB)`：
- `begin(owner, asset, request_key, *, size_bytes, sha256, filename, content_type)` 返回会话公共状态；`upload_part(owner, session_id, number, stream)` 要求精确分片大小，重用分片编号只接受同样字节。
- `get/reconcile/abort` 返回无 key/upload_id 的公共状态，`complete` 返回 `ObjectInfo`。分片状态、大小、SHA256、ETag 和 provider upload ID 存在内部表；没有凭据、签名 URL 或原始 SDK 错误。
- 完成不是看到 HTTP 200 即结束：SDK 处理可能嵌入的错误，再 HEAD 和完整读回，对整件文件做长度/SHA256 校验。完成响应丢失只核对现有对象，不重新 complete。下载核对失败也不重跑生成。
- 进程中断留下 creating/part_uploading/completing 状态时，默认拒绝新操作。只有运维确认旧 worker 已停止或被隔离，才使用 `reconcile(..., interrupted=True)`；不能把超时当成原请求不再运行，也不能将此参数开放给用户。
- 创建结果不明且找不到唯一匹配 upload ID，或完成结果不明且对象尚不存在时，保留未决状态与预留，不盲目新建上传。仍需运营核对和明确的过期/人工处置流程，当前没有自动扫桶或自动清理。
- AWS complete 发送 `If-None-Match: *`。R2 文档没有同等明确的条件 complete 契约，故只使用随机新 key，能力表将 conditional_complete 标为 false，不能宣称服务器强制不可覆盖。Hippius multipart 虽有文档，但当前实现明确拒绝，未据名字推定支持。

R2 要求普通分片至少 5 MiB、最后一片可更小，并支持上传中断后继续；默认未完成 multipart 在七天后自动终止是供应商默认生命周期行为，**本项目没有配置或验证该规则**。分片/完整 multipart 的 ETag 不是整件 SHA256。AWS 条件 complete 在 409 情况下有重新创建上传要求；当前代码保守停留待核对，不自动照做以免把未知结果误判成失败。[R2 分片规则](https://developers.cloudflare.com/r2/objects/upload-objects/)、[S3 条件写](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html)、[S3 CompleteMultipartUpload](https://docs.aws.amazon.com/cli/latest/reference/s3api/complete-multipart-upload.html)

当前版本是服务器分片转发与重启恢复。它仍会让素材经过业务服务器，若该服务器在 AWS，就有外发成本；上线的大流量路径仍建议后续完成经过鉴权的浏览器/GPU直传 R2，且核对 CORS、分片精确长度、配额和最终哈希后再启用。

## AssetService 接入与无损升级

资产服务新增 `platform_asset_upload_receipts` 与 `platform_asset_storage_quota` 两表；没有改写或重建原 `platform_assets`。现有已知原件/模型对象大小在首次创建配额行时计入；缺失文件、历史孤儿对象或操作系统外部写入不因没有元信息就算“无人使用”。旧素材保持可读取；缺少旧上传回执的素材不伪称可以恢复。

- 原件与规范化副本分别记录 role、key、size、SHA256、提交阶段和 multipart session。先持久记 key，再执行条件 PUT；`StorageWriteUncertain` 保留核对目标。任何未知操作都不直接调用新的 write_new。
- 原始完整文件及 CPU 规范化文件保留在明确 data_dir 下 `asset-staging/<asset UUID>`，账本只保留 UUID/安全文件名，不将系统路径返回浏览器。不会在一次异常后用 TemporaryDirectory 删除唯一收到的原件。
- 相同 owner/project/client_asset_id 且同内容指纹复用已有资产；改变内容、MIME、来源或选段时返回冲突，不返回错误的旧素材。仅本次新收到、已确认是精确重复的暂存副本可由当前请求移除；不会删除旧素材或存储对象。
- client_asset_id 是1–128字符的ASCII业务标识，允许前端 `entity_id:file_id` 的冒号；它只进入参数化数据库查询，不参与磁盘路径或物理key拼接。无效/过长/路径式ID返回422；对象key仍使用独立严格校验。已回归真实 `result-<UUID>:cloud_artifact_<UUID>` 上传及重试。
- `resume(owner, asset_id)` 可从完整源文件继续 CPU准备或原件/模型上传；`reconcile(owner, asset_id, interrupted=True)` 只供确认旧处理者已退出后的运维恢复。API 还应先校验项目 assets:write 权限。中途断线且原文件尚未完整接收，需客户端重新选择文件，不能声称支持任意字节断点续传。
- 默认应用策略是每用户 10 GiB、租户 40 GiB、每用户同时处理 2 件、租户 4 件。新操作按四倍单文件上限预留（原件/模型各一份暂存和一份对象），已结束操作按已知保留文件/对象结算；未决写入保留预留。重复重试不会重复对象或累计配额，余额和并发计数跨进程重启保留。
- 这是**逻辑容量与服务内并发控制**，不是文件系统硬限制。Starlette 解析请求体的临时文件、CPU编码瞬间峰值、外部进程写入和元信息未知的历史对象需另有磁盘/容器配额及监控。暂存副本不会自动到期删除；应在备份和资产归档策略明确后独立做 housekeeping。

读回校验会产生实际读取流量。当前没有在线调用或计费验证；实际启用 R2/S3 仍需经过配置、私有桶和凭据权限验收。默认 Local 及 GPU 关闭状态不变。
