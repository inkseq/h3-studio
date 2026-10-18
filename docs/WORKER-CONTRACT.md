# 显式单槽执行器与验证边界

2026-10-04。`studio_platform/worker.py`，不导入旧server/cloud脚本，不碰旧data，不租GPU。

入口：`WorkerRunner(repo,store,work_dir,backend=DisabledBackend(),control=None,submission_guard=None,stop_requested=None).run_once(worker_id,pool)`。真实生产须传持久`WorkerControl`，并先登记经过资格验证的精确pool/recipe/model/config执行槽；单独Runner无control只供隔离协议与CPU测试，不构成跨机GPU占用控制。
返回只含job_id/state/simulation的安全摘要，不返回prompt、凭据、签名URL或供应商原始响应。
默认disabled不连接网络、不启动FFmpeg、不领任务、不创建work目录。

显式测试：`MockBackend(state_dir,enabled=True)`。CPU生成固定画面与可选440Hz音轨，整段永久SIMULATION / CPU demo - NOT H3标识；用于接口与媒体管线测试，不是AI结果或H3速度/质量证据。执行计划backend必须mock且enabled=True，否则不执行。

`ComfyBackend(endpoint='',enabled=False,allowed_origins=(),transport=None,actual_cost_resolver=None,comfy_revision=None)`。
启用须显式endpoint及完全相符allowlist；HTTP仅loopback，远程只允许显式HTTPS；拒绝URL认证/query/path/fragment，禁重定向和环境代理。未装GPU/未授费用时保持disabled。
本机/网关必须专属于一个已登记GPU执行槽，Comfy原生端口不公网开放。当前只接已有纯函数Comfy H3编译器，不接受浏览器任意node图。

一次run_once优先收集、对账、生成。短阶段让出lease，再由下一次run_once恢复同attempt。模型提交前先begin_submission持久化；/prompt含唯一client_id与extra_data.sixnine_attempt_id，SaveVideo前缀也绑定attempt。
新Comfy提交必须有submission_guard，默认无guard拒绝；准备前和紧邻begin_submission前各检查一次。CLI/fleet接当前ExecutionPolicies.submission_allowed，过期/撤销策略在确未提交时以成本0失败并释放预算，不修改参数/价格后静默重发。已running/unknown/collecting继续原attempt，不要求新的报价/资格。CPU Mock是明确模拟例外。stop_requested供多槽CPU supervisor的便携drain标记，循环与阶段心跳核对；详见FLEET-CONTRACT.md。
HTTP响应丢失/5xx/无有效prompt_id进入submission_unknown。对账同时查带标签history与queue；无匹配或多个匹配保留unknown，不自动重发。历史被清空时可能需要人工/网关日志对账。
成本与作品状态分离：成片已校验和存储即可succeeded。没有实际账单、账单resolver抛出异常，或返回非整数/负数/越界费用时，result.billing_status=pending，原预算继续占用；之后受权后台调用repo.settle_completed_job(scope,id,actual_cost_microusd=...)，同金额重复结算幂等、矛盾账单拒绝。此降级仅包围费用读取，不隐藏素材校验、对象写入或租约丢失错误。不把估算当账单，不把None当0。实际费用resolver仍须使用有界读取超时；不能把无期限阻塞的供应商账单查询塞入同步worker。

素材只使用不可变计划内已授权model object_key，按owner前缀再检查；上传规范化副本到私有Comfy输入区。准备失败可延后，尚未生成；后续重领记新的未提交attempt。
输出只取该图SaveVideo/SaveAudioAdvanced的output/h3-studio、精确attempt文件名；不抓用户/供应商任意URL。下载限制512MiB/180s；JSON限制16MiB、提交响应1MiB；HTTP超时30s；预处理/导出/全解码FFmpeg180s、ffprobe30s。按阶段心跳续本地lease900s，不代表续租GPU。
按用户要求输出精确24fps帧数/时长与尺寸，检查H264；声音任务要求32kHz双声道与独立FLAC。全解码成功后记录size/sha256/维度/时长/音轨，再写私有store。对固定attempt对象键HEAD/hash核对，重收集不另建生成或重复物理对象。
独立FLAC的 `metadata.duration_s` 使用验证后的文件实际时长，保持现有请求时长的0.1秒验收容差；请求仍保留原时长，不能用请求时长推断后续可裁剪的音轨范围。此修正仅影响新校验写入的音轨，不自动改写已有receipt或历史artifact元数据；粗剪执行仍核对真实解码样本数。

