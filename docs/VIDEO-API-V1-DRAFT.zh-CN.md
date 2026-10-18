# H3 视频 API v1 与映序接入草案

日期：2026-10-04，Australia/Sydney。**设计稿，以下新路由、实体和字段尚未实现，不是可直接调用的现有 API 文档。**现有 `/api/*` 接口继续保留；实施时通过同一业务服务兼容旧网页，禁止双写两套生成队列。

对应部署计划：[LAUNCH-SCALE-NOVEL-PLAN.zh-CN.md](LAUNCH-SCALE-NOVEL-PLAN.zh-CN.md)。映序位置：`C:/Users/danmo/Desktop/inference/video-studio-design/studio-app`。用户已确认映序为目标。

## 1. 职责与调用链

映序浏览器 → 映序业务后端 → H3视频API → 调度器 → GPU worker或已接入供应商 → 受控成片存储 → 映序候选库。

映序拥有章节/场景/镜头/角色及版本、剪辑顺序和用户采用选择；H3拥有素材处理、生成任务/attempt、能力/配方、执行池和生成结果。两个服务可先部署在同一CPU主机，接口与数据归属保持清楚，之后无需更改创作模型就能拆开。引导页和无限画布继续操作同一个项目与任务集合。

映序后端使用独立服务身份；服务账户不是超级用户，不直接继承superdan权限。项目映射在H3服务端登记。用户委托通过可验证身份或授权映射表达，不接受任意 `owner` 字符串。初期自有两人账户可以映射稳定用户ID，不要求一开始引入Cognito。

## 2. 拟议路由

| 接口 | 用途与语义 |
|---|---|
| `GET /v1/capabilities` | 当前调用者可用配方、各输入/控制限制、验证级别和能力版本；不承诺当下有空闲GPU |
| `POST /v1/assets` | 初始化素材，返回asset ID和短期上传授权；不在请求里接任意远程URL |
| `POST /v1/assets/{id}/complete` | 上传结束；异步探测格式、时长/音轨/尺寸，校验通过才成为ready |
| `GET /v1/assets/{id}` | 查询校验及派生状态；按身份检查所有权 |
| `POST /v1/assets/{id}/derivatives` | 明确选段/裁剪/归一化，生成新的派生asset ID及父子关系；原文件不覆盖 |
| `POST /v1/generation-plans` | 预检、能力选择、费用/等待估算；返回不可变计划摘要/期限；不提交GPU或收费生成API |
| `POST /v1/jobs` | 使用有效计划创建异步任务；要求Idempotency-Key；202返回持久job ID |
| `GET /v1/jobs/{id}` | 状态、阶段、估时、失败原因、执行事实及结果引用；后端持续执行不依赖页面打开 |
| `POST /v1/jobs/{id}/cancel` | 提交取消意图；accepted不代表上游已停止或费用为零 |
| `GET /v1/jobs?client_project_id=...` | 分页恢复项目任务，只返回授权范围 |
| `POST /v1/batches` | 保存章节镜头计划及依赖，分窗口预检/准入；不是无限制立即创建GPU任务 |
| `GET /v1/batches/{id}` | 完成/待处理/阻塞/失败计数与各镜头job引用 |
| `GET /v1/jobs/{id}/artifacts` | 枚举受控成片、音轨、海报、执行记录；各对象有不同访问权限 |
| `POST /v1/artifacts/{id}/download` | 鉴权后返回短期下载能力或代理下载；不把临时URL当作品永久链接 |

asset初始化、派生、plan、batch均需请求去重，尤其不能重复产生付费供应商上传。素材上传和派生会消耗CPU/存储/流量，单独限额，不伪称完全零成本。

## 3. 能力表：不静默删参数

配方ID如 `h3-base-ref2va-v1` 是**本服务提出的内部配方名**，不是供应商model ID、中央资源ID或凭据profile。配方manifest绑定源码、权重revision、variant、精度、引擎、采样默认值与导出规则。上游真实model ID单独保存；不能在迁移时改写原值。

能力返回分开记录：`implemented`、`validation_level`、`enabled`和`capacity_state`。例如历史成功可写`historical_inference`并带准确配置/时间；当前GPU已关时capacity为offline。不得把离线构图成功、配置导入成功或模型目录存在标成线上可调用。

每个字段须有类型、单位、范围/枚举、默认值、适用模式、组合约束、验证证据；参数范围由部署发现并固定版本，不由前端硬编码猜测。`capabilities_version`变化需要再次预检。

必须覆盖现有控制：

