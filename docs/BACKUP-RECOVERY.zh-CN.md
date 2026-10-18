# 业务备份与隔离恢复

当前工具支持 **SQLite + Local、PostgreSQL + 显式 Local/R2 的业务与媒体备份，恢复到全新 SQLite/Local 隔离目录**，另保留 PostgreSQL 数据库原生导出命令。R2 覆盖通过不可变 key、owner、大小、SHA 与同一数据库快照绑定，不依赖 R2 原生版本控制。跨库作品恢复不等于生产 PostgreSQL 恢复或 PITR。源码测试与真实线上备份/恢复验收分开，以操作回执为准。

这不是自动容灾切换。恢复后需要重新建立认证，并人工核对尚未结束的任务、外部实例和费用。API、CPU 渲染、GPU 和自动扩容服务均不会被备份工具启动。

## 当前可执行入口

从项目根运行已有虚拟环境的 Python：

```text
python tools/backup_platform.py local --database <数据库绝对路径> --object-root <LocalObjectStore根绝对路径> --destination <尚不存在的备份目录绝对路径>
python tools/backup_platform.py verify --backup <已有备份目录绝对路径>
python tools/backup_platform.py restore-local --backup <已有备份目录绝对路径> --destination <尚不存在的恢复目录绝对路径>
```

`object-root` 是包含 `objects/`、`staging/` 的存储根，必须与该数据库当前使用的 LocalObjectStore 相符；不是其中的 `objects/` 子目录。工具不自动猜测正式数据路径。目标的父目录必须已存在，目标本身必须不存在；不提供覆盖或强制恢复选项。

运行镜像中的同一入口是 `python -m studio_platform.backup_cli`，参数完全相同。该模块随 `studio_platform/` 进入CPU镜像，原 `tools/backup_platform.py` 只委托同一个实现，不要求把整套tools或凭据目录加入镜像。由管理员选择受保护且容量充足的备份位置；不新增公开备份下载API，也不在发布时自动执行备份。`postgres-local`无须安装pg_dump；`postgres-database-only`仍需单独已审阅的pg_dump工具。

备份输出是 `database.sqlite3`、`media/` 和 `manifest.json`。恢复输出是 `platform.sqlite3`、`objects/` 和 `RECOVERY-HOLD.json`。只有最后写出的 manifest 才表示备份完成；失败时保留已创建的新目录供管理员判断，不能把它当成功备份，也不会自动清理任何源文件。

程序接口在 `studio_platform/backup.py`：`backup_local`、`backup_postgres_local`、`backup_postgres_engine`、`verify_local`、`restore_local`、`dump_postgres`。CLI 只输出数量和状态，拒绝时不给出 DSN、对象 key、子进程错误文本或用户媒体路径。

## 哪些东西会保存

| 内容 | 处理方式 |
| --- | --- |
| 项目文档、脚本、计划、任务与尝试记录 | 同一 SQLite 一致快照或 PostgreSQL 只读事务中的业务表；作品的 tenant、owner、project ID 保留 |
| 已入库素材原件和归一化副本 | 按快照中资产记录引用复制，保留原 key、类型、字节数和 SHA-256 |
| 已发布且 validated 的任务成片和音轨 | 按 artifact→job→owner 关联复制；真实模拟 MP4 已完成恢复验收 |
| 预算、配额、上传/输出收据、实例意图 | 保留账本和未知状态证据，不声称云费用已经停止 |
| 账户密码、会话、服务端 token、登录限流认证表 | 完全不导出；新库认证需重新初始化 |
| `.env`、中央 API 加密库、SSH、钱包、配置秘密 | 不枚举、不复制；这些位置不作为输入 |
| 尚未发布的 ingress/staging、孤立对象、worker 缓存、模型权重 | 不复制；manifest 明确 `staging_included=false`、`orphan_objects_included=false` |

数据库中的提示词、脚本和作品关系本身是用户数据，因此备份必须保持私有。业务数据中若有人自行输入了秘密，工具不会理解其语义并自动脱敏；不能把备份当作可公开的调试包。

未完成上传只保存已有业务账本和已发布引用，`pending_assets` 会被报告。仅在 staging 中存在的唯一素材不属于这份备份的完整性承诺，不能因为备份成功就删除原项目或临时上传目录。恢复目录改变了存储/暂存绑定，旧上传与输出收据也不会自动换绑继续执行。

## 一致性与文件安全

