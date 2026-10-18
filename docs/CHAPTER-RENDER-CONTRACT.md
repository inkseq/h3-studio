# CPU章节粗剪契约：当前格式 v3，兼容历史 v1/v2

2026-10-04。本功能是已有镜头的CPU粗剪，不调用视频生成模型、GPU或外部生成API，不是专业剪辑/混音系统。实现为 `studio_platform/render_backend.py`，CLI为 `studio_platform/render_cli.py`。默认关闭；本轮测试只用独立临时目录中的合成小视频与音轨。

## 计划与数据

后端 `kind=cpu-render`；recipe=`chapter-roughcut-v1`、model=`sixnine-chapter-roughcut-v1`、pool=`cpu-render`保持业务配方身份。当前新计划为`render.version=3`，精确匹配`configuration=cpu-render-v3`；历史`version=1/2`计划仍分别精确匹配`cpu-render-v1/v2`。这些是本站处理标识，不是供应商model ID。不能把旧worker改成无条件接受新选段或字幕，也不修改已提交计划。

API负责登录用户、项目/章节/素材权限与来源快照；客户端不能提交URL、对象key或本地路径。Worker后端仍检查对象key所属owner、SHA256、大小和实际解码内容。`job.request` 的规范化结构为：

```json
{
  "recipe_id": "chapter-roughcut-v1",
  "request": {
    "model": "sixnine-chapter-roughcut-v1",
    "duration": 2,
    "generate_audio": false,
    "export_crf": 18,
    "render": {
      "version": 3,
      "shots": [
        {"shot_id": "shot-a", "source_id": "source-a", "frames": 24, "source_start_frame": 24},
        {"shot_id": "shot-b", "source_id": "source-b", "frames": 24, "source_start_frame": 0}
      ],
      "audio_tracks": [],
      "subtitles": null
    }
  },
  "output_spec": {"width": 1280, "height": 720, "fps": 24, "frame_count": 48},
  "sources": {
    "source-a": {"kind": "video", "object": {"key": "owners/owner/assets/asset-a/video.mp4", "size_bytes": 1234, "sha256": "64位小写十六进制摘要", "content_type": "video/mp4"}, "simulation": false},
    "source-b": {"kind": "video", "object": {"key": "owners/owner/assets/asset-b/video.mp4", "size_bytes": 1234, "sha256": "64位小写十六进制摘要", "content_type": "video/mp4"}, "simulation": false}
  }
}
```

示例为结构说明，摘要/大小不是可运行素材。API还可存client_ref、server_source_hash、simulation、render_blockers等业务元信息。`sources` 必须恰好等于本时间线上使用的来源集合；source ID只参与字典查找，绝不成为磁盘文件名。视频支持MP4/MOV；独立音频支持WAV/FLAC/MP3。`object`可附带受控存储ObjectInfo的provider、etag、version_id。

## 画面与声音语义