上述180s媒体包络针对H3/Comfy。独立cpu-render章节作业允许最多600秒/14400帧，受控本地渲染及collection完整解码超时最多1800秒，阻塞阶段有lease keepalive；不能把其较大限制套到H3。新章节render v3支持显式已确认字幕，v1/v2/v3按精确configuration路由，固定字体/布局和来源确认见CHAPTER-RENDER-CONTRACT.md、CAPTION-BURN-IN-CONTRACT.zh-CN.md。CPU粗剪明确actual cost 0是当前不另向用户收生成费，不表示主机/存储/运维免费。

collection先使用ArtifactWriter持久预留本地staging及owner/shared存储额度，固定attempt receipt保存校验证据；大于单PUT上限的对象走已有multipart链。输出校验、对象落库与预算结算是不同证据。只在当前lease/fence下原子发布artifacts与存储结算，恢复重收集不再POST生成，也不把本地receipt当作供应商实际账单。远端传输仍需单独在线验收。

取消按明确部署revision处理，绝不调用全局interrupt。已核验的官方ComfyUI revision `e9027f2b30f37bb3052714eb08fcf479542f4fc0`具有按task ID的`POST /api/jobs/{id}/cancel`，仅显式声明此revision时使用；响应只表示取消动作已派发，继续核对history/queue与结束事实。部署manifest中的revision声明不是worker对远端二进制的运行时证明。
未知/其他revision只允许对queue_pending中本attempt的精确prompt ID执行`POST /queue {delete:[id]}`，并核对queue确已移除；该接口成功响应可为空。running任务没有经核实的定向取消能力时保留cancel_requested，等待结束，不能借用全局interrupt。进程重启后遗失的内存dequeue确认保守视为未知；不从history缺失推断停止或成本0。取消后晚到成片照常保存并标记completed_after_cancel_request。
源码证据：[官方按ID取消实现](https://github.com/Comfy-Org/ComfyUI/blob/e9027f2b30f37bb3052714eb08fcf479542f4fc0/server.py#L861)、[入队extra_data保存](https://github.com/Comfy-Org/ComfyUI/blob/e9027f2b30f37bb3052714eb08fcf479542f4fc0/server.py#L1012)、[原生queue删除](https://github.com/Comfy-Org/ComfyUI/blob/e9027f2b30f37bb3052714eb08fcf479542f4fc0/server.py#L1042)。
`runner.drain()`阻止新领取，已提交任务让出持久租约保留对账。`run_forever(...)`仅显式调用时安装SIGTERM/SIGINT处理，退出后持久设置槽drain_requested、恢复handler并关闭HTTPclient；即使数据库暂不可用，仍执行handler/client清理，不宣称写入drain成功。drain标记保留至明确空闲后操作员恢复，不因当前任务核对完成而自动清掉。不会杀GPU或全局取消其他人的任务。
正常空队列run_once也续尚有效的登记心跳，避免活着的idle worker静默失联；已经过期/unknown的登记不会由此自动复活，须核对上游或明确空闲事实。

单槽本地OS锁按endpoint哈希加锁，同endpoint的runner必须共享work_dir；锁跨线程/进程且随进程退出由OS释放。持久Control另以provider/instance/physical_gpu_ids约束跨进程槽；未知任务不会随OS锁释放而变空闲。多主机仍需API机器身份、端点与槽授权集成，登记资料必须真实，不能靠自报不同provider/实例名绕开设备身份。
SQLite/PostgreSQL账本测试＋CPU媒体＋httpx.MockTransport契约测试通过，不代表实际Comfy/H3 GPU或远端存储验收。真实GPU配置、权重revision、未知提交恢复的上游日志留存和成本归属仍需真实阶段门槛。
