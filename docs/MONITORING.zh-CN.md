# 监控与日志总览

这一页回答三个问题：出事时谁会提醒你、去哪里看、该做什么。技术细节见 [deploy/platform/ALERTS.md](../deploy/platform/ALERTS.md)（服务器告警）和 [OPERATIONS.zh-CN.md](OPERATIONS.zh-CN.md)（任务诊断）。2026-10-27 整理。

## 去哪里看

| 想知道什么 | 去哪里 | 备注 |
|---|---|---|
| 网站报错（用户在页面上碰到的错误） | Sentry 项目 `inkseq-design`，筛选 environment=production | 前端已接入 |
| 服务器报错、controller 卡住的告警、用户反馈 | Sentry 项目 `inkseq-api` | 后端地址在 #655 里接入；接好之前这里是空的 |
| 有多少人来、走到哪一步离开 | PostHog（美国区项目 683580），Web analytics 和漏斗 | 已关闭 IP 记录；不录屏 |
| 生成快不快、失败多不多、钱花在等机器上还是生成上 | Grafana「Inkseq 生成概览」：https://tallgodwit3049.grafana.net/d/inkseq-generation-overview | 目前只有 production 的数据 |
| 服务器 CPU、内存、磁盘、备份是否成功 | 告警邮件 + AWS CloudWatch 告警（ap-southeast-1，`Inkseq-*`） | 见下一节 |
| AWS 账单 | AWS Budgets `inkseq-aws-monthly`：每月 $200，实际花到 50%、80% 或预测超出时发邮件 | GPU 费用不走 AWS，由应用内每日上限和测试花费告警管 |
| 某个任务为什么卡住 | 管理后台 /admin 的「总览」和「任务」；或服务器上跑 `python -m studio_platform.diagnostics`（只读） | |

## 告警邮件是什么意思，该做什么

服务器上每分钟检查一次，有问题发到运维邮箱（SNS `sixnine-{production,staging}-operator-alerts`）。同一个问题不会重复轰炸，恢复后会发一封恢复通知。

| 邮件里的代码 | 意思 | 先做什么 |
|---|---|---|
| `gpu_runtime_exceeded` / `gpu_idle_stop_overdue` | GPU 机器跑太久，或闲着没关 | 管理后台看机器；确认没有在跑的任务后停机 |
| `test_spend_80` / `test_spend_100` | 测试花费到了上限的 80% 或 100% | 停掉测试机器，查哪个测试在花钱 |
| `job_stuck` / `job_repeated_wake` / `terminal_reservation_stale` | 任务卡住、反复被唤醒，或预留没释放 | 看 /admin 任务页；不要直接重发，先核对原任务（见 OPERATIONS） |
| `generation_failure` | 生成失败变多 | Grafana 看失败出在哪个阶段 |
| `health_down` / `http_5xx_rate` / `host_process_unhealthy` | 网站打不开、5xx 错误变多，或服务进程不健康 | 看服务器上 `docker ps` 和 app 日志 |
| `host_cpu_sustained` / `host_memory_sustained` / `host_disk_80` / `host_disk_90` | 服务器资源不够 | 磁盘先看日志和备份目录；不要删用户素材 |
| `business_backup_failed` / `business_backup_missing` / `business_backup_unobservable` | 每日备份失败、到 18:30 UTC 还没有当天备份，或状态读不到 | 看 #655 的恢复说明；最后一份好备份在哪天要写清楚 |
| `retention_service_failed` | 发布版本清理失败 | 不影响用户；交给 Codex |

CloudWatch 另有三个告警，盯的是告警系统自己：`MonitorHeartbeat`（检查程序还活着）、`SnapshotHealthy`（运营账本快照，**不是**业务备份）、`DeliveryHealthy`（告警邮件发得出去）。

## 日志保留

- 容器日志（app、controller、数据库）：每个容器只保留约 30 MB（10 MB × 3），旧的会被覆盖，没有集中存档。要查几天前的事，看 Grafana 的 controller 日志和 Sentry。
- 告警程序和备份：`journalctl -u inkseq-alert-monitor.service`、`journalctl -u inkseq-business-backup.service`。

## 定期要看的

- 每 2 小时：Claude 只读巡检生产 GPU（排队时间、闲置机器、卡住的任务），有问题发到 #193。
- 每周：PostHog 漏斗和 Sentry 新错误。生产有真实用户后，Claude 在小结里提醒。

## 还没做的

- 后端 Sentry 接入（#655）。
- Grafana 告警规则：现在只有看板，告警仍然靠服务器邮件。
- staging 的生成指标：Grafana 里只有 production。