- 使用 SQLite Backup API 先形成有时间、容量上限的内存快照，支持 WAL 已提交数据；不会在运行中直接复制裸数据库文件。随后只从该快照导出可信代码定义的业务表到全新数据库，认证表页从未写入备份文件。
- 媒体来自该快照确定的不可变 key；复制前核对存储元信息，复制过程中核对实际长度和完整 SHA-256。若引用缺失、内容变化、跨 owner、未知 provider 或 checksum 不一致，整份备份不能通过验收。
- 验证时重新读取备份数据库，核对业务表行数、资产/任务关系、媒体清单和实际文件；不能仅凭 manifest 宣布成功。
- 未知表、触发器、视图和现有列结构差异会拒绝，需要随版本升级审查导出契约。不会猜测新字段或静默漏掉陌生业务表。
- 所有路径需显式绝对路径，不跟随符号链接或 junction，拒绝多硬链接媒体。备份目标不能放进源对象根，恢复目标不能放进备份内部。
- POSIX 新目录为 `0700`、文件为 `0600`；Windows 新目录移除继承权限，仅当前用户与 SYSTEM 可访问。权限设置失败即停止。未改变源目录 ACL。
- 默认数据库快照上限 256 MiB、60 秒；单媒体上限 512 MiB、最多 100,000 个不同 key、媒体总量默认 40 GiB。超限需明确扩展设计，不自动无限占用内存或磁盘。

这些 SHA-256 用于发现损坏和错误关联，不是签名或抗恶意篡改认证。备份目录仅由可信管理员管理。ACL 也不是静态加密：正式方案仍需加密卷或获批的加密备份目的地、独立权限和异机副本；本轮没有新建密钥、凭据或远端存储。

## 恢复后的安全状态

`restore-local` 先完整验证备份，然后只向全新目录写入。它不会连接或覆盖运行中的数据库。

1. 所有历史计划及任务的 execution plan 均设为 disabled，计划过期。未终结任务进入 `recovery_hold`，错误码 `disaster_recovery_review_required`，清除 worker lease、递增 fence。
2. 保留原任务 ID、当前 attempt、上游 task ID、实例 ID/状态、费用和存储预留，以供查询真实上游状态。不会将未知提交解释成失败、取消或可重试。
3. worker 标记 unknown/drain，设备与 CPU slot 停止被视为可用；所有 pool/global capacity 为 0，scaler leadership 过期。已经存在于供应商的实例可能仍然计费。
   容量审批另行显式设为 `enabled=0`，等待容量的记录设为 `recovery_hold`；保留审批payload/hash、截止时间、实例关联、cycle和预留费用。这些动作不依赖“capacity为0”间接阻挡。旧版没有容量表的备份，先验证原有表集，再仅在全新恢复目标建立空表，原备份不改写。
4. `RECOVERY-HOLD.json` 记录原状态及待核对事项。前端已接“恢复后待核对”，不提供对这些旧任务取消或重投的普通用户操作；作品编辑保持可用。
5. 认证表没有备份。使用当前版本的受控管理员入口重新建立原用户名对应身份；无需改作品 owner。新会话/token 必须重新签发，历史会话不能复活。

管理员后续顺序：验证私有目录与 checksum → 核对作品 owner/媒体 → 重新建立认证 → 在不执行生成的环境查看恢复内容 → 查询有上游 ID/实例 ID 的未知任务和实际账单 → 逐项决定账本处理与是否允许新任务。当前没有通用“解除全部 hold”按钮，也没有自动重投接口。

**恢复已发布作品可供查看，不等于已经能够继续全部写入。** 素材收据、active上传数量及存储预留仍保留原始证据；若源快照带有busy上传，新目录中可能仍占满名额。Local根与工作目录属于原storage_binding，现有素材恢复工具明确拒绝跨目录补绑或核销；必须另做未决收据、实际文件、原位置与新位置的人工核对和迁移设计。不能为恢复上传功能直接清零active、删收据或修改绑定，源staging内唯一原件仍需独立保全。代码依据为`backup.py`的隔离恢复、`assets.py`的存储绑定和`storage_asset_journal.py`的配额检查。

## PostgreSQL + Local：完整便携业务备份

部署为 R2 时，管理员在已接受的原应用容器内使用
`python -m studio_platform.backup_cli configured --destination <全新私有目录>`。
它只复用当前配置的数据库和 R2 读取身份，不调用应用初始化或创建源表。
每天执行与独立 S3 传输、精确版本恢复的流程见
[每日备份运行约定](../deploy/platform/DAILY-BACKUP.md)。该入口默认限制媒体总量
2 GiB，旧显式 Local 入口的限制不变。恢复目录只读验收通过前，不删除源媒体。

