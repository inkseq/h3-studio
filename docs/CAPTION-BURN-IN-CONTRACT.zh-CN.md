# 可选中文字幕烧录 · render v3

2026-10-04。本轮纯规划/权限检查已经实现；最终媒体与字体验收以 renderer 的实际测试记录为准，不表示公网或 GPU 已开启。

## 用户入口与不可变契约

`POST /v1/render-plans` 新增 `burn_subtitles: boolean`，默认 `false`。不接收字幕文件、URL、字体路径、滤镜或任意 ASS。真实字幕来自当前 owner/project 的服务端文稿 `journey.captionTracks[chapter_id]`。

新计划的 `request.render.version=3`，镜头选段和声音沿用 v2，新增：

```json
{"subtitles":null}
```

或：

```json
{"subtitles":{"preset":"shortdrama-zh-v1","cues":[{"id":"cue-one","start_frame":3,"end_frame":46,"text":"欢迎来到雨夜。\n我们终于见面了。"}]}}
```

烧录为永久画面，播放器不能关闭。SRT 仍独立导出，静音也能烧字幕；不做听写、翻译或自动同步。手动确认只是用户对这一版文字/时间的确认，系统不能据此证明对白同步正确。

`caption_server.caption_signature` 独立重建与前端一致的 `{basis,soundMode,audio,cues}` 对象，与 `confirmedSnapshot` 比较；不能只信 `confirmed=true`。已用真实 canonical JavaScript 的返回值核对复杂镜头选段和生成音轨依据。

烧录最多500条，每条显式1–2行，每行1–18个 Unicode 码点；空行、重叠、出章、缺确认均阻止预检。原稿保留，不截断、不自动改字、移动时间或插入换行。仅 CRLF→LF 做等价换行规范化。烧录正文拒绝 ASCII `{}`、反斜杠、`<>` 和除 LF 外的控制/格式字符；这些限制不删除已有草稿或改变 SRT 导出。

原秒数向24fps内对齐：`ceil(start*24-epsilon)`、`floor(end*24+epsilon)`，结束不包含该帧；不足1帧阻止。Unicode U+2028/U+2029 行/段分隔符也拒绝，不能绕过最多两行。预检返回 `timeline.subtitles` 原文与实际帧/秒范围，用户确认后才入队。

烧录开启时，来源 hash 包含完整字幕轨道（正文、确认快照及保存的样式字段）和固定 preset；文字/时间/确认变化后旧计划失效。不开启时保持旧时间线 hash，不因无关字幕编辑失效。运行任务仍使用提交时的不可变文稿，不偷偷更新已经入队的字幕。

## 固定版式与执行边界

`shortdrama-zh-v1` 使用固定中文字体、白字黑描边；字号约 `floor(min(width,height)*0.045)`，左右8%和底12%留边。renderer 用真实受信字体再量测每行和描边，溢出阻止，不能静默缩字、换行或显示缺失文字。此安全区不保证避开所有社交平台的界面。

服务端在受控 attempt 目录创建固定 ASS 文件；样式、文件名及字体配置来自程序/操作员，不来自字幕文字。只把经纯文本检查后的内容写入 ASS 的文本字段，把已经明确的换行写成服务端 `\N`。固定 ASS 通过 libass 烧录，额外 CPU 编码仍受当前单槽、取消、心跳、1800秒期限及媒体/磁盘配额约束。建议只对拼接后的章节做一次烧录；其后仍核验帧数、尺寸、时长、是否静音和完整解码。

新计划精准绑定 `cpu-render-v3`。保留 v1/v2 的历史 drain 配置与工作目录，旧 worker 不能接 v3；升级前先收尾已有任务，不把主机登记改名当成任务已结束。

## 已查字体与官方依据

本机 Windows `ffmpeg -version` 为7.1.1完整构建，实际列有 `ass`/`subtitles` 和 `--enable-libass`。现有 `C:\Windows\Fonts\msyh.ttc`、`msyhbd.ttc`、`simhei.ttf`、`simsun.ttc` 已通过文件元信息确认。本次未复制字体、安装依赖或下载资源。Windows 的本机视频位图渲染可与字体再分发区分；不能因此把 Windows 字体复制到 Linux 镜像。[Microsoft 字体说明](https://learn.microsoft.com/en-us/typography/fonts/font-faq)

生产 `Dockerfile.platform` 已加入 Debian Bookworm `fonts-noto-cjk=1:20220127+repack1-1` 与 fontconfig，选择 `Noto Sans CJK SC`；包官方安装体积约91MB，含常规/粗体中文字体。声明依赖不代表目标生产镜像已构建/上线，仍须对该镜像做真实字体与 libass 验收。[Debian 包与大小](https://packages.debian.org/bookworm/fonts-noto-cjk)

Noto Sans CJK 使用 SIL OFL 1.1，随应用再分发需保留版权和许可证；生成的视频不需要因此改用字体许可证。[字体的实际许可证](https://github.com/notofonts/noto-cjk/blob/main/Sans/LICENSE)

FFmpeg 官方文档明确 `ass`/`subtitles` 使用 libass，支持受信 fontsdir；Unicode 自动换行还依赖 libass/libunibreak版本，因此本版使用显式有界两行，不把自动换行当成可用能力。[FFmpeg 7.1.1 官方文档](https://raw.githubusercontent.com/FFmpeg/FFmpeg/n7.1.1/doc/filters.texi)、[libass 格式说明](https://github.com/libass/libass/wiki/ASS-File-Format-Guide)

## 验收与代价

最低验收：同一静音片烧中文两行；有音轨片仍保持原音频；一帧字幕只出现在正确帧；特殊字符/路径/ASS 注入拒绝；确认后编辑字幕使旧计划不能首次提交；无字幕计划不受编辑影响；三个 CPU 配置相互隔离。字体缺失、字体量测超宽、烧录失败须明确失败，不输出假成功。

新增成本是 CPU 重编码和中间文件，以及字体包镜像体积。没有额外云端生成调用；实际速度需在目标 CPU 上测量，不在这里推断。没有新增自动字幕模型或模型下载。
