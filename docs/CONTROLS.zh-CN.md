# H3 控制项与验证范围

本次“满血”指控制项，而不是只加载完整精度权重。网页和异步 API 共用同一份参数校验与 Comfy 图构建器；实际采样尺寸、参考素材、时间锚点、采样和解码参数都进入节点。专业用户另可打开本机 SSH 隧道中的原生 ComfyUI，使用更自由的节点工作流。

## 可调参数

| 分类 | 网页 / API 字段 | 实际作用与边界 |
|---|---|---|
| 工作流 | `mode` | Ref2VA 图片/视频/音频参考；FL2VA 纯文字或首尾帧。主模型仍是非量化 BF16，无 Turbo 替换。 |
| 生成尺寸 | `resolution`, `aspect_ratio`, `width`, `height` | 480P、576P、768P 和受限自定义尺寸，直接改变 latent 采样宽高。低分辨率是实验草稿；自定义须 32 的倍数、256–1536、像素面积不超过 1344×768、宽高比 0.4–2.5。不是官方 2K 再生成。 |
| 时长 | `duration` | 4–15 整数秒；原生采样向上对齐 17n+5 帧，按请求秒数精确导出。4 秒是新增范围；原生 24fps，不能把改播放速度称为高帧率生成。 |
| 参考细节 | `ref_image_size` | `match` 按生成面积缩小参考图，`max` 保留最多 2048 短边的参考细节；不放大原图，细节优先可能显著增加耗时。原有默认仍是 `max`。 |
| 视频原声 | `video_audio` | 每条参考视频单独决定是否连接其音轨；独立声音标记的编号随实际连接的音轨变化。 |
| 时间锚点 | `guides` | 已上传图片、视频或音频放到指定秒数；实际进入 `MiniMaxH3AddGuide` 的 VAE 编码路径。最多 8 个；拒绝超出最终成片时长的素材，避免静默裁尾。视频锚点可选择是否包括声音。 |
| 随机性 | `seed` | 支持完整无符号 64 位种子；网页使用十进制字符串，避免 JavaScript 大整数精度损失。空值由服务器生成。 |
| 采样 | `steps`, `sampler_name`, `scheduler`, `denoise` | 步数 1–100 为本服务的操作范围；采样器和调度器来自当前部署节点枚举。默认 20 步、res_multistep、Ref beta / FL simple。denoise 是 sigma 日程范围，不是“参考强度”。 |
| 音画日程 | `shift_video`, `shift_audio` | H3 原生 sigma shift；默认视频 12、音频 3。省略保持原图行为，显式修改时同时连接 guider 和 scheduler。实验控制，无质量保证。 |
| 编码器 | `encoder_device` | 默认调度或 CPU；没有改变权重精度。CPU 不等于更快。 |
| 解码 | `video_decode`, `audio_decode` 和分块参数 | 视频普通 / 分块解码，可调空间块、重叠、时间块与时间重叠；音频只开放完整解码。当前 H3 音频 VAE 与通用分块节点实测张量维度不兼容，网页和 API 已拦截该选项。分块是内存取舍，不是增强细节按钮。 |
| 导出 | `generate_audio`, `export_crf` | 是否导出生成音轨；H264 CRF 0–51 控制压缩损失与体积，不改变模型采样细节。低于 18 时云端首轮编码也使用指定 CRF，避免先按默认18压缩；最终本地导出使用请求CRF。MP4 H264、独立 FLAC、32kHz 双声道保持原有兼容格式。 |
| 预检 | `POST /api/workflow-preview` | 只校验已有素材和参数，返回真实节点图及采样规格；不排队、不生成、不计新增 GPU 推理费用。 |

分辨率预设按短边和对应像素面积上限计算，再对齐 32 像素，因此极宽画幅实际短边会更小。网页显示最终宽高。例如 768P / 21:9 为 1536×672；768P / 16:9 为 1344×768。API `resolution` 是本工作台枚举，不是 MiniMax 官方托管 API 的模型参数。

## 提示词引导与硬控制的区别

人物外貌、镜头运动、台词、声音、保留/改变的内容可以通过提示词辅助表达，但没有独立的“身份强度”“口型精度”“镜头强度”旋钮。辅助文字可编辑，参考不保证完全锁定人物、动作或构图。

公开 Base 已经过 CFG 蒸馏，当前官方原生条件节点没有负面提示词入口；不能加一个 CFG 或负面词表单就声称生效。官方 Context-IR、H3-Regenerate-2K 没有部署。ControlNet/Fun patch 和 Turbo LoRA 不在当前五个模型文件中；原生 ComfyUI 出现相应节点不代表额外权重可用。局部蒙版重绘/扩展需要另一套 latent mask 图，目前引导网页未封装，不能称所有通用节点能力都已变成表单。

## 验证口径

新增控件的离线与真实浏览器验收记录将保存在 `controls-acceptance.json`。这不替代真实 GPU 推理。既有七项成片只验证了 768P、20 步、默认采样/解码和参考模式，详见 `RESULTS.zh-CN.md`。新增 4 秒、480P/576P、自定义尺寸、时间锚点、分块和非默认采样等，未完成真实生成的组合必须明确保留“实验 / 未实测”。

本次最终 **66项离线测试通过（8.778秒）**。新增真实任务 `aa0d0cf40e604ab3a20726a1d3d301fa` 完成4秒480P（832×480、24fps、96帧）、三类参考与三类时间锚点（0、0、2秒）、完整64位种子、match参考图、关闭参考视频原声、sigma12.1/3.1、视频分块/音频完整解码和CRF23，用时264.922秒。MP4与FLAC独立全片解码、尺寸/时间/32kHz立体声及SHA256校验通过；浏览器实际播放至4秒。只证明这个具体组合执行成功，没有证明每种采样器/尺寸/锚点强度都通过或严格复刻。

初次相同参数使用音频分块，在采样完成后因张量维度不兼容失败，记录为 `c38d5905e89946e09055730c9f81e003`。只改为普通音频解码后重跑成功；音频分块现已在网页/API/构建器拒绝。原生画布起点已通过浏览器Ctrl+O实际导入，未从该画布额外提交GPU任务。

用户要求保持服务器，原三小时TTL已取消并API核验。网页显示“未设置自动销毁 / 持续运行 · 按小时计费”；不会因旧22:57倒计时主动停GPU。$1.46/小时持续计费，不自动充值。

原生 ComfyUI：`http://127.0.0.1:8189/`，仅在本次云实例与 SSH 隧道可用时工作。专业面板提供 H3 BF16 画布起点文件 `web/h3-ref-bf16-workflow.json`，可以导入原生界面。直接在原生界面提交的任务不进入 Studio 任务账本，可能占用 GPU 队列。专业模式不绕过租期，不自动续租。

## 依据

- 固定部署源码：`research/nodes_minimax_h3.py`，ComfyUI `e9027f2b30f37bb3052714eb08fcf479542f4fc0`；实际节点 schema：`comfy-object-info.json`。
- [Comfy 原生 H3 说明](https://docs.comfy.org/tutorials/video/minimax/minimax-h3-native)。
- [MiniMax H3 模型卡](https://huggingface.co/MiniMaxAI/MiniMax-H3)。
- [Base 提示指南](https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/docs/VIDEO_PROMPT_WRITING_GUIDE_base_en.md)、[参考模式提示指南](https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/docs/VIDEO_PROMPT_WRITING_GUIDE_ref_en.md)。

官方托管 H3 / H3-Max 的分辨率和时长规则不同于本工作台公开 Base；[官方 API 参数](https://platform.minimax.io/docs/api-reference/video-generation-v2-create)不能直接套用到自建图中。