```text
python tools/backup_platform.py postgres-local --url-file <受保护的应用DSN文件绝对路径> --object-root <LocalObjectStore根绝对路径> --destination <全新备份目录绝对路径> --schema public
python tools/backup_platform.py verify --backup <该备份目录绝对路径>
python tools/backup_platform.py restore-local --backup <该备份目录绝对路径> --destination <全新SQLite恢复目录绝对路径>
```

此入口直接使用现有 psycopg/SQLAlchemy，不需要安装 `pg_dump`、数据库管理员、创建新角色或新PG数据库。应用身份必须有目标业务表的 SELECT 权限；源连接始终为 `REPEATABLE READ` + `READ ONLY`。只从一次事务读取可信表的列、业务行、表计数，写成私有便携SQLite，再从它得出对应媒体引用。媒体复制使用不可变key和实际SHA校验；新增提交不会混进较早的快照，缺失或变更的文件会使备份失败，不会出成功manifest。

本包Compose里的现有DSN是无query的 `db:5432/sixnine`、角色`sixnine_app`，内部网络由Compose隔离。管理员在这个已审阅网络内运行portable备份时，需显式加 `--private-platform-network`，它只接受上述精确身份，并显式设置连接`sslmode=disable`；不接受其他host、端口、账户、数据库或query，不修改原DSN文件。这个参数是操作者对网络环境的明确声明，不是网络隔离自动检测。其他远端连接仍需在受保护DSN中明确SSL配置，不会悄悄退回另一个端点。

源数据库有不认识的业务表/视图或列结构变更时拒绝；认证四表只识别名字，不读取其行。源查询设有 statement/lock/idle-in-transaction 超时，并对整体导出耗时、行数和数据量作检查；单行JSON编码≤16MiB、单表≤100万行、业务数据量及便携SQLite文件≤256MiB。超过这些早期规模界限需要专门的流式/分卷方案，不能自动扩到无界内存。

输出沿用上述成功manifest与媒体格式，额外写明 `source_database=postgresql`、`restoration_target=isolated-sqlite-and-local-objects`、`native_postgres_restore=false`。恢复执行同一套认证排除、owner保留、任务hold、禁用容量审批的逻辑。它不改变现有应用的数据库设置、不配置新服务，也不将SQLite隔离恢复当作原生产PG上线。

真实本机PG测试在snapshot建立后，通过另一个连接提交项目更名和新素材上传。备份仍保留旧项目/旧素材集合；恢复后保留假上游task、未知实例及123微美元预留，认证canary不在文件中；审批禁用、waiter hold、跨owner读取拒绝均通过。另测中断快照、缺媒体不写成功manifest、受保护URL入口、目标拒覆盖以及JSON null和SQL NULL分别保持原语义。所有资料均为隔离测试schema内合成数据。

## PostgreSQL：原生数据库单独导出

```text
python tools/backup_platform.py postgres-database-only --url-file <受保护的完整DSN文件绝对路径> --destination <新目录绝对路径> --schema public
```

此命令需要操作者已安装兼容的 `pg_dump`，可通过 `--pg-dump` 指向受信可执行文件。不会安装软件或使用默认服务配置。URL 文件必须是受保护的单行 PostgreSQL URL，明确给出 host、port、user、password、database；POSIX 禁止 world 权限和 group-write，允许受控 group-read。未设 SSL 或显式 `disable` 仅接受 loopback；远端必须显式配置 `verify-full`。上文 `postgres-local --private-platform-network` 是唯一受控私网例外，只允许精确 Compose 身份；不能靠任意远端 URL 的 `?sslmode=disable` 绕过。

DSN 仅在进程内解析，子进程通过最小环境接收 libpq 变量；参数、日志、manifest 不包含密码。`PGPASSWORD` 不是可抵御同用户/管理员进程检查的秘密边界，正式运行身份应独立、收紧进程检查权限，并从获批的运行时授权入口提供已有受保护 URL 文件。此工具不会导入中央凭据、复制 DPAPI 库或生成项目 `.env`。

导出只允许当前业务表，使用 custom archive、`--no-owner --no-acl --no-blobs`，不包含账户/会话/服务 token，不包含角色等全局对象。manifest 明确 `media_included=false`、`restore_verified=false`，后者代表该次导出的目标恢复尚未验证，并非否定下述本机演练。

