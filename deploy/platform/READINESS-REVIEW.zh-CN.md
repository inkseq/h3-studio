# 生产发布、私有存储与灾难恢复就绪复核

复核时间：2026-10-04（Australia/Sydney；本轮测试截至 2026-10-03 22:07 UTC）。范围是当前 `studio_platform`、本目录部署包、受限发布器、存储及备份恢复代码。未读取生产秘密、访问存储账户、创建云资源、改 DNS 或执行真实供应商生成。本报告是代码与本机隔离验证，不是公网验收或完整渗透测试。

**结论：可继续做受控部署准备，尚不能宣布 www.sixnine.art 已上线或生产恢复已就绪。** 新生产包可以运行密码保护的 CPU 网站，默认 Local 存储；GPU、付费 API、云创建和 CPU renderer 都关闭。Linux 秘密来源、目标主机与公网交付、离机备份仍有明确缺口。

补充实际证据（22:27 UTC）：已用独立本地 Docker 完成内部 CA 的真实 HTTPS → Caddy → password app → PostgreSQL → Local 链路，上传/Range/跨用户/真实容器重启/XFF伪造均通过，所有测试资源已清理。详见 [STACK-VALIDATION.zh-CN.md](STACK-VALIDATION.zh-CN.md)。这补齐本机网络栈验证，仍不将内部 TLS 或随机 fixture 当成公网/正式秘密验收。

## 已有实现与实际证据

| 边界 | 当前实现及本轮证据 | 不能由此推导的结论 |
|---|---|---|
| 公开网站认证 | Compose 强制 `password` 和精确 HTTPS origin；Settings 拒绝公网 `local-test`；登录 Cookie 为 Secure、HttpOnly、SameSite=Lax。应用健康检查要求两个正式账户就绪。`bootstrap.py` 只从管理员交互终端设置密码，不放入参数或环境 | 尚未在公网完成 superdan/supervan 登录、注销、过期、隔离及下载验收；本地假账户不是正式账户 |
| 网络及权限 | 只有 Caddy 发布 80/443；app/DB 内部网络、无 app 出口；DB 未公开。app 为 UID 10001、只读根、固定两核/内存边界；只信固定 Caddy 地址的代理头 | 未核验真实主机端口、子网冲突、DNS、TLS、剩余磁盘及重启行为 |
| 发布来源 | 同一测试镜像保存为 bundle，哈希/归档/镜像 ID 核对；独立 root 批准 manifest；只调用已安装的 root 发布器，不 sudo incoming 脚本。bootstrap 与发布权限分离 | 本机检查不替代实际 GitHub environment、分支规则、固定出口 runner、SSH 主机指纹、sudoers 与批准流程 |
| 失败发布 | 发布状态持久化，丢失响应能核对；失败回到已批准的旧应用/代理镜像；依赖镜像变化拒绝自动发布 | 不是数据库降级、数据回滚或零停机保证；旧镜像必须兼容已迁移 schema |
| Local 媒体 | 受控对象 key、不可覆盖发布、实际大小/SHA 校验、拒绝链接/穿越；API 从会话及业务 owner/project 关系授权 | 不是跨机冗余、系统磁盘硬配额、自动过期清理或离机备份 |
| 对象上传恢复 | R2/S3 大于 100 MiB 走持久 multipart；固定对象 key、上传状态、配额与内容证据可重启核对。未知 PUT/complete 不盲目重新写另一份 | fake/SDK Stubber 验证不证明任一线上账户、桶、CORS 或供应商故障语义 |
| 完整便携备份 | SQLite 一致快照；PG 一个 REPEATABLE READ/READ ONLY 事务导出业务，再复制对应不可变 Local 媒体并校验。真实隔离 PG 测试覆盖并发写入时一致性、未知费用和认证排除 | 当前完整恢复目标是全新 SQLite + Local 隔离目录；不等于恢复到生产 PG 或 PITR |
| 恢复隔离 | 不导出账户密码/会话/机器 token 表；保留作品 owner。未终结 job 置 recovery_hold，旧执行计划禁用/过期，worker/租约失效，扩容审批禁用，waiter hold，容量上限为 0 | 认证需重新配置；上游任务/实例可能继续运行和计费，恢复本地数据库不意味着云端已停止 |

## 三种存储不能互换理解

