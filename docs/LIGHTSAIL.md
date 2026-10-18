# Lightsail 部署与 CI/CD

这份文档保留的是早期独立 `server.py` / `web/` 服务的部署方式。当前映序与 H3 统一平台请使用 [deploy/platform/README.zh-CN.md](../deploy/platform/README.zh-CN.md)；两套数据目录、发布器和备份约束不同，不要交叉套用。本文件中的“仅设计”、SQLite与旧路径均按这一历史范围理解。

2026-10-04。当前为可验证的部署配置，未创建 Lightsail 实例、迁移本地数据或发布公网地址。
GPU 已销毁，本发布配置固定关闭生成。多机/API 调度另见 `SCALING.zh-CN.md`，尚未接入线上。

## 发布链路

私有 GitHub → push/PR 离线测试 → 构建并测试 CPU 镜像 → 在 main 手动选择 deploy →
同一个镜像经校验传到 Lightsail → 数据库一致性备份 → 暂停入口、切换版本 →
验证 CPU、密码认证和公网 HTTPS → 恢复入口。

首次上线先手动发布；日后稳定后可把 deploy 条件改为 main 合并自动发布。
生产发布串行执行，不取消进行中的发布。CI 没有云凭据、真实素材和生成请求。
GitHub environment 的权限/审批功能受账户计划影响，不假设已启用；手动触发本身不等于分支保护。

## 主机和配置

- 独立 Linux x86_64 CPU VM；推荐 Ubuntu 24.04，Python3、Docker Engine/Compose V2+ 已由管理员配置。
- 单个 app 进程，不增 Uvicorn worker；目前 SQLite 与队列不支持多个 app 实例。
- `/srv/h3-studio/data`：数据库、原始素材、归一化素材、结果、工作流，UID/GID `10001` 可写。
- `/srv/h3-studio/runtime`：非秘密运行状态，只读挂载；不放 API key 或 SSH 私钥。
- `/srv/h3-studio/config/site.env`：从 `deploy/site.env.example` 填实际域名。仅非秘密值。
- `/srv/h3-studio/incoming/<commit>`：每版镜像及部署脚本；`current` 指向成功版本。
- `/srv/h3-studio/backups`：发布前 SQLite 一致性快照。它不是媒体备份，也不是异机备份。
- Caddy 持久卷：TLS 状态，更新时不删卷。不执行 `docker compose down --volumes`。

示意初始化命令，由具备权限的运维在选定的目标 VM 执行；本轮未运行：

```sh
sudo install -d -m 0750 /srv/h3-studio/{config,runtime,backups,incoming}
sudo install -d -m 0750 -o 10001 -g 10001 /srv/h3-studio/data
# 仅 incoming 应授权给选定部署用户；不要递归把 data 改成部署用户。
```

域名 A 记录应指向实例固定 IPv4；如果没有配置 IPv6，不发布错误的 AAAA 记录。
实例开放 80/443；8844、8188、8189 不公开。SSH 仅对选定管理员/发布通道开放。
GitHub 普通托管 runner 出口 IP 不固定：实际接通需要固定出口 runner/受控私网，
或经过授权的临时精确防火墙规则。不要为了跑通 CI 将 SSH 对全网开放。
工作流目前使用标准 SSH/scp；**还没有配置或创建 runner、SSH key、GitHub Secrets 或防火墙规则**。

## 首次账户建立

公开网站强制 `H3_AUTH_MODE=password`；只允许 `superdan` 和 `supervan`，不开放注册。
密码保存在数据库中的 bcrypt 哈希；会话仍按用户隔离。不能推送哈希、数据库或 session 到 GitHub。
本地旧版的 username-test 模式仅为兼容本机测试，不会作为公网容器的认证方式。

首次需要在目标 VM 已加载待发布镜像后，由管理员在交互终端设置两位用户密码；
命令仅传用户名和数据库路径，密码由隐藏输入读取，不能当命令参数或环境变量传入。
具体参数见 `python tools/manage_users.py --help`。参考执行结构：

```sh
sudo docker run --rm -it --network none --user 10001:10001 \
  --mount type=bind,src=/srv/h3-studio/data,dst=/data \
  h3-studio:<commit> python tools/manage_users.py --data-dir /data --username superdan
# supervan 同样执行一次。不要使用 shell history/CI log 存放密码。
```

首次镜像由 CI 构建并传入后，若账户未设置，发布会在停服前拒绝。
设置完成后再运行该次 release.sh。已有密码账户更新版本不必重新设置。
更改密码会撤销该账户现有会话；暂未实现邮件找回、公开自注册、MFA 和自动邀请。

## GitHub 设置

建议私有仓库 `h3-studio`。仓库 `production` environment 设置：

| 类型 | 名称 | 用途 |
|---|---|---|
| Variable | `LIGHTSAIL_HOST` | 已选实例 IPv4 或管理 DNS 名称 |
| Variable | `LIGHTSAIL_SSH_USER` | 已选部署用户 |
| Secret | `LIGHTSAIL_SSH_KEY` | 专用于这台 CPU VM 发布的 SSH 私钥 |
| Secret | `LIGHTSAIL_KNOWN_HOSTS` | 经独立核验的服务器 host key；不能在发布时盲信 ssh-keyscan |

部署用户需要目标 incoming 写权限及发布命令所需的非交互 sudo。该权限可控制应用主机，
仅用于可信私有仓库的 main 发布；不要给 fork PR、第三方 workflow 或共享 GPU 控制使用。
这些是 SSH 发布配置，不是模型服务 API profile。现有 Lium key、Windows 中央 DPAPI
库和 `.env` 均不进入 GitHub/服务器。CPU 网站运行不需要云租赁凭据。

## 数据与回滚

普通更新不复制、重置或清空 DATA。发布前有 active job 时拒绝发布，不假装已实现在线 drain。
GPU 恢复后需先增加完整 drain/对账流程，才能对正在生成的服务自动发布。

失败时停止代理；若旧版本存在且 SQLite schema 未变，则等待旧 app 健康后恢复代码。
schema 改变时保留现场、代理保持停止，人工检查一致性快照和迁移记录；不自动覆盖数据。
这不是零停机发布。发布中有短暂维护窗口，尚未在真实 Lightsail 上演练故障回滚。

本机历史素材尚未迁入：上传 metadata 里有 Windows 绝对路径，不能直接复制 DB 就称可用。
迁移必须做 SQLite 一致性备份、媒体完整复制、受控路径转换、清除旧会话，并对两用户下载/引用验收。
本地文件继续保留，不能把云站空数据库当成历史数据迁移完成。

## 当前上线缺口

1. AWS 账户与区域、实例/预算、域名尚未指定。本机 default 配置端点是第三方存储，不是已核验 AWS 账户；未复用它创建云资源。
2. 两个真实密码未建立；不会自动生成并写入聊天。
3. 发布 SSH 通道、主机身份、GitHub environment 与网络入口未接通。
4. 历史数据迁移/备份目标、公网验收与多 GPU/API 路由尚未执行。

参考：[GitHub 部署控制](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/control-deployments)、
[Lightsail 防火墙](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-editing-firewall-rules.html)、
[Caddy 自动 HTTPS](https://caddyserver.com/docs/automatic-https)。