| 组 | 需要保留的现有语义 |
|---|---|
| 任务模式 | FL2VA文生/首帧/尾帧/首尾帧；Ref2VA多模态参考；两种模式组合不可擅自合并 |
| 画面与时间 | resolution、aspect_ratio、width/height及当前32像素/面积/比例约束；duration 4–15整数秒；原生24fps、17n+5采样与精确导出分别显示 |
| 参考 | 图片/视频/音频分别列出；ref_image_size match/max；每条视频video_audio；实际素材数量/长度/像素预算以该配方校验器为准 |
| 时间锚点 | guides最多8项，图片/视频/音频分别指定目标秒数，源选段与是否连视频声音明确；不能越过生成时长 |
| 随机与采样 | seed使用十进制uint64字符串；steps 1–100服务范围；sampler_name、scheduler取部署枚举；denoise不是参考强度 |
| 音画与内存 | shift_video/shift_audio、encoder_device；视频完整/分块解码的空间块、重叠、时间块/重叠；音频只允许完整解码 |
| 输出 | generate_audio、export_crf 0–51；MP4 H264和音频FLAC等实际支持格式；下载/媒体检查 |

保留实验标记。音频VAE分块目前因实测不兼容禁用；不提供空有表单的CFG、负面词、角色锁定或“参考强度”。VDN必须单独配方，不能在Base任务中悄悄替换；SGLang VDN不承接Ref2VA。外部API仅接受它已核实支持的字段，不得忽略seed、guide等换取成功。

角色图生成、剧本LLM、Marble/3D世界、独立音乐服务以后各接对应资源，不能把H3视频API宣称为已经提供这些功能。

## 4. 映序单镜计划示例

以下只是字段设计样例，素材ID为占位符；不表示这个组合已经在5090上验收。这里使用内部配方名，未写任何真实上游ID、API凭据或账户值。

```json
{
  "client_ref": {
    "project_id": "yingxu-project-example",
    "chapter_id": "chapter-01",
    "scene_id": "scene-03",
    "shot_id": "shot-08",
    "shot_version": 7
  },
  "recipe_id": "h3-base-ref2va-v1",
  "capabilities_version": "example-version",
  "prompt": "同一位角色在雨中转身，镜头缓慢靠近。",
  "inputs": {
    "images": [{"asset_id": "asset-character-look-v2", "purpose": "identity"}],
    "videos": [{"asset_id": "asset-motion-trimmed", "purpose": "motion", "include_audio": false}],
    "audios": [{"asset_id": "asset-audio-trimmed", "purpose": "audio_reference"}],
    "guides": []
  },
  "controls": {
    "resolution": "768P",
    "aspect_ratio": "16:9",
    "duration": 5,
    "seed": "1234567890123456789",
    "steps": 20,
    "ref_image_size": "max",
    "generate_audio": true,
    "export_crf": 18
  },
  "execution_policy_id": "policy-approved-self-hosted",
  "client_edit": {"edit_duration_s": 4}
}
```

`purpose`帮助界面解释素材用途与编译提示词，不等于H3具备人物/动作专用硬控制。后端将它映射到实际受支持的输入结构。各配方默认值在计划里完全展开，再计算摘要，避免引擎升级时默认值暗变。省略steps等字段应采用固定配方默认，不允许任意映射通用采样器。

`execution_policy_id`由服务端验证所有权和预算，绑定允许供应商、硬件范围、费用上限、超时/排队策略。用户在JSON写一个policy名称不能授权新增租机。预算未设或生成未启用时，可以返回blocked预检说明，不创建付费执行。

首尾帧配方使用独立`first_frame`/`last_frame`素材引用；映序需要补尾帧关系。`client_edit`只是来源剪辑意图，不把5秒生成改造成4秒计费，也不暗中裁剪最终原始成片。

计划响应至少包含：`plan_id`、规范化request hash、完整有效参数、素材版本、具体候选backend及不可互换的能力差异、费用币种/来源时间/估算范围、已预留或未预留、预计排队/冷启动/生成时间分项与置信度、warnings、expires_at。没有可靠报价时写unknown而不是0。

默认计划不占用GPU或预留预算；`POST /v1/jobs`在事务中重新校验有效期、能力、资产和余额后原子预留。价格超原确认范围则要求重新确认计划；不得静默提交更贵任务。批次等待过久时同样重新预检。

## 5. 创建、幂等、状态与结果

创建请求只引用已确认plan及来源关联。幂等键按service client＋tenant＋project隔离：同键同摘要返回原job，同键不同摘要返回409。重复点击或网络重试不得自动换新键；用户明确“重新生成一版”才建立新的生成意图和键。并发唯一约束需数据库保证。

