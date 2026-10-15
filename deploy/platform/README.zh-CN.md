# 映序 / Inkseq：单台 CPU 部署包

这是离线部署产物。**尚未购买实例、分配公网 IP、修改 DNS、签发公网证书或配置正式秘密来源。** 默认本地私有素材存储，GPU生成、CPU粗剪、外部API和云创建均关闭；没有复制旧 H3 的媒体、数据库或中央凭据。旧 `deploy/` 主配置不变，本目录用于新的 `Dockerfile.platform`。

## 拓扑与取舍

| 服务 | 网络 | 数据/身份 | 对外端口 |
|---|---|---|---|
| Caddy | edge + 私有 web | 独立证书状态卷；无数据库秘密 | 80、443/TCP、443/UDP |
| app | 私有 web + database，均无公网出口 | UID/GID 10001，非管理员DB身份，独立 `/data` | 无 |
| db | 仅私有 database | PostgreSQL 17，持久 PGDATA；仅SCRAM主机认证 | 无 |
| db-init | 仅私有 database | 一次性DB管理员身份，建立/核对受限 app role | 无 |

产品主入口为 `https://inkseq.com`，`www.inkseq.com` 308 到主域名。域名迁移阶段 A 同时服务新主域名和旧 `www.sixnine.art`，旧 apex 仍 308 到旧 www；新域名 HTTPS、登录和原有 PAT/Agent 验收后，受保护 `site.env` 中的 `INKSEQ_LEGACY_REDIRECT=1` 才让旧 apex/www 308 到新域名并保留路径、查询和 POST 方法。详细切换和配对回滚见 [域名迁移契约](DOMAIN-MIGRATION.zh-CN.md)。后端优先使用 `PUBLIC_ORIGIN`，旧 `SIXNINE_PUBLIC_ORIGIN` 仅作冲突检查的兼容别名；不改变租户、账户、Key 或媒体标识。

已核对的主机模板起点是 2 vCPU、8 GiB RAM，见 [主机规格与费用依据](INFRASTRUCTURE.zh-CN.md)；这不是已租用配置或吞吐承诺。app CPU 配额固定2核，兼容这个起点；其内存上限3 GiB、数据库1 GiB、Caddy256 MiB。Compose显式固定 `SIXNINE_RENDER_ENABLED=0`，配置检查和健康检查都会拒绝意外开启；CPU粗剪需要先部署独立renderer、核定容量与配额，再单独评审启用，不能从主机总配额推断它已开启。HTTP上传暂存使用独立磁盘目录，避免把多个512 MiB文件全部放RAM；必须另外监控磁盘容量。单机不具备跨主机高可用，PG和媒体需要加密的离机备份与恢复演练。

app 只信任私有网中 Caddy 的 `172.29.69.2`，Caddy 覆写 `X-Forwarded-For` 为直接连接者的地址，不接受互联网传来的身份头。web动态地址池为 `172.29.69.8/29`，让app及一次性管理容器避开Caddy的固定地址。上线前检查 `172.29.69.0/28` 与宿主机/VPN/Docker网络无冲突；如更换，要同步改 Compose 中web子网/地址池、Caddy地址、Uvicorn允许地址及验证规则。当前前提是DNS直接指向Caddy；若日后加Cloudflare代理/CDN，应重新审查可信代理范围，不能直接信任所有XFF。[Caddy代理头规则](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)

## 秘密与数据库权限

非秘密配置只填 `site.env.example` 中的镜像引用和文件路径。正式参数文件可以叫 `site.env`，**里面不得出现密码、Token或完整DSN**；该文件只给 Compose 插值，应用没有挂载或读取dotenv。

