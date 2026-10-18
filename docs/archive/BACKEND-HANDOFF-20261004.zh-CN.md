# 后端收尾与 onboarding 交接

2026-10-04，Australia/Sydney。用户要求先暂停后端扩容实测，完善 front-facing 和 onboarding 后再继续。本文件记录暂停边界，不授权重租、重新生成或启动新一轮验收。

## 线上已经可用

- https://www.sixnine.art ：AWS 新加坡 EC2 CPU 主机、HTTPS、PostgreSQL、私有素材/成片存储；当前线上 commit `fc91fc705d1cd559a4d33bc5ab6a02763f9ee7d6`。
- `superdan` / `supervan` 正式密码登录，按账户隔离故事、素材、任务、结果和 API Key。
- 自助创建/撤销 API Key，限制权限、故事及到期时间；机器 Key 无权管理账号密码或其他 Key。
- Agent API 可创建多个故事、章节、场景、角色、镜头、素材关联、画布数据，提交生成计划/任务，下载/采用结果，设置裁切、字幕和声音编排，导出 JSON/CSV/SRT。
- 网站与 Agent 读写同一云文稿，有版本冲突和幂等保护；本机草稿独立，登录后需明确保存/选择云故事。
- `/v1/agent-guide`、`/v1/guided-schema`、`/v1/agent-skill.zip`；本机 Skill 在 `C:/Users/danmo/.codex/skills/sixnine-yingxu`。
- GitHub CI → 私有 S3 → 独立批准确切 manifest → 固定 SSM Document → EC2 发布已实际跑通。push 不直接绕过操作员批准替换生产。

## 真实生成与并发证据

1. 隔离业务 API：两台单卡 RTX PRO 6000 Blackwell 96GB，8/8 个 50 步、768P、请求 5 秒、有声任务成功；真实积压触发第二台，GPU 执行重叠约 1651 秒。成片均完整解码/哈希验证，两个账户隔离通过。
2. 参考模式：图片、视频、音频、视频内音轨、图片 guide 实际参与 Ref2VA；4 步 480P 与 50 步 768P 均有独立实测。不是只加载 VAE。
3. 正式公网：`1816736a-49f2-4be4-89a3-0f1ad1e7bfb2` 已从公开 HTTPS API 经生产 PG、GPU、私有存储回写到 superdan 的「公网 H3 实测 · 花园金毛」，项目 ID `production-h3-acceptance-20261004`。MP4 和独立 FLAC 均验证、采用、裁切及绑定音轨；会话 API 与 PAT 读到同一文稿。
4. 单 GPU 生产交接 worker 的 512 MiB 配額内实测峰值 282009600 字节，无 OOM。独立解码通过不代表做过主观音质或 VBench 评分。

证据入口：`AGENT-API-RELEASE-20261004.zh-CN.md`、`.platform-agent-e2e/public-api-verification.json`、`.platform-agent-e2e/public-gpu-verification.json`、`.platform-gpu-live/results-index.html`、`.platform-gpu-live/GPU-ACCEPTANCE.zh-CN.md`。

## 当前运行状态与费用

- 网站继续运行。公开 `/healthz` 已复核 `status=ok`、`auth_ready=true`；`generation_enabled=false`、`render_enabled=false`、`cloud_creation_enabled=false`。
- 单 GPU 交接 worker 已自然退出 0，未 OOM；host active barrier 为 false。旧生产 PG worker 仍保留 draining/fenced 历史状态，没有为新周期直接改写或清空记录。
- 两台旧 Lium pod 都由原唯一控制器销毁，供应商 removed statements 与 GET `/pods` 空列表核验完成。用户后来要求留一台时，两台已经销毁；没有为此自动重新租机。
- 两台最终供应商总费用 **US$3.490345**。内部 job reservation 不是额外供应商账单，不能重复计算；尚未结算的内部保留记录没有伪造为零。恢复测试仍承接原 US$50 授权和已发生费用。
- 旧 `h3` heartbeat 已确认是 PAUSED。没有新建自动化或新有限生产 cycle。
- 网站数据目前在加密 EBS，S3 存发布包；尚无已验收的异地数据库/媒体备份。删除本地项目不会停止 EC2/EBS/IP 等云计费。

