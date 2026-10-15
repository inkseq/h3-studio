# inkseq.com 域名切换契约

这是 #213 的部署契约，不是线上验收回执。安装前必须绑定同一批已审阅产物与受保护配置；历史 release 文件和原生 GPU 包不在原地修改。

应用固定 `PUBLIC_ORIGIN=https://inkseq.com`，迁移别名为 `PUBLIC_ORIGIN_ALIASES=https://www.sixnine.art,https://sixnine.art`。只接受明确的 HTTPS Host；浏览器写入的 Origin 必须匹配本次请求的可信 Host。`SIXNINE_PUBLIC_ORIGIN` 仍可单独读取，若与 `PUBLIC_ORIGIN` 同时设置而不同则拒绝启动。公开服务不接受本地预览或通配符例外。

`INKSEQ_LEGACY_REDIRECT` 是仅交给 Caddy 的非秘密 `site.env` 字段，只允许 `0` 或 `1`；缺省为 `0`。阶段 A (`0`)：新 apex 和旧 www 都代理同一个 app；旧 apex 保持 308 到旧 www。`www.inkseq.com` 始终 308 到新 apex。阶段 B (`1`)：旧 apex/www 对应的完整 URI 308 到新 apex，保留 POST 方法和请求体。不得在新域名 TLS、普通登录、原 PAT/Agent 和原有媒体读取通过之前切到阶段 B。浏览器 Cookie 是 host-only；用户到新域名需登录一次，不迁移 Cookie 或重建账户。

安装新的 `check_config.py`、`release.py`、`frontend_release.py` 等受信主机辅助程序时，使用固定独立目录 `/opt/sixnine-release-origin-v1` 和明确哈希，保留原 `/opt/sixnine-release` 及当前/上一个 release。未改的配套辅助程序也按原审阅字节共置，避免新旧 import 混用。现有两个 SSM 文档只增加版本 2，固定调用新目录的 `deploy_approved.py` 和 `deploy_frontend.py`；只保留原 `Commit` 40-hex `ENV_VAR` 参数，不扩大 IAM ARN、命令参数或任意 shell 权限。版本 1 留作配对回滚。新验证器同时识别保留的旧单域名配置和精确的新域名配置；它不会修改任何运行实例。新原生 GPU 源包不因域名而重建。

切换前记录当前和上一个 release、原 `site.env` 原始字节/哈希、当前 Caddy 配置与容器身份、migration active/paths 哈希及前端指针。阶段 B 变更只在单独批准的既有主机操作中把非秘密 flag 置 `1` 并以相同审阅镜像重建/验证 Caddy；不修改发布目录中的 Caddyfile。失败时先恢复原 `site.env` 字节及其 flag，再用原匹配的辅助程序恢复原 app/Caddy release 和前端指针；必须分别验证旧域名 HTTPS/登录、API 和容器身份。单独换回应用镜像不是完整回滚，现有 migration helper 的失败保持也不等于自动恢复域名配置。

`staging.inkseq.com` 留给 #195 的独立环境，不作为本生产过渡的别名。数据库、R2 对象、租户、API Key、Agent grant 及其期限保持不变；R2 CORS 仅按独立审阅的操作增加新精确来源，不改变公开访问或对象凭据。