| 选择 | 当前状态 | 启用前具体缺口 |
|---|---|---|
| Local | 生产包唯一选择；媒体在 `/srv/sixnine/platform-data`，HTTP 临时上传在独立 `/srv/sixnine/upload-spool`，数据库在 `/srv/sixnine/postgres` | 准备正确目录 owner/权限；磁盘容量告警；批准加密离机目标及备份周期；在新目标演练恢复。逻辑配额不是整个文件系统硬限额 |
| R2 | 首选远端候选；中央 `cloudflare-r2 / cloudflare-r2--rig-root`、资源 `provider-r2-crypto-config` 仅 configured_unverified。适配代码已离线验证 | Linux 运行身份/中央授权入口、私有桶与当前权限、受控出口、实际对象读回、签名到期与 Range、故障恢复及费用核验；没有完成浏览器 CORS/直传验收；已有Local资产还需独立迁移/双读及字节核验，不能只改环境变量在线无缝切换 |
| Hippius | 实验适配器；尚无已登记匹配 profile。固定 key 条件 PUT 明确拒绝，随机 `write_new` 不宣称服务器强制防覆盖；AssetService 所需保证不满足，因此不作为可用生产选择 | 正式身份/桶、运行版本与官方文档差异、条件创建替代协议、multipart/CORS/持久性专项验收。失败不会悄悄切 R2 |
| AWS S3 | 独立显式区域 endpoint/provider 的适配器，非当前启用后端 | 目标身份/桶/区域、网络与权限、线上恢复与成本验收；不能沿用当前机器默认 AWS profile 猜账户 |

R2 的 S3 签名 URL 必须保留已签名 API host，不能换为 `media.sixnine.art`。如以后需要自定义媒体域名，必须另做鉴权/Range 路径。浏览器当前先上传 CPU API 并在服务端归一化，不能把“已经有 presign 方法”写成“大文件已绕过 AWS 中转”；只换存储后端仍可能产生 CPU 主机出站流量。相关依据和当时官方链接见 [HIPPIUS-STORAGE-REVIEW.zh-CN.md](../../docs/HIPPIUS-STORAGE-REVIEW.zh-CN.md)。

## 当前需要完成的部署条件

1. **正式运行秘密来源与重启顺序。** Windows DPAPI 不能复制到 Lightsail。`/run/sixnine-secrets` 只是 tmpfs 文件契约，不是加密库。需由中央维护者批准 Linux 的身份/加密来源并保证重启先供给文件再启动服务。R2 短期凭据刷新尚未实现。DB 管理员/应用密码为不同秘密，角色和原 DSN 端点必须匹配；不能用项目 `.env` 或另一个默认账户填缺口。
2. **真实主机和交付权限。** 当前只有 Lightsail 模板与结构验证，未创建主机。需确认区域、规格、预算、已登记 SSH 公钥对及固定管理员 IPv4；预装受控 Docker/Compose/控制器，检查数据目录权限、网段/端口及镜像 digest。GitHub 动态 runner 不能直接穿过固定 `/32`，要先落实固定出口或经审核的传输通道；不能把 SSH 开到全网。
3. **备份责任和恢复目标。** 确认离机加密位置、频率、容量告警及保留政策，执行实际新目标演练。现有 portable 可以保全业务和已发布 Local 媒体；生产 PG 恢复流程、PITR、远端对象备份、定时任务与告警尚未完成。上传/收集暂存、孤儿对象不在完整 portable 包内，不能承诺恢复未完成上传。没有删除或保留期授权。
4. **公网验收。** 管理员交互建立两个正式账户后，再完成 DNS/TLS 与外网测试：匿名不可读作品，A 不可访问 B 的项目/素材/任务/下载，错误 Origin 被拒，受保护下载支持 Range，重启后账户/作品仍可读，丢失 `/run` 文件时明确失败。当前没有完成这个真实站点闭环。
5. **付费与渲染单独启用。** 当前 Compose 不附带 worker，`generation=0`、`render=0`、`cloud_creation=0`、execution backend disabled。CPU 字幕/粗剪本地已经实现，不意味着两核生产控制主机已通过并发渲染容量测试。GPU 池、Boyesir transport、自动扩容和远端存储要分别配置及授权验收，不能随 UI 上线隐式启用。

登录源地址须作为公网验收的一项：当前 Compose 明确 `--proxy-headers --forwarded-allow-ips 172.29.69.2`，Caddy 固定该内部地址并以真实直连来源覆写 X-Forwarded-For；不信任任意来源的头。因此不能简单认为所有用户必然共享 Caddy IP 的登录限额，也不能仅凭配置宣布真实转发已通过。验收需用不同客户源和伪造头确认地址边界；同家庭/NAT的用户仍会共享源IP限额。若后续加入 Cloudflare 代理，需另行修改并验证可信代理链，不能将允许范围设为 `*`。

