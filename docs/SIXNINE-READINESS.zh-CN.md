# sixnine.art 上线就绪核查与存储选择

后续实现更新（2026-10-04）：统一业务API、持久队列、worker/fleet、存储适配、映序云项目及CPU粗剪已有实现与本地测试，当前状态见 `ARCHITECTURE.zh-CN.md`。以下保留初始只读核查时点，表中的“待实现”和直传方案不能作为最新运行说明；公网/DNS/真实GPU仍未上线。

日期：2026-10-04，Australia/Sydney。此次是只读账户/域名核查及设计更新，**没有部署、修改DNS、创建角色/桶/服务器或启动GPU**。

## 已确认与未完成

| 项目 | 核查结果 |
|---|---|
| H3应用 | 单机网页、账户隔离、真实生成历史、下载及CPU Docker包装已存在；不是分布式部署 |
| 云架构 | PostgreSQL持久队列、独立worker、自动扩容、对象存储适配、映序真实API桥接仍待实现 |
| AWS MCP | `STS.GetCallerIdentity`真实成功；连接为root身份，不能把它作为应用/CI运行身份；未输出或复制凭据 |
| AWS CLI | 用户说明已安装；本轮用官方MCP验证账户，没有运行CLI或修改其既有profile/endpoint；不以MCP成功证明CLI每个profile都正确 |
| Namecheap | 在用户已登录Chrome中查到sixnine.art为Active、Namecheap BasicDNS；只读查看，保留用户原标签页编辑状态 |
| R2配置 | 中央`cloudflare-r2/cloudflare-r2--rig-root`可在当前进程加载，配置的endpoint主机符合Cloudflare R2官方域名，Access/Secret字段存在；未打印值；未做在线鉴权/桶/读写验证 |
| GPU | 沿用已有销毁记录；本轮未启动，不以网站上线隐含恢复GPU |

## 存储决定：建议R2 Standard存媒体

网页/API与数据库位于AWS，媒体在Cloudflare R2。主要理由是Lium等外部GPU要反复取参考素材，用户会播放和下载成片，R2不收出口流量费。2026-10-04查到Standard存储$0.015/GB·月，另有请求费用；不能称整套存储免费。AWS S3同区传输有优势，若将来大部分GPU固定在AWS同一Region，再按实测成本/吞吐决定是否使用S3后端。[R2定价](https://developers.cloudflare.com/r2/pricing/)、[S3传输规则](https://aws.amazon.com/s3/faqs/)、[S3定价](https://aws.amazon.com/s3/pricing/)

浏览器直接上传R2；GPU直接读取与上传结果；用户直接下载。AWS后台只鉴权、记录asset/job、签发短期权限，避免完整视频经过AWS代理。需要CPU处理/转码的媒体另记计算与传输成本，不承诺所有传输都零收费。数据库保存对象ID/key/版本/元信息，不保存将过期的签名URL作为作品地址。

素材和成片默认私有；HTTPS、静态加密、任务级访问、上传完成校验、每用户配额和不可变对象key。浏览器CORS仅允许实际应用域。上线需专用桶及范围受限的权限，不能默认复用其他项目的桶或对中央R2 profile扩大权限。重要成片的独立备份和恢复仍需落地。

R2有S3兼容接口，但不是AWS S3完整替身：签名PUT/GET、multipart、Range读取逐项测试；HTML表单presigned POST不能直接照搬，SSE-KMS/ACL/versioning等要按兼容表处理。保留显式endpoint与profile，禁止SDK因漏配置自动改连AWS。[R2兼容表](https://developers.cloudflare.com/r2/api/s3/api/)

私有素材初期使用R2 S3 API域名的短期签名URL。不能把其hostname改成`media.sixnine.art`；自有媒体域名以后需要独立鉴权网关（例如Worker＋私有R2 binding），支持HEAD/Range/206并计入Worker费用，不公开私有桶。[R2签名限制](https://developers.cloudflare.com/r2/api/s3/presigned-urls/)

R2使用不要求本轮迁走Namecheap DNS。若后续需要Cloudflare代理或Worker自定义域名，再核对zone和DNS托管条件后单独安排；不为存储选择盲目更换nameserver。

## DNS现状与拟议布局

Namecheap Advanced DNS实际可见：

| 当前类型 | Host | 当前值 | TTL |
|---|---|---|---|
| CNAME | www | parkingpage.namecheap.com. | 30分钟 |
| URL Redirect | @ | http://www.sixnine.art/，Unmasked | 页面未显示 |
| TXT | @ | SPF使用Namecheap转发服务 | Automatic，页面标记锁定 |

未导出注册人地址、电话或邮箱；未点击Save/Remove。页面仅核查与上线相关的DNS，不是整账户配置审计。

拟议入口：

- `https://www.sixnine.art`：映序主站，遵循用户指定地址。
- `https://sixnine.art`：由有证书的HTTPS入口重定向到www；不能仅靠现有HTTP URL redirect宣称HTTPS已配置。
- `https://h3.sixnine.art`：独立H3工具、现有同源`/api/*`和API文档。
- 映序`/api/video/*`：未来由映序后端转发统一生成服务，浏览器不携带服务商key/M2M主凭据，不直接假设跨域Cookie可以共用。
- 自有媒体域名暂不需要创建。

服务器目标IP/入口尚未确定，所以不写猜测的A记录。部署后核查冲突记录、准备回滚，再替换www停放记录并配置apex/h3；保留邮件相关记录。证书、redirect、登录、两账户隔离、重启恢复全部验收后才称上线。

## 真正上线的剩余工作

1. AWS运行角色与部署身份：当前MCP为root，仅完成核查；应用用实例角色/受限秘密引用，CI用仓库受限OIDC。中央DPAPI库不复制到Linux，远端加载入口按中央规则实现。
2. 明确AWS区域、实例规格与CPU/存储预算，准备可审查部署配置；没有因此获GPU自动租赁授权。
3. 现有H3 Compose仅单服务，Caddy仅一个H3域名；补映序构建/静态目录、主站后端及域名路由。不能把原型静态发布称为多人云端制作服务。现有发布脚本会停Caddy与app；共用代理后不能每次H3发布都停主站，需拆代理生命周期或明确联合维护窗口，避免H3健康依赖阻断映序入口。
4. 映序作品在原浏览器origin的localStorage/IndexedDB。新域名看不到旧数据，迁移前通过包含素材的`.yingxu.zip`导出/导入验收；JSON导出不等于已备份媒体。
5. 先实现对象存储与持久任务，再接单镜生成；之后两5090并发与限额自动扩容按主计划验收。公网CPU预览可以先于GPU生成开放，但界面必须显示真实能力状态。
6. R2本轮只完成配置存在/主机类型检查；上线前补选桶、线上鉴权、小文件读写、Range、分片恢复与跨用户隔离验证，禁止用历史profile加载成功冒充这些测试。

监控应记录访问主体、对象ID、响应、延迟、费用及错误，不记录认证头或签名查询串。若采用S3，增加Block Public Access、最小IAM、加密、访问日志/CloudTrail data events/CloudWatch，并将日志费用列入预算。基础设施可用性不是备份；数据库与媒体需有独立恢复演练。

关联文档：`LAUNCH-SCALE-NOVEL-PLAN.zh-CN.md`、`VIDEO-API-V1-DRAFT.zh-CN.md`。这是实施准备记录，不是上线成功收据。
