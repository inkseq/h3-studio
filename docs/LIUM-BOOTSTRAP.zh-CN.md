# Lium 节点准备与资格验证

`studio_platform.lium_bootstrap` 是 CPU 控制面的可选组件。默认禁用；它不租机、不删除实例、不更改预算。`ScaleCoordinator` 先在数据库中保留预算并写入唯一 intent，再提交 Lium 租赁。CPU 上的控制器仅为已经确认的单卡实例准备固定版本 ComfyUI/H3、建立私有隧道、验证真实输出，然后接入原有 Fleet。

API 凭据由中央 `api_registry.load_api("lium", profile="lium--rig-root")` 在进程内加载。GPU 主机只收到 `bootstrap_cloud.py` 与 `model_manifest.json`；数据库、对象存储及 Lium 凭据留在 CPU 端。SSH 只读取已有私钥路径，不复制私钥到 GPU。

## 操作入口

```powershell
.venv/Scripts/python.exe -m studio_platform.lium_bootstrap
# 返回 disabled；不会读取配置、连接数据库或供应商。

.venv/Scripts/python.exe -m studio_platform.lium_bootstrap --mode run --config C:/absolute/operator-boot.json --intent-id <已记录的UUID>
```

运行前通过现有 `SIXNINE_DATABASE_URL` / `SIXNINE_DATABASE_URL_FILE` 指向拥有该 intent 的账本。Fleet 子进程还需要既有 `SIXNINE_DATA`、`SIXNINE_GENERATION_ENABLED=1`、`SIXNINE_EXECUTION_BACKEND=comfy-worker` 与明确的 `SIXNINE_EXECUTION_POLICY_FILE`。不能把实验数据库误写成生产验证。

配置是没有凭据值的本机运维 JSON：

```json
{
  "work_dir": "C:/absolute/durable-control",
  "source_dir": "C:/absolute/h3-studio",
  "ssh_key_file": "C:/absolute/existing-key",
  "known_hosts_file": "C:/absolute/known_hosts",
  "local_port": 18881,
  "configuration_id": "operator-approved-exact-config",
  "model_id": "MiniMax-H3-Base-BF16",
  "min_gpu_bytes": 96636764160,
  "enabled": true,
  "trust_first_host_key": false,
  "smoke_enabled": false,
  "fleet_enabled": false,
  "recipe_ids": ["h3-base-fl2va-v1"],
  "minimum_remaining_s": 1200
}
```

所有路径必须绝对；known_hosts 默认严格拒绝未知 SSH 主机。新租赁节点若选择首次信任，必须明确开启 `trust_first_host_key`，会把供应商 API 提供的公共 IP/端口对应主机公钥记入独立 known_hosts；这不是预先验证的硬件身份。每个 intent 绑定一个 CPU 端口，多个单卡节点分别使用例如 18881、18882。ComfyUI 只在 GPU 的 `127.0.0.1:8188` 监听，CPU 仅开放 loopback 隧道，不暴露公共 ComfyUI API。

## 资格层次

1. `ready_for_qualification` 只证明固定源码/权重大小和缓存 revision、GPU UUID/显存与 ComfyUI 进程就绪，**没有证明生成成功**。
2. `smoke_enabled=true` 明确提交一次原版 BF16 FL2VA：4 秒、480P、4 步、声音开启、CPU encoder、tiled video VAE，下载 MP4 与独立 FLAC、CPU 全片解码校验并保存 SHA256 与媒体参数。
3. `qualified` 默认仅覆盖这项 FL2VA 连通性测试。4 步样片不是推荐成片质量，也不是全部分辨率/时长/采样器、5090 或 VBench 验收。若明确配置 `recipe_ids=["h3-base-fl2va-v1","h3-base-ref2va-v1"]`，会额外运行独立 `ReferenceSmoke`：用本轮通过校验的 FL 视频/音频和自有程序化图片，真实输入 Ref2VA 的图片、视频、视频音轨、独立音频和图片 guide。它有独立持久收据、来源哈希、未知提交对账及输出解码验证；仅成功才允许 Ref 配方的 Fleet 启动。
4. 只有 `fleet_enabled=true` 且上述生成已通过才启动 CPU Fleet。执行策略仍由既有 `ExecutionPolicies` 限定，控制器不会自己生成一个声称全部能力已验收的策略。

96GB 实验配置使用显存下限 90GiB。若测试 5090，可显式下调 `min_gpu_bytes` 到至少 30GiB，同时使用不同 configuration_id；这不会自动量化、剪枝、更换模型、升级 GPU，且仍需真实成功证据。

## 幂等、故障与恢复

- 本机 receipt 与文件锁绑定 intent、pod、configuration、两份源码的哈希及本地端口。供应商未知创建结果只能对账，不能重新租机。
- GPU 端 bootstrap marker 先写入并 fsync 再启动进程。CPU 遗失响应后读取 marker/status，不再提交启动。新模板使用专用 `.venv --system-site-packages`，保持镜像已有 Torch/CUDA，避免 PEP668 系统包安装失败。
- 推理提交开始时间在 `/prompt` 之前持久化。HTTP 超时后依据唯一 tag 查询 queue/history，不重复发起同一测试。没有历史记录不等于安全重试。
- Fleet 启动意图在子进程启动前持久化。控制器重启遇到已启动/启动未知的 Fleet 返回 `fleet_recovery_required`，需先核对存活 worker 和任务；不会悄悄重新认领 GPU。
- 已启动 Fleet 不能就地扩大 recipe_ids；必须显式 drain 并使用新的配置标识。一个 Ref 混合输入成功仍不等于所有 guide 数量、输入边界、采样参数或音频质量均已测试。
- 终止/超时状态会请求 drain，不直接杀推理进程。`idle_probe` 仅为精确绑定的节点提供实时 Comfy queue 空闲证明；真正删除仍由持久 ScaleCoordinator 结合数据库任务、worker lease 与供应商事实执行。
- provider DELETE 响应、空列表或租赁预算都不是最终账单。须以精确 pod 的 removed statement 确认销毁，并保留实际费用或账单待到状态。
- 实际供应商 TTL 会按整数小时向下取整，不能把全局预算截止当成每台的实际到期时间。`LiumProvider.lifetime` 对齐本地创建记录并取得保守到期时间，控制器只能缩短账本 deadline。`WorkerControl` 对新 claim 和准备完素材后的 POST 均检查预计运行时间加 120 秒是否仍可完成；已提交任务继续对账/收集。

## 本轮验收目录

`.platform-gpu-live/` 是本机隔离真实实验（不入 Git，未连接 EC2 生产数据库），含 `approval.json`、`ledger.sqlite3`、`status.json`、bootstrap 收据、样片和最后报告。`live_control.py` 是本轮授权的运维控制脚本，默认禁用。第一次启动是操作员资格测试需求；第二台扩容必须来自实际已入队业务 jobs，先真实验收，再报告 1→2→0 是否发生，不能以离线测试或两台手动启动冒充自动扩容。

当前相关离线验证：`python -m unittest test_platform_lium_provider test_platform_lium_bootstrap test_platform_scaler test_platform_fleet test_platform_capacity -q`。离线测试不证明云端可用。