**2026-10-04 已完成真实 PostgreSQL 17 本机演练**：在已有专用测试容器 `sixnine-platform-test-db-20261004` 中，为临时假项目创建唯一 schema；真实 `pg_dump` 导出业务表，真实 `pg_restore` 恢复到新建的独立数据库。验证全部业务表行数、素材 key/SHA 与同一快照媒体文件相符；认证 canary 表不在恢复库；将恢复任务置 hold 后仍保留假上游任务 ID、`creation_unknown` 实例和预算记录；原测试源任务仍为 running。最后仅删除本次唯一目标 DB/schema 并通过目录查询确认删除。未访问其他容器或真实项目库。

原生dump演练证明当前表集合和媒体关联可恢复；上面的新portable入口另已证明同一PG事务导出与媒体清单一致。二者都不等于提供通用生产PG一键恢复。以下尚未完成：

- 原生 `pg_dump` 与媒体清单共同绑定同一个导出snapshot的编排；现有 `postgres-database-only` 仍只有数据库，不能拿它和另一时点的文件列表拼成完整备份。
- 生产 PG 恢复 CLI、目标版本兼容迁移、独立恢复账号及恢复后隔离逻辑的正式自动化。
- WAL/PITR、异机加密副本、保留/过期策略、定期验收报警；未设置保留期或删除授权。
- S3/Hippius 源对象备份仍不支持；R2 使用下述 configured 入口，不能向 Local 命令隐式注入替代 provider。未完成上传的临时原件与原始 storage binding 仍需独立保全。

## 验收命令及结果

```powershell
# SQLite/本地测试；没有明确启用时，原生PG演练会跳过。
.venv\Scripts\python.exe -m unittest test_platform_backup -q

# PG便携测试仅接受已有进程环境中明确提供的本机sixnine_test数据库。
# PLATFORM_TEST_DATABASE_URL由获准的测试运行器注入，不在报告复制DSN。
.venv\Scripts\python.exe -m unittest test_platform_backup_postgres -q

# 仅在本机已存在指定专用测试容器且获准后运行。
$env:SIXNINE_TEST_PG_BACKUP='1'
.venv\Scripts\python.exe -m unittest test_platform_backup -q
```

本次最终备份测试20项全部通过：16项基础/原生PG演练测试，加4项真实PG portable测试；与主机预检、部署包验证、bootstrap及release合并回归共47项通过。覆盖真实 PNG 与模拟 MP4 恢复、WAL 提交、并发PG快照、SQL/JSON null区别、跨 owner 读取拒绝、认证内容排除、容量审批禁用与旧备份兼容、损坏/缺失/硬链接/路径篡改拒绝、已存在目标不覆盖、未知上游证据保留及严格私网DSN选择。没有用真实生产作品或供应商生成收费内容作测试。

## 官方依据与对象存储后续决策

- [SQLite Online Backup API](https://www.sqlite.org/backup.html) 与 [Python sqlite3 backup](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup)：一致快照而非运行中裸复制；本工具增加时间/容量界限，避免持续写入造成无界等待。
- [PostgreSQL pg_dump](https://www.postgresql.org/docs/current/app-pgdump.html) 与 [pg_restore](https://www.postgresql.org/docs/current/app-pgrestore.html)：custom archive 支持受控恢复；按表导出不能假定自动包含全部依赖，因此必须在新目标实际验证。它不替代 PG 全局角色和 PITR 方案。
- [libpq password file](https://www.postgresql.org/docs/current/libpq-pgpass.html)：如未来改为原生 pgpass，应遵守文件权限要求；当前不另建或复制密码文件。
- [Cloudflare R2 S3 兼容表](https://developers.cloudflare.com/r2/api/s3/api/)：不能假定 S3 Bucket Versioning / Object Lock API 同样可用。[R2 Bucket Locks](https://developers.cloudflare.com/r2/buckets/bucket-locks/) 是独立的保留控制，不等于历史版本备份。
- [Amazon S3 Object Lock](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html)：后续若选择 S3 的版本与锁定策略，需单独配置权限、保留范围和费用；本轮没有改变任何桶设置。

删除当前项目之前，仍必须按中央 `HOUSEKEEPING.md` 核对唯一未发布素材、模型/环境共享关系与云计费资源。退役的最后一次备份须先冻结所有API编辑/上传、worker收集及其它写入，再核实它们确实停止，制作并验证最终一致备份，并单独归档未纳入portable包的唯一staging/工作目录资料。日常在线快照可以保持服务运行，但不包含快照之后的新提交，不能凭在线备份verify成功就删除仍在写的源。存在这份工具或一次成功演练，不构成删除数据的授权。