## 已完成代码、尚未部署/实测的部分

有限生产自动扩容代码已实现并保持默认禁用：同一生产 PG、限定故事/预算/截止时间、最多两台单卡、真实冷等待、租赁意图先持久化、丢响应只对账、50 步完整资格、任务与资格 TTL 门槛、两 GPU 并行/CPU 结果收集串行、自然退出及供应商销毁证明后解除发布屏障。

新增代码在 `studio_platform/production_scaler.py`、`production_scaler_boot.py`、`drain_safe_runner.py`；host 入口为 `deploy/platform/gpu_scaler.py`；六镜头公网验收入口为 `deploy/platform/verify_public_scaling.py`。`prepare` 只创建验收故事结构；生成提交须明确 `submit-authorized`，默认无动作。

已经有隔离 PostgreSQL/SQLite 和假供应商合同测试；真实 TestClient 验证零 worker + 容量审批产生六个 waiting_capacity 任务且无租赁/重复提交。**正式生产 PG 自动 0→1→2 台、六个公网任务与最终自动缩容没有执行**，不能由此前隔离实测代替。新增 controller 的 1 GiB CPU 容器双子进程资源尚未实测。5090、B200 和单个任务的多卡张量并行也不属于本轮成绩。

新 helper 未安装到主机，新扩容代码未替换当前线上版本，没有执行新的 operator budget/capacity setup、旧 PG worker 退休或新租赁。

## Onboarding 优先事项

1. 正式账号首次进入：当前初始密码安全保存在 AWS Secrets Manager `/sixnine/platform/bootstrap-accounts`，还没有邀请链接、自注册或找回密码流程。onboarding 应确定邀请/登录/改密路径，不能把初始密码放到聊天或网页公共配置。
2. 从「我的故事」新建云故事；清楚区分浏览器草稿与云项目，提供首次空状态引导。
3. API Key：解释故事范围、权限、有效期、仅显示一次和撤销；给 Agent 下载 Skill 与可复制的无密钥示例，禁止让用户公开贴 Key。
4. 第一次生成：上传输入、用途、参数、预计等待/预算、队列状态、失败重试、结果采用；当前 GPU 关闭必须明确显示，不让用户以为提交即会开始。
5. Agent 修改后通知用户载入云端新版本，保留未保存草稿；多账户协同权限尚未实现，不能把隔离账号称为团队协作。
6. 自动写剧本、图片/音乐/Marble 供应商和 CPU 粗剪生产开放分别列为未接通能力，不能由已有 UI 推断服务可用。

## 恢复扩容实测的顺序

1. 明确继续后，先核对当前代码/线上版本、原费用、供应商实例列表和旧持久账本。不要重复使用 disabled offer 草案或过期价格作为可用事实。
2. 核验旧 pod 销毁凭证、旧容器自然退出及新鲜 PG 的所有 attempt/job 终结，再经 `WorkerControl.retire` 退休旧交接 worker、关闭旧池；不手改 worker/device 表、不清空费用。
3. 发布完整测试后的新 commit、独立批准 manifest，安装受审查 host helper。复核 EC2 IMDSv2 + hop limit 1（本轮已核对），不向 controller/GPU 分发 AWS role 凭据。
4. API profile 仍为 `lium/lium--rig-root`，Linux 运行时是同一身份的固定 AWS Secrets Manager ARN/version；加载到 host 内存后仅 stdin 交付 controller，不建项目 `.env`。
5. 先建立固定验收故事，再配置确切剩余预算、两个已核验 offer、3 小时 TTL、原生五秒 `124/24` 时长上限及完整审批。无默认自动补款/续期。
6. 仅启动一个生产 controller；公开提交六任务，收集两 GPU 重叠、1 GiB CPU 峰值、作品回写/账号隔离和最终销毁/账单证据，再决定长期服务策略。

保留用户原有 `SCALING.zh-CN.md` 未提交改动；本轮不覆盖其内容。
