# Worker运行方式与安全门槛

2026-10-04。本轮只运行临时本地测试，不启动常驻服务，不租云GPU。默认backend=disabled；控制器的自动创建默认dry-run、跨池/各池容量默认0。CLI不会发现/租用GPU、读取旧H3云脚本或自动续租供应商实例。

## 本地入口

在`C:\Users\danmo\Desktop\inference\h3-studio`使用现有专用`.venv`；容器用镜像中的Python。例子只是启动形式，执行前由主进程统筹，避免与预览worker并发。以下disabled命令不打开数据库、不创建work目录、不发请求：

```powershell
.\.venv\Scripts\python.exe -m studio_platform.worker --worker-id mock-preview --pool mock --work-dir C:\Users\danmo\Desktop\inference\h3-studio\.worker-preview --once
```

只有显式`--backend mock`才运行CPU演示。`--once`处理最多一个阶段；已有任务可能需后续调用完成收集/核对。测试环境明确设置新DATA路径，不能指向旧`data/studio.sqlite3`：

```powershell
.\.venv\Scripts\python.exe -m studio_platform.worker --backend mock --worker-id mock-preview --pool mock --work-dir C:\Users\danmo\Desktop\inference\h3-studio\.worker-preview --data-dir C:\Users\danmo\Desktop\inference\h3-studio\.worker-preview-data --once
```

API与worker必须指向同一新平台数据库及私有object store，才会看到同一任务；单独新建DATA不会复制已有用户/项目。省略`--once`是显式循环，SIGINT/SIGTERM停止新claim；已提交任务保留账本与上游ID，后续进程只核对/重收集，不重新生成。此行为不停止供应商实例计费。FFmpeg/HTTP每阶段有超时，不承诺瞬时退出；容器stop grace应允许正在进行的有界阶段完成，强杀后仍按lease/fence恢复。

所有输出只含固定state、job_id及simulation，不打印环境、数据库URL、prompt、签名URL、凭据或响应体。CPU样片全程显示SIMULATION / CPU demo - NOT H3；不能拿它声称H3画质、时延或音频表现。

## Settings与依赖

CLI复用`studio_platform.settings.Settings.from_environment()`，DATA来自`SIXNINE_DATA`，数据库来自`SIXNINE_DATABASE_URL`或其`SIXNINE_DATABASE_URL_FILE`入口；未指定二者时使用DATA/platform.sqlite3。显式`--data-dir`只改变派生SQLite路径，保留Settings已从URL/文件加载的明确数据库地址。数据库认证只由受权运行环境注入，不在shell参数/文档中填写真实口令。SQLite用于本地验证，生产多worker使用PostgreSQL的短事务与行锁。

已验证依赖为SQLAlchemy2.0.54、psycopg3.3.6、现有httpx/Pillow/FFmpeg/ffprobe；锁定文件及Docker由主项目维护，不直接移动虚拟环境。`Repository.create_schema()`创建repository.metadata全部platform表，并对旧instance_intents无损补provider='unknown'、旧workers补持久drain_requested；不能替代其他模块/未来字段的显式迁移。管理initdb另需注册auth/assets等各自metadata。

当前CLI仅支持local object store。远端S3/R2/Hippius需复用项目现有受权storage adapter注入WorkerRunner，不能让第三方GPU持中央主key或数据库管理权限。CLI遇到remote store会安全拒绝，不回退本地或悄悄使用默认端点。

## 真实Comfy槽必须显式配置

真实模式为`--backend comfy-worker`，须提供`--endpoint`、完全相符的`--allowed-origin`、`--provider`、`--instance-id`、每张`--gpu-id`、允许的`--recipe-id`、精确`--model-id`、`--configuration-id`和`--comfy-revision`。远程只允许明确HTTPS origin；loopback可以HTTP。端点认证与私有隧道由受权控制面处理，原生Comfy端口不开放到公网。

实际启用前操作员必须在账本明确配置跨池总容量和各池上限、核对费用/预算，并以真实队列空闲证据使用`--confirmed-idle`。该参数是一项明确操作声明，不是端口健康检查，也不是GPU模型加载验证。缺真实manifest或预算授权时不能虚构这些值来让命令通过。

同一provider/实例/物理卡只能被一个worker登记；TP2一worker登记两张卡而仍一任务槽。登记后只能执行同pool/backend/recipe/model/config计划。`pool_status`的ready+busy表示未过期的匹配注册心跳，可以排队；expired/unknown/draining不作健康容量。正常idle循环续有效心跳；登记租约过期保留设备所有权，不自动恢复空闲。未知running先核对，不能新登记另一worker或假设GPU空闲。提交前崩溃安全重排后，仍需明确上游空闲才能恢复；drain跨核对/到期/成片完成保留，操作员确认空闲并mark_ready才恢复新生成。

控制面必须负责认证worker、认可实例设备ID和模型资格manifest。普通用户不能登记设备、改预算/报价/资格；映序通过自身后端调用用户隔离任务API。第三方GPU的单任务授权/下载上传网关及生产私有存储接入仍属后续运行适配，当前CLI不声称它们已完成。
现已增加CPU多槽fleet入口（FLEET-CONTRACT.md）：Runner留CPU，GPU只Comfy，通过已建立逐实例私有隧道访问，无需让GPU持DB/store凭据。单worker CLI仍仅local store；fleet复用明确R2 adapter入口，真实远端存储仍需运行阶段核验。真实新提交两次检查当前ExecutionPolicies.submission_allowed；资格/报价/策略撤销会在提交前失败，已有上游任务仍可核对/收集。

## 取消、退役与费用

Comfy定向取消严格按经核实revision能力，详见WORKER-CONTRACT.md。未知版本只可删除已核对的pending prompt；running不能用全局interrupt，取消请求不等于已停。生成成片可在账单待核对时下载；预算仍保留，实际费用由受权后台独立结算。

worker drain/retire仅改变调度资格/设备登记；供应商实例销毁须由独立控制面核对，超时和删除目录都不会停止计费。真实创建/销毁逻辑本轮不由此CLI执行。前述本地SQLite、隔离PostgreSQL、CPU可解码视频和fake Comfy协议测试，不构成真实GPU生产验收。