## 发布/恢复后的付费任务处理

- 正常应用发布只更换已批准应用/代理；不删除数据库、媒体、预算或上游 attempt。当前生产配置没有生成 worker，也没有供应商凭据和 app 网络出口。
- 灾难恢复保留原 task ID、attempt、实例 ID、费用预留及创建未知状态。它们是核对依据；不能因为新主机没有 worker 就释放预算或自动补生成。
- 所有未终结任务一律 `recovery_hold`，包括 waiting_capacity。恢复的容量审批 `enabled=0`、waiter 为 hold；原 intent/hash/deadline 保留。旧队列/设备租约不能直接继续执行。
- 操作者需要先核对上游是否仍运行、结果能否收集、实际费用与实例状态，再制定逐项恢复动作。普通取消请求不能把 hold 变成可重新投递状态。重新建密码不会自动解除 hold。
- 恢复到新Local根后，旧上传/输出收据、active与存储预留仍绑定原根，可能阻止新上传。当前管理CLI只恢复同位置收据，不能跨根核销；重新开放写入需独立核对未决资产与迁移，不清零绕过。退役时先冻结全部写入，再制作最后备份并另存唯一staging/工作目录；在线快照不包含后续提交。
- 本地恢复不停止云实例；Lightsail 模板 Retain 同样意味着删除 stack 不会自动停止实例/静态 IP 计费。没有把“没有运行本地 worker”作为停费证据。

## 本轮修复与回归

发现并修复 `backup._postgres_environment` 的实际 TLS 边界：旧代码允许任意远端 DSN 显式 `?sslmode=disable`，与安全连接约束不一致。现仅 literal loopback 或 `postgres-local --private-platform-network` 的精确已审阅 Compose 身份允许 plaintext；其他远端必须 verify-full。测试仅写合成 DSN 到临时受保护文件，不建立连接，不输出其值。

本轮复跑 `test_platform_backup`、`test_platform_backup_postgres`、`test_platform_deployment`、`test_platform_preflight`、`test_platform_bundle`、`test_platform_bootstrap`、`test_platform_release`：59 项，57 通过、2 项可选集成跳过。其中 4 项 portable 备份测试实际连接已有专用本机 PostgreSQL 测试库，以唯一 schema 隔离；两项跳过是本轮未再次开启的原生 pg_dump 演练/额外临时 DB 容器集成，不是失败。

此前真实 pg_dump/pg_restore 到独立测试库的演练已有记录，见 [BACKUP-RECOVERY.zh-CN.md](../../docs/BACKUP-RECOVERY.zh-CN.md)；本轮没有把其历史成功冒充新生产验证。主机假元信息/Compose 配置/归档检查和代码审阅不需要任何真实云身份。

存储/重启故障及恢复隔离组合复跑 `test_platform_storage`、`test_platform_storage_multipart`、`test_platform_artifact_writer`、`test_platform_reliability`：78 项，77 通过、1 项跳过（仅适用 SQLite 的实际 quarantine 测试，在本轮 PG 模式明确跳过；基础备份回归已覆盖 SQLite 恢复）。S3 供应商为 fake/Stubber，只有任务账本连接本机隔离 PG。

该组合起初暴露一个测试顺序不确定问题：旧任务和新任务使用同一固定时间，随机 UUID 会决定先检查哪个候选，导致在旧任务尚未被检查时就断言它已 hold。队列负责人已把 fixture 的先后时间明确，并增加再次 claim 不会提交旧任务的断言；没有为此改变队列算法。修正后上述组合通过，原 attempt/task ID、预算预留和 hold 保持。未发现新的已复现付费重投漏洞；生产开启仍受上述实际部署条件约束。

## 操作入口

- [生产包说明](README.zh-CN.md)、[受限发布与回滚](RELEASE.zh-CN.md)、[只读主机预检](HOST-PREFLIGHT.zh-CN.md)
- [主机模板与费用观察](INFRASTRUCTURE.zh-CN.md)、[Linux 中央秘密候选设计，未启用](RUNTIME-SECRETS.zh-CN.md)
- [备份和恢复的准确范围](../../docs/BACKUP-RECOVERY.zh-CN.md)、[存储兼容性与未验收项](../../docs/HIPPIUS-STORAGE-REVIEW.zh-CN.md)

本报告不构成修改 DNS、租用主机、导入新凭据、付费测试、迁移对象或清理旧文件的授权。