正式加密秘密来源尚未配置。需要一个另行审阅、可在Linux使用的机制，从已批准加密来源把下列文件提供在宿主 `/run/sixnine-secrets` 的tmpfs中；不能把Windows DPAPI库或API Key复制到普通JSON/.env。宿主重启后必须先恢复这些运行时文件，再启动服务。Compose secrets只做按服务挂载，不等于加密秘密管理系统。[Docker Compose secrets](https://docs.docker.com/compose/how-tos/use-secrets/)

| 现有运行时文件 | 建议宿主权限 | 可读服务 | 内容契约 |
|---|---|---|---|
| `/run/sixnine-secrets/db_admin_password` | root:root 0400 | db、db-init | 已批准的32–256字符、UTF-8单行管理员密码 |
| `/run/sixnine-secrets/app_database_url` | root:10001 0440 | app、db-init | `postgresql+psycopg`完整DSN，user=`sixnine_app`、host=`db`、port=5432、database=`sixnine`；密码32–256字符且按URL规则编码 |

两个密码必须独立，由正式秘密机制产生和保管。本包没有生成真实密码，也没有给出可复制当生产密码的默认值。源目录由root控制，应用只获得受控group-read，不能改写文件；Docker Compose 的文件型 secret 权限依赖实际宿主文件，不能只依赖 YAML uid/mode 声明。DSN只在进程内读取，设置入口为 `SIXNINE_DATABASE_URL_FILE`；同时存在 `SIXNINE_DATABASE_URL` 会被拒绝。

`init_database.py` 只连接私网 `db`。它验证PostgreSQL17，使用advisory lock串行初始化；创建 `sixnine_app` 为 LOGIN、NOSUPERUSER、NOCREATEDB、NOCREATEROLE、NOREPLICATION、NOBYPASSRLS、连接上限32，并让它拥有单一 `sixnine` 数据库以执行本项目DDL。旧role如果有更高权限、继承其他role、数据库owner不同或密码不匹配，初始化失败，**不会自动修改旧身份或轮换密码**。SQL/driver错误不打印密码或DSN。

DB管理员密码不交给app。普通app只能管理它自己的数据库对象，没有Docker socket、宿主AWS配置、中央库、GPU设备或云网络。PG启动需要少量能力初始化数据权限；app则清空Linux capabilities、只读根文件系统和no-new-privileges。官方Postgres的 `_FILE` 入口只在初始化/运行进程内使用密码，Compose配置不含值；修改文件不会自动修改既有DB密码，轮换需单独协调。[Postgres官方镜像说明](https://github.com/docker-library/docs/blob/master/postgres/README.md)

## 上线前准备（尚未执行）

1. 确认 AWS 非root日常身份、Lightsail实例预算、区域、静态IP、防火墙及备份责任。公网仅开80/443；SSH仅管理者已知IP，不能开放5432/8845。先核实当前域名和网络，不在未确认状态下覆盖DNS。
2. 目标Linux预装已审阅Docker/Compose。准备明确的新路径：`/srv/sixnine/platform-data`、`/srv/sixnine/upload-spool`归10001:10001且0700；`/srv/sixnine/postgres`给Postgres17专用，归已核验镜像的 postgres UID/GID（官方17 Alpine为70:70，Debian为999:999），权限0700，不能指向旧H3或别的PG目录。这里PGDATA是该挂载根的 `pgdata/` 子目录；官方入口只修正子目录owner，父目录若是root:root0700会导致降权后无法访问。Compose不自动创建缺失宿主目录，也不替管理员更改已有数据owner。
3. 对Postgres和Caddy确认发行版/安全更新，再锁定确切digest。平台镜像必须是CI已测试的同一镜像，使用40位commit tag或digest，不现场重建成另一份；检查 `Dockerfile.platform` 的新代码和静态前端已经包含在镜像中。本包没有自动pull或注册表登录。
4. 由批准的秘密机制准备受保护 `/run` 文件，核对角色/数据库/端点和文件权限。确认磁盘配额、加密备份位置与保留策略。未解决正式秘密来源和备份，就不宣称生产已就绪。
5. 将本目录的部署文件和仅含非秘密引用的 `site.env` 放在同一release目录。以下命令只说明未来人工部署步骤，本轮没有执行。

```sh
compose_version="$(/usr/bin/docker compose version --short)"
/usr/bin/docker compose --env-file site.env -f compose.yaml config --format json | python3 check_config.py --compose-version "$compose_version"
docker compose --env-file site.env -f compose.yaml up -d db
docker compose --env-file site.env -f compose.yaml run --rm db-init
docker compose --env-file site.env -f compose.yaml run --rm --no-deps app python -m studio_platform.manage init-db
docker compose --env-file site.env -f compose.yaml run --rm --no-deps app python -m studio_platform.manage set-password --user superdan
docker compose --env-file site.env -f compose.yaml run --rm --no-deps app python -m studio_platform.manage set-password --user supervan
docker compose --env-file site.env -f compose.yaml run --rm --no-deps app python -m studio_platform.manage status
docker compose --env-file site.env -f compose.yaml up -d app
```

用户密码通过交互终端隐藏输入，不作为参数、环境变量或配置文件。新平台管理命令是 `studio_platform.manage`；旧 `tools/manage_users.py` 管的是旧SQLite服务，不能拿来初始化这套Postgres账户。app的健康检查同时要求DB可访问、两个正式账户配置完成、生成/云创建关闭。因此没配置账户时“不健康”是预期阻挡，不是改成免密码继续上线的理由。

账号/备份/镜像核对完成后，再经授权将 www/apex/h3 的DNS指向目标静态IP。不要残留错误AAAA记录。仅在确认DNS和80/443可达之后执行 `docker compose --env-file site.env -f compose.yaml up -d caddy`，由Caddy申请证书。验证www登录、两用户隔离、素材/Range下载、章节/项目保存，以及apex/h3重定向；这一步会联网申请证书，本轮未做。

## 更新、备份与停机

发布前保存一致的Postgres备份、资产清单及对应私有媒体/暂存文件，验证可还原。运行中的PGDATA不能简单复制当可靠备份。Caddy卷含证书私钥，备份同样需要加密。不要将凭据文件、数据库备份或媒体打进镜像、GitHub产物或普通日志。

更新只替换明确测试过的平台镜像引用，并检查新旧schema兼容性；两张素材回执/配额表是新增，不删除旧asset表。不要通过 `down -v`、删除 `/srv/sixnine` 或更换Compose project name来“修复”状态。恢复旧镜像不等于能回滚数据库；恢复备份可能丢失之后写入，需单独决策。

此部署的Local存储先让私有网页可工作。R2已有中央配置但Linux凭据来源和在线契约未验收；应用当前无出口，所以不会伪装R2已启用。后续接R2需要显式增加受控网络出口、受保护凭据入口、私有桶/CORS/上传完成核验及预算检查。GPU、供应商API及自动扩容继续按独立部署阶段处理。

当前API每个部署只有一个store，旧Local对象不能只靠切换R2环境变量继续读取。已有资产须另行设计迁移或双读、逐对象归属/大小/哈希核验与回滚，再明确切换；本轮没有实现在线无缝换存储，也没有移动本机作品。

### 受限发布入口

自动发布使用管理员预先审核并安装的 `/opt/sixnine-release/release.py` 和同目录配置检查器；目录及文件由root拥有，部署用户不可改写。这是一次性的管理员安装步骤。CI只提供40位commit SHA并调用已安装的入口，不把incoming目录中的脚本交给sudo执行。

控制器核对产物清单后，将平铺的部署文件放入root控制的 `/srv/sixnine/releases/<sha>`，用该目录渲染Compose，再调用预装的 `validate(config, deployment_directory=该发布目录)`。检查器只允许该目录的 `init_database.py` 和 `Caddyfile` 作为只读代码挂载；数据路径、证书卷、secret引用、端口、命令、单worker和资源边界均固定。不允许额外挂载或同名但位于其他目录的脚本。文件属主、symlink/hardlink检查和可信manifest来源仍是发布控制器的职责，配置检查器不能单独证明产物可信。

当前入站multipart并发限制为每用户2、进程总计4，在读取上传正文之前占用并覆盖接收与处理全过程；持久素材/裁剪配额另行核验。此策略假定一个Uvicorn worker，增加进程或主机前必须加入共享入口限流。秘密授权候选方案见 [RUNTIME-SECRETS.zh-CN.md](RUNTIME-SECRETS.zh-CN.md)，该机制尚未部署。

主机元信息与实际发布归档的只读检查见 [HOST-PREFLIGHT.zh-CN.md](HOST-PREFLIGHT.zh-CN.md)。需要管理员单独安装受信`preflight_host.py`并准备空的root专用Docker配置目录；该工具不会读取秘密内容或自动修复主机。

## 本机离线验收

`test_platform_deployment.py` 默认测试Compose渲染、安全策略和Caddy无网络配置验证；设置 `SIXNINE_TEST_DOCKER_DB=1` 才跑额外本机容器集成。该集成用随机专属名称、内部Docker网络、tmpfs PostgreSQL和合成假密码，验证受限role初始化/重复运行、过权role拒绝、秘密文件权限。没有pull镜像，结束移除自己的临时容器/卷/网络，不挂载任何 `/srv` 生产路径。

2026-10-04：本文件所述检查包含部署策略、固定挂载/资源边界、管理员与app独立密码及本机Postgres集成。尚未在真实Lightsail、真实secret来源、公众DNS或公网TLS上验证；当前镜像的本机测试结果不替代安全更新审查与正式上线验收。
