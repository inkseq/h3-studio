# 素材接收与处理的管理恢复

当前提供 **LocalObjectStore 的显式离线管理恢复**，默认仅列出收据。普通浏览器没有抢占busy的权限；API启动不会自动接管。不会启动生成、模型下载或租赁云实例。数据仍在原位置，不移动原件，不创建新素材ID或重设全部配额。

## 使用前确认

在目标主机使用服务相同的 `SIXNINE_DATA`、`SIXNINE_DATABASE_URL` 或 `SIXNINE_DATABASE_URL_FILE`、存储和租户配置；数据库连接信息仅在进程内加载，不能贴到命令行或输出。`--data-dir`必须是已存在绝对路径；有显式DSN/DSN文件时保留该数据库，只有两者均未配置才按该目录选择SQLite。必须使用同一真实Local存储/暂存根，不能指向备份或另一个相似文件夹。

默认查询示例（应用容器内可用 `python -m studio_platform.asset_recovery`）：

```text
python tools/recover_platform_assets.py --tenant sixnine --owner superdan --project-id <项目ID>
```

返回asset ID、当前receipt_version、状态、busy、完整接收/准备标记、预留字节及有限对象phase。不展示文件内容、存储键/路径、签名URL、会话或任何API凭据。每页默认20，最大100，`--offset`翻页；SQL只读事务，不建表、不实例化存储服务。现有账户supervan需另行指定，不合并账户。

## 必须先停止旧写入者

1. 保存现有收据和唯一素材的备份/核对资料。正在进行的网络写入和部分接收文件仍需保留。
2. 停止旧API容器/进程，确认没有其它机器、容器或旧版本API继续写**同一DB和同一staging**。CPU/GPU生成worker是另一种任务，不要为了素材修复误停或重租GPU；若部署不清楚是否存在其它素材写入者，先停止恢复操作。
3. 使用默认只读列表核对明确的tenant、owner、project、单一asset、版本和phase；不提供全量恢复或一键清active。
4. 再用下面某一命令并显式声明 `--assert-writers-stopped`。此声明是管理员的真实操作前提，不是工具自动证明“进程已死”。未声明时在读取配置/打开数据库前拒绝。

新版本每个asset从接收前到ready/配额释放均持有真实OS操作锁；恢复拿不到锁就拒绝，即使提供声明也不能抢仍在运行的本地操作。锁不使用超时夺取。Linux用flock、Windows用字节锁，进程退出释放；保留的lock文件本身不表示进程仍运行。不同素材仍可并行，解码内存准入保持原规则。

当前Compose的API服务名为`app`。确认部署版本和写入者范围后，管理员可在已审阅发布目录使用下面的命令形式；这是操作示例，本轮没有执行。DB须已经在运行，`--no-deps`不会替管理员启动数据库或其它服务；run覆盖API命令，不发布服务端口。

```text
docker compose --env-file <已审阅非秘密site.env路径> -f compose.yaml stop app
docker compose --env-file <同一site.env路径> -f compose.yaml run --rm --no-deps app python -m studio_platform.asset_recovery --tenant sixnine --owner superdan --project-id <项目ID>
```

第二行默认只读；实际恢复在该模块命令后追加下文相应`--mode`、素材ID、版本和声明。保持相同秘密挂载、UID、数据卷和私网数据库配置，不拷贝DSN。逐记录核对完成后，管理员再按既有发布流程重新启动`app`，不能用`down`同时销毁或中断无关服务。

**旧版本、异host、未共享暂存根或不可靠网络文件系统的写入者不受这份本机锁证明覆盖。** 必须人工确认停止。不要删除锁文件来绕过持有者，也不要把时间久、重新构造AssetService或单个Uvicorn worker参数当作停止证据。

## 完整接收的恢复

```text
python tools/recover_platform_assets.py --mode recover --tenant sixnine --owner superdan --project-id <项目ID> --asset-id <素材ID> --receipt-version <当前版本> --assert-writers-stopped
```

- 完整原件已接收、预处理期间被停止：验证原件/哈希并继续同一收据，不能生成另一个素材ID。
- 已prepared且存储写入未知：仅核对同一个已记录的key；缺对象不能换key盲目重写。尚未发起的model对象可按原计划首次写入。未知写入保留原预留和证据。
- 已保存ready、尚未释放active：核对每个已验证存储对象及元信息，只归还一次active并按实际保留字节记账；不重做解码或PUT。

`state=recovered`仅在最终`status=ready/busy=false/media_ready=true`返回，退出0；其它返回`state=incomplete`退出2，不能当成可用素材。版本变化、身份不匹配、完整性失败、仍有本地活操作等返回`asset_recovery_refused`退出1；工具不会输出原始异常、DSN或原件文字。重新列举核对后才决定重试，不忽略失败。

## 未完整接收的收据

```text
python tools/recover_platform_assets.py --mode settle-incomplete --tenant sixnine --owner superdan --project-id <项目ID> --asset-id <素材ID> --receipt-version <当前版本> --assert-writers-stopped
```

仅允许已停止、明确匹配存储身份、`accepted_input=false`且没有prepared/derivation/任何对象写入证据的收据。新上传在接收前已持久化suffix和storage_binding，故进程在copy中停止也能核对；历史缺这些证据的收据拒绝，不猜测或自动补绑。

操作将该收据标为failed，原样保留包括`receiving`在内的已收到字节，按实际保留大小记账，归还active。**部分文件不会被解码、上传或标ready，也不删除。** 用户之后重新选择完整文件上传。返回`incomplete_settled/status=failed/media_ready=false`且退出0，只代表处理名额已结清，绝不代表媒体恢复完成。重试需用重新列出的当前版本，重复结算不会再次归还active。

原件是否完整、对象结果未知、storage_binding缺失或不匹配时，不能用此模式绕过核对/释放未知存储预算；保留收据供进一步人工核验。

## 已验证与限制

2026-10-04：17项专门恢复测试分别在临时SQLite与真实本机隔离PostgreSQL schema全部通过；recovery/assets/media_admission/upload_route合并65项通过。仅小PNG和假存储故障，覆盖处理中断、ready-before-release、部分receive、活线程/另一个进程持锁拒绝、双管理员竞争、当前版本/owner/project/storage绑定、同key未知读回、缺对象不重写/保留预留、默认只读、错误DB不建表及秘密错误脱敏。具体测试为 `test_platform_asset_recovery.py`；没有访问真实作品、云存储或付费生成。最终45d3cccb不可变Linux镜像已通过97项专项，其中包括这17项恢复测试及实际Linux锁路径；同级发布收据见验收索引。

本轮仅Local支持；R2/S3/Hippius及异机存储恢复拒绝，不加载中央API凭据。该命令可使用现有PostgreSQL DSN，但不会迁移数据库或自动初始化空库。运维仍需确认实际已停止写入者、独立备份和可用磁盘空间；本机测试不是生产灾备演练或跨主机fencing。

业务备份恢复到另一Local目录后，旧上传收据仍保留原storage_binding和预留。本工具不能用来跨目录自动核销旧busy状态或“修好”新目录配额；这属于尚未实现的迁移/灾备写入恢复流程，见[备份恢复边界](BACKUP-RECOVERY.zh-CN.md)。