- 镜头数组顺序就是输出顺序；v2从`source_start_frame/24`秒开始，截取`frames/24`秒。历史v1没有入点字段，仍从0开始。原视频先规范化时间起点和24fps，再按准确帧号裁剪；没有使用容易受关键帧位置影响的流复制裁剪。源短则拒绝，不补尾帧、不循环、不慢放填时长。实际源时长与输出帧数均重新核对。
- 编辑字段为`shot.data.selectedVideoRange={assetId,fileId,cloudAssetId,cloudArtifactId,start,end}`，三种文件ID没有时显式null；范围绑定采用素材实体与原文件身份。换源后可保留旧选段草稿，但预检阻止应用到新视频。无此字段时沿用从0取镜头时长。入点向上、出点向下对齐24fps，预检同时展示原区间、实际入点、成片采用的终点与未使用尾段。用户明确选择保持镜头时长或按选段长度调整时长，服务端不偷偷修改文稿。
- 帧裁剪使用固定的`setpts`、`fps`、`trim`滤镜，先裁后缩放；表达式不含用户文字。基于[FFmpeg trim说明](https://ffmpeg.org/ffmpeg-filters.html#trim)：裁剪帧号与时间戳是不同语义，裁剪后需要重置时间戳。本轮真实30fps三色片段裁取中间一秒后核对24帧与首尾像素；这不是所有VFR/HDR素材的全面剪辑兼容证明。
- 24fps、H.264、yuv420p、CRF18；保持源比例，居中加黑边，输出像素宽高比1:1。无转场、自动构图或色彩统一；v3可明确选择下述人工确认字幕烧录。每段显式输出CFR24，避免拼接容器缺少最后一帧时长造成累计漂移。
- **视频内已有原声始终去掉**。无独立音轨时MP4完全没有音轨，不自动继承H3原声。
- 每条独立音轨为 `{source_id, timeline_start, source_start, source_end, gain}`，时间单位秒；播放源的指定区间，在章节时间线上放到明确起点，gain范围0–1。时间偏移以32kHz采样点取整。源短或规范化轨道超过成片长度时后端拒绝；API可先给用户明确的尾部裁剪提示后提交有效区间。
- 音轨重采样为32kHz stereo，采用FFmpeg声道转换、再应用gain；叠加不自动按轨道数归一化，最终使用固定0.95峰值限幅保护。未占用区间填静音。这不是响度均衡或专业母带处理。输出MP4含AAC，并额外保存独立FLAC；不会凭编码成功宣称听感优秀。
- 任何实际使用来源的`simulation=true`，整片每一帧都覆盖固定`SIMULATION`顶栏。水印由Pillow的内置字体生成；不用用户动态文字拼接filter，也不会缺字库就静默省略。该标记依赖服务端来源元信息，不是对任意外部上传素材的真伪检测。

## 复用候选视频的配套生成声音

v2纳入此能力，v3继承相同声音语义。前端必须同时检查`capabilities.chapter_render.generated_audio=true`，否则不向旧服务器提交生成绑定轨；不能假设旧后端会识别新编辑字段。规范化执行请求仍使用上面的五字段音轨结构，`generatedFrom`不作为worker的授权依据，也没有新增数据库表。

项目编辑轨可增加以下关联，`assetId/fileId`指向独立音频实体，不能指向MP4：

```json
{
  "assetId": "audio-entity", "fileId": "cloud_artifact_audio-id",
  "shotId": "shot-a", "role": "generated", "offset": 0,
  "gain": 0.7, "muted": false, "needsReview": false,
  "start": 2, "end": 5,
  "generatedFrom": {
    "jobId": "job-id", "videoEntityId": "video-entity",
    "videoArtifactId": "video-artifact-id", "audioArtifactId": "audio-artifact-id"
  }
}
```

`generatedFrom`恰好四个ID；服务端通过真实artifact→job账本确认两者均已验证、同一成功任务、同tenant/owner/project。浏览器的`sourceJobId`和四个ID都只是待核验声明。配套音频必须是独立FLAC产物；只有MP4、普通上传视频、其它任务音频或缺失FLAC均不满足绑定条件，**不提取MP4原声**。角色`generated`仅展示用途，不授予权限。

每个镜头最多一条启用的生成绑定音轨；其`offset`必须为0。`start/end`是UI投影，服务端从当前选中视频的已核实入点和镜头帧数重新计算。相同视频修改选段或镜头时长，声音随之变化；替换或删除采用视频、改变artifact身份、音频原文件变化，未静音关联轨阻止粗剪。`needsReview=true`也阻止启用轨，清除此标记不能绕过实际ID核验。旧关联草稿仍可保存以便撤销；用户可重新采用、静音或明确移除`generatedFrom`变回独立手动音轨。静音方案不读取这些音轨；其它声音方案若所有轨均静音，需明确切静音或添加可用声音。

设视频入点为整数帧F，镜头长度N帧，此镜头前累计S帧，计算`sample(frame)=floor((frame*32000+12)/24)`。源音频区间为`[sample(F),sample(F+N))`，章节起点为`sample(S)`，存为这些整数除32000的秒数。先对每镜头`floor(seconds*24+0.5)`得到帧数，再累计整数帧，绝不反复累加每帧1333采样点。每个边界舍入误差最多半个采样，独立映射的片长差最多一个采样，不产生逐镜累计漂移。这是导出数值对齐，不是H3唇音同步或浏览器实时预览精度保证。

v2在混音前把每轨选段先实际解码、重采样32k stereo，按`atrim=start_sample:end_sample`截成独立受限FLAC，核验**未填充片段**的真实采样数；源短一采样也拒绝，不让后面的`apad`掩盖缺失声音。这些临时片段计入原4GiB attempt和8GiB根容量、原1800秒FFmpeg阶段时限，没有另外放开大小或启动进程常驻服务。历史v1保留原按秒裁剪流程。依据[FFmpeg atrim官方说明](https://ffmpeg.org/ffmpeg-filters.html#atrim)，采样参数直接计数解码采样；裁剪后显式重置PTS。旧产物账本的`duration_s`曾采用任务请求时长，预检可能无法识别少于0.1秒的历史音频不足；worker仍重新检查实际内容，失败不发布成片。

声音和音乐共享32轨限制，沿用各轨gain及固定限幅；例如声音0.7、音乐0.2是可调整起点，不代表自动响度匹配。v2合成色块/分段标记音频验证已覆盖2秒入点取3秒、分数帧脉冲位置、真实声音/音乐gain、短FLAC和伪长时长拒绝。HTTP测试覆盖同任务通过、跨owner/project/tenant/job、未验证/错误类型/非成功任务拒绝。纯计划测试覆盖同源跟随、换源/删源待确认、撤销恢复、静音/解除关联、缺FLAC、重复绑定和32轨边界。没有生成或评价任何真实H3声音。

## v3可选中文字幕烧录

HTTP粗剪预检仅增加`burn_subtitles:boolean`，缺省false。用户明确开启时，服务端从当前项目字幕轨重建人工确认依据，并验证镜头、选段、声音和字幕原文没有变化；不接受浏览器传ASS、filter、字幕文件URL或字体路径。规范化`render`固定增加`subtitles`：关闭为null，开启为下例结构：

```json
{"preset":"shortdrama-zh-v1","cues":[
  {"id":"cue-one","start_frame":12,"end_frame":24,"text":"月光落在窗前\n她终于回来了"}
]}
```

最多500条，时间范围为24fps整数帧的左闭右开区间，每条至少1帧，按时间排序且不重叠、不得越过章节；原稿秒数向内对齐帧边界并展示实际时间。最多2行显式换行，每行1–18字符；不删字、不自动换行、不修改字号来掩盖溢出。CRLF原稿由planner规范化为LF，执行层只接受LF；大括号、反斜杠、HTML角括号、控制符和Unicode行/段分隔符在开启烧录时拒绝，原字幕草稿/SRT仍可保留编辑。缺确认或来源变化需要重新核对，不默认为已确认。

固定样式：简体中文无衬线字体，白字黑描边，底部居中；字号`max(10,floor(min(width,height)*0.045))`，左右各8%、底部12%安全区。renderer使用真实字体量测行宽并检查字形与两行高度，溢出或字体不支持的字符明确拒绝。没有用户字体上传、自定义ASS样式、动画、卡拉OK、自动识别或翻译。底部字幕不会盖掉原有顶部SIMULATION标记。

Linux要求既定`fonts-noto-cjk=1:20220127+repack1-1`与fontconfig，family精确`Noto Sans CJK SC`，文件`/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc`，TTC face由family检索（已核实为2），禁止静默回退另一字体。FFmpeg需要libass的`ass` filter。`XDG_CACHE_HOME=/tmp/sixnine-font-cache`位于容器可写tmpfs，发行包版权文件保留。Windows仅操作员显式`--subtitle-font-profile windows-yahei`引用已有`C:/Windows/Fonts/msyh.ttc`/`Microsoft YaHei`，不复制字体到项目、镜像或结果。默认profile为`noto-cjk`；不接收任意font-dir、文件路径或URL。

CLI v3在登记ready前调用`backend.assert_subtitle_ready()`，只探测字体/FFmpeg，不生成视频。直接使用backend且`subtitles=null`时无需字体；开启烧录时在复制源之前重新检查，提交执行前还会复核。缺字体/滤镜不会生成无字成片。旧v1/v2后端和队列保持原契约，不接收v3字幕字段。

renderer仅生成固定`captions.ass`文件，UTF-8 BOM、固定Style/Events；正文经过共享纯文本校验，只把显式LF转换成服务端控制的`\N`。滤镜恒定`ass=filename=captions.ass`，FFmpeg cwd固定为受控attempt目录，因此用户文字和Windows路径都不进入滤镜转义层。ASS格式依据[libass官方生成指南](https://github.com/libass/libass/wiki/ASS-File-Format-Guide)。帧时间转为向下取整的ASS百分秒，24fps单帧间隔大于这一步精度损失；真实逐帧测试验证开始帧包含、结束帧不包含及仅1帧显示。

字幕在视频拼接后单独H.264烧录，再与已有明确音轨合并；静音模式也可烧录且不创建音轨。关闭时不运行字幕滤镜。字幕会增加一次有界视频编码及暂存`captioned.mp4`，仍受原512MiB单输出、4GiB attempt、8GiB根缓存与1800秒FFmpeg阶段限制。collector现有二次编码仍存在，暂不宣称无损或最佳速度。失败、超限或字形缺失不发布成片；unknown重启仍核对原attempt，不重渲染。

`test_platform_render_subtitles.py`9项在Windows真实微软雅黑和Linux固定Noto/FFmpeg5.1.9环境均通过；Linux以UID10001、只读根、无网络、受限tmpfs运行，仅挂载测试与代码，不挂载用户数据。包括真实两行中文字形、安全区、单帧时机、不同汉字不是相同缺字框、静音和声音共存、SIMULATION保留、注入/非法范围拒绝、缺字体仅阻烧录、CLI缺字体不得注册v3、已完成attempt恢复不重跑。这是合成色块与标记音频的功能验证，不是ASR、真实H3生成或人工听感评测。

## 服务上限与费用

| 限制 | 当前值 |
|---|---|
| 镜头 / 独立音轨 | 1–50 / 0–32 |
| 成片帧数 / 时长 | 1–14400 / 最长600秒 |
| 尺寸 | 偶数；每边256–1280；总像素≤1280×720 |
| 单输入 / 去重输入总量 | 512MiB / 2GiB |
| 单输出 / 单attempt工作目录 | 512MiB / 4GiB |
| 渲染超时 | 总FFmpeg阶段默认1800秒，不能配置超过1800秒 |
| 输入准备超时 | 独立阶段默认1800秒；逐块检查，距上次心跳达5秒时续租/检查取消；单次阻塞读取由store传输超时另行约束 |
| FFmpeg CPU | 编码2线程、复杂filter线程1；独立CPU worker单slot |

以上是本站当前工程边界，不是H3或供应商限制。FFmpeg `-fs`仅是编码期截止，可能越过最后一个packet；进程期间还检查输出/工作目录大小，完成后严格验大小、帧数和时长，超限不发布。它不是操作系统磁盘硬配额；生产仍需要磁盘容量监控和隔离。

`actual_cost_resolver(job, task_id)=0` 表示本站当前没有向用户另收该CPU粗剪费用，**不表示CPU、存储或流量没有成本**。它不使用GPU报价，也不占用GPU实例/GPU数量门槛。

## 提交、恢复与取消

`prepare(job, tag, store, heartbeat)`创建唯一attempt目录，先记录请求身份，按服务端生成的文件名读取不可变输入，验证实际内容。返回tag、身份摘要和仅驻内存的heartbeat；不把callback写入磁盘。

`submit(prepared, tag)`先原子保存提交意图，随后同步执行有界FFmpeg。结果文件fsync后再保存成功状态。再次submit同一attempt一律不重跑。`poll/reconcile`对已成功结果重新核对元信息和摘要；失联/崩溃后若只有提交意图，保持unknown，**不凭文件存在或进程PID自动再渲染**。原输入与中间证据保留，不自动删除。

`fetch`只返回受控attempt文件路径，且重新核对job身份；这些内部路径不进入公网响应。`WorkerRunner._collect`再做整段解码、帧数/音频验证并上传产物；当前会二次编码，属于已知性能/画质取舍，后续才能在完整验收后优化成安全复用。

`cancel`只标记当前进程实际持有的attempt；FFmpeg轮询中终止自己持有的进程句柄。不会用旧PID杀进程，也不会中断别人的GPU或全局队列。Worker heartbeat可读取该job取消状态并调用此方法，通常在下一次5秒心跳内处理。服务器丢失所有权后只能核对unknown，不能宣称已停。

CPU state根默认8 GiB逻辑容量上限：在复制输入前持久预留每个活动attempt最多4 GiB，已终结attempt按仍保留的实际文件计入；重启继续计数。只扫描自身已知的一级attempt目录，遇到链接或未知目录安全拒绝，不递归扫描其它资产、不自动清理。CLI可显式设置`--max-state-gib`（默认8，最小4）。这不是OS文件系统硬配额；仍需保留期限、独立归档和授权清理策略，不得直接删除唯一产物来释放空间。

## 明确启动与验收

CLI只在`SIXNINE_RENDER_ENABLED=1`且提供`--enabled`时工作；API与worker需要同一数据库、同一受控store配置。示意命令中的目录和主机ID必须由部署者明确选择：

CLI默认`--contract-version 3`。升级时旧v1/v2 worker应先收完原计划/未知提交再退役；若需要用新二进制恢复历史队列，可另行显式启动`--contract-version 1`或`2`的旧任务收尾进程，使用原正确身份与受控工作目录。各版本工作槽仍按configuration精确匹配，不能把任一类型的队列改写成另一版本；运行/收集期间的状态和费用证据不丢弃。

```text
python -m studio_platform.render_cli --enabled --worker-id cpu-one --instance-id stable-cpu-host --work-dir /srv/sixnine/render-work --confirmed-idle
```

`--confirmed-idle`表示已核对该CPU主机无待核对/遗留进程；不能为掩盖unknown随便使用。`--once`只跑一轮并退出drain。默认LocalObjectStore；显式R2复用中央 `load_storage_credentials`，其他provider需经审阅的runtime adapter，绝不回退默认AWS账号。没有GPU发现、云创建、供应商生成或默认服务启动。

`test_platform_render_backend.py`本轮20项通过：真实小视频顺序、黑边、24fps/帧数、原声剔除、独立音轨时间和gain、FLAC、全程模拟标记；以及来源权限/摘要/短片拒绝、输出上限、超时杀进程、取消、丢响应恢复、提交后崩溃不重复、默认关闭和CPU CLI空队列。包括缓存预留重启计数、未知目录不递归、慢素材读取超时与准备期取消。测试范围不是600秒高负载基准，也没有评测专业音质或GPU生成。

## 产物配额与上传恢复

`ArtifactWriter.begin_staging`在fetch/FFmpeg前持久预留共享owner/tenant容量：每个预期角色先按raw下载+verified暂存+未来对象三份单文件上限预留（默认1.5 GiB/角色）。此时失败或崩溃仍保留receipt和容量，不因lease过期释放。实际文件验证完成后，`prepare`在同一事务将预留缩减为每个产物对象+verified暂存两份实际大小，再加Comfy仍保留的raw实际大小，因此小视频成功后不会永久占用大上限。默认与素材服务一致为10 GiB/用户、40 GiB/tenant；CPU backend独立attempt缓存另受上述8 GiB限制。零收费粗剪仍受存储配额。

新表`platform_artifact_write_receipts`保存job/attempt、明确store绑定、固定对象键、摘要、大小和角色状态；`platform_artifact_storage_accounting`按scope/物理键幂等登记旧artifacts，不覆盖现有上传reservation。历史缺失大小/摘要/归属则拒绝新增，不能假设为0；历史对象以已有元信息计数，未通过盘点核实的旧暂存副本仍是待核实项。

存储各构造器通过`storage_schema.create_storage_schema`使用与Repository启动一致的PG advisory事务锁（SQLite BEGIN IMMEDIATE）做只新增DDL，避免首次多个worker同时建表竞态。正式initdb也在已有平台DDL锁内预建这些metadata。

≤显式single-PUT上限用条件创建；R2/S3超过默认100 MiB通过现有MultipartUploadManager（最多512 MiB、32 MiB parts），固定attempt键额外登记在`storage_multipart_fixed_keys`防止另一个上传会话复用。R2不宣称支持条件CompleteMultipartUpload，依赖服务器唯一键账本与已授权写入者；AWS保留条件complete。

PUT不明时只核对原键；HEAD元信息不足，必须读回完整SHA256。MPU不明恢复原会话，绝不另开生成任务。持久意图仍处于in-flight的崩溃MPU须运维确认旧进程/网络操作已被隔离后才能使用内部`write(..., fenced=True)`；不能用lease过期或网页参数代替这一证明。不存在的未知对象不自动释放配额、不盲重试。

`TaskQueue.complete(..., settlement=writer.settlement(receipt))`在同一数据库事务登记artifact、完成job并归属预留空间。由于verified暂存仍保留，成功不虚假释放其空间。失败、部分成功和孤儿保留证据与配额，无自动删除。旧`complete`调用签名仍可用；这些历史artifact会在后续容量检查时幂等回填。

Worker一旦有输出receipt，恢复直接核对暂存及原对象，跳过fetch和FFmpeg，不重新生成。collector必须复用同一持久工作目录/共享挂载；换到空宿主或更换store配置会安全拒绝，不能把这条恢复机制描述为任意无状态机器可接管。当前未实现授权清理、跨宿主暂存迁移和硬磁盘配额。