入队返回202与持久ID，不等待推理完成。状态序列参考主计划；生成执行、素材收集、取消和计费分开记录。`submission_unknown`可恢复但不可直接当成失败重发。Job查询输出实际backend、模型revision、精度、有效seed、真实参数、各阶段时间及受控artifact IDs；用户界面可只显示简洁摘要，内部证据不可丢。

成功条件：输出已进入自己的持久存储，校验可解码、时长/尺寸/音轨与任务要求一致，记录大小和校验值。供应商success或一条可能过期的远程URL不等于已完成。收集失败保持collecting或可恢复状态，不再支付生成一次。

权限敏感的原始响应、供应商凭据和签名URL不写执行证据。完整模型/配置指纹与面向用户的model label分开。结果记录source shot version；晚到的旧版本结果只进入该版本候选，不能自动覆盖目前选中的成片。

建议错误：401/403身份或项目授权；404对未授权资源统一表现；409幂等冲突/过期计划/版本冲突；422素材或控制不支持；429配额并给出重试提示。已经接纳的任务在临时缺卡时保持queued/blocked和可解释原因，不用HTTP错误让前端盲目创建第二单。系统无法持久接单才返回503。

## 6. 批次、回调与依赖

一章不是一条大prompt：batch包含shot intents、各版本和依赖。固定素材先上传和预检；依赖前镜输出的输入使用待解析引用，前镜成功并选取所需素材后才冻结该镜计划与预算。检验DAG无环，引用必须同授权范围。依赖失败只阻塞下游，不取消无关镜头。

MVP用持久状态＋带退避的轮询即可。后续可加webhook：仅允许登记的HTTPS目标，校验事件签名/时间戳、防重放与event ID去重；客户端成功存储后应答，服务端有限次重投并保留轮询补偿。不得任意抓取用户URL或让回调目标访问内部地址。

章节部分完成可以继续审核；只重跑明确选定的失败/返修镜头，保留全部旧候选。自动把前镜候选当下镜参考应由用户选择的连续性策略决定，不等于自动采用为成片。取消批次停止未执行镜头；运行镜头单独呈现取消/计费状态。

## 7. 映序需要做的功能与验收

| 用户故事 | 需要开发 | 验收标准 |
|---|---|---|
| 我在另一台电脑继续同一个章节 | 云端项目版本、账户/成员、素材桥接 | 重登后章节/造型/素材/任务一致；上传前保留浏览器本地作品备份，不默认抹掉或合并 |
| 我点一次生成，关页面后也能完成 | 单镜计划→确认→job绑定→后台轮询 | 同一生成意图重复点击只得一个job；关闭浏览器仍继续；恢复页面可见结果 |
| 人物/动作/声音分别上传，不研究模型节点 | 按用途的上传槽＋选段＋有效角色造型 | 上传实际Blob并保存云asset映射；选段生成真实派生文件；预览即本次实际输入 |
| 熟悉后能调全部已支持控制 | 共享capabilities驱动高级面板 | 引导页/无限画布改同一参数；不支持选项禁用并解释；预检显示实际尺寸、帧数和默认值 |
| 一章很多镜头一起排队 | batch计划、依赖/配额窗口、逐镜预算和ETA | 用户能看哪些独立并发、哪些等前镜；不占满其他用户配额；只重做选中部分 |
| 修改剧本或角色后不会混入旧版结果 | 不可变输入快照、版本差异、候选归属 | 修改使相关旧计划失效/待复核；已运行任务晚到也不覆盖新选片 |
| 我只想看到可以用的片段 | 成片存储、基础校验、候选审核 | 失败原因区分生成/下载/质量不满意；人工采用后才进镜头；原结果保留 |
| 我能按章节观看和交付 | 独立剪辑/声音合成管线 | 使用edit duration与素材in/out；原对白/配乐与生成声音分开；实际MP4可播，JSON不是成片 |

映序目前localStorage/IndexedDB及演示jobs无法直接作为多人云端事实来源。业务后端和素材映射是接入工作的一部分，不能只把生成按钮改一个URL就宣布完成。

## 8. 实施边界与验证

先做无网络假provider契约测试：素材/权限/能力拒绝、重复键、旧版本、部分批次失败、失联、结果下载重试、unknown提交不重发；随后按主计划分阶段授权真实GPU测试。

远端秘密访问沿用中央资源规则：本机复用中央加载器；AWS角色/Secrets Manager接入由中央加载器维护方支持并登记service/profile，不复制Windows DPAPI库或加载器。GPU worker只持有任务级短期权限。中央资源ID、API profile、内部recipe及供应商model ID四者不互换。

本文件未承诺现有H3所有控制已经在5090/B200验证，没有创建API客户端、复制凭据、修改映序代码、租GPU或调用供应商。
