export const operatorDetailCatalogs = {
  en: {
    'operator.more.modeUnknown': 'Mode not confirmed',
    'operator.more.modelPending': 'Model not confirmed',
    'operator.more.stale': 'State observation is stale{age}. Waiting for an update.',
    'operator.more.ago': ' ({time} ago)',
    'operator.more.weights': 'Weight download',
    'operator.more.image': 'Image pull',
    'operator.more.perHour': '/hour',
    'operator.more.machineDetails': 'Machine {id} details',
    'operator.more.invalidStart':
      'Enter a valid configuration, keep time and price. The per-GPU price must be above zero and within the administrator ceiling.',
    'operator.more.previewUnknown': 'The start preview could not be confirmed. Check it again.',
    'operator.more.groupUnknown':
      'The group was accepted, but its identity is unconfirmed. Refresh its original record.',
    'operator.more.keepExpired': 'The selected keep time has passed. Adjust it and check again.',
    'operator.more.conditionsUnknown':
      'Current inventory or start conditions could not be confirmed.',
    'operator.more.startUnknown':
      'This machine start is unconfirmed. Later machines were not submitted. Reconcile the original operation; do not restart the batch.',
    'operator.more.invalidKeep': 'Enter a valid idle time and a future keep time (up to 24 hours).',
    'operator.more.capacityLoss':
      'Add a compatible machine, or confirm that these jobs should keep waiting.',
    'operator.more.config': 'Configuration {index}',
    'operator.more.configModel': 'Configuration {index} model and GPU',
    'operator.more.configMode': 'Configuration {index} mode',
    'operator.more.configCount': 'Configuration {index} machine count',
    'operator.more.configPrice': 'Configuration {index} per-GPU price ceiling',
    'operator.more.ceiling': 'Maximum {price}/GPU/hour',
    'operator.more.spec':
      '1 GPU per machine · RAM ≥ {ram} GiB · VRAM ≥ {vram} GiB · Default ceiling {price}/GPU/hour',
    'operator.more.total': '{count} machines / {count} GPUs',
    'operator.more.checked': 'Start conditions checked for {count} machines',
    'operator.more.initialHold':
      'Initial budget reservation: {amount}. Inventory is checked again for each start; results appear per machine.',
    'operator.more.availableOffers':
      '{count} available reference configurations · Quantity is not reserved',
    'operator.more.machine': 'Machine {index}',
    'operator.more.start': 'Start {count} machines',
    'operator.more.checkStart': 'Check and start {count} machines',
    'operator.more.protections':
      'Hourly prices, account spend limits and original job protections continue to apply.',
    'operator.more.waitingLoss': '{count} {mode} jobs will have no available machine',
    'operator.more.generating': 'Generating #{id}',
    'operator.more.elapsed': ' · Running for {time}',
    'operator.more.secondsPerVideo': '{seconds} seconds/video',
    'operator.more.perVideo': 'Cost per video',
    'operator.more.todayCost': 'Today’s total cost',
    'operator.more.totalCost': 'Total cost',
    'operator.more.todaySpend': 'Today’s spend',
    'operator.more.submissionTiming':
      'Average submission to delivery: {seconds} seconds · {count} successful samples',
    'operator.more.stageSamples': '{count} successful stage samples · {start} to {end}',
    'operator.more.window': ' · {start} to {end}',
    'operator.more.videos': '{count} videos',
    'operator.more.offerCount': '{count} offers{available}',
    'operator.more.offerAvailable': ' · {count} available',
    'operator.more.accountSpend': 'Settled {spent} · Spend limit {limit} · Reserved {reserved}',
    'operator.more.reserved': 'Budget reserved {amount} (not a bill)',
    'operator.more.machines': 'machines',
    'operator.more.queueCounts': 'Running {busy} · Ready {ready} · Preparing {preparing}',
    'operator.more.groupPrice': '{count} machines · ≤ {price}/hour',
    'operator.more.until': 'Until {time}',
    'operator.more.policyPending': 'Runtime policy awaiting update',
    'operator.more.manualStop': 'Until I stop it manually',
    'operator.more.idle': 'Stop after {minutes} idle minutes',
    'operator.more.keepUntil': 'Keep until {time}',
    'operator.more.noWaiting': 'No waiting jobs',
    'operator.more.waitCompatible': 'Waiting for compatible capacity',
    'operator.more.canAccept': 'A machine can accept work',
    'operator.more.roughWait': 'Rough queue wait: {time}',
    'operator.more.ungrouped': 'Ungrouped',
    'operator.more.downloadUnknown': 'Download amount not reported',
    'operator.more.pullExtract': 'Pull + extract ',
    'operator.more.completedFiles': 'Completed files ',
    'operator.more.downloaded': 'Downloaded ',
    'operator.more.downloadBytesUnknown': 'Download bytes not reported',
    'operator.more.fileProgress': '{complete} / {total} files',
    'operator.more.files': '{count} files',
    'operator.more.downloadAmount': ' · Downloaded {bytes}',
    'operator.more.extractedAmount': ' · Extracted {bytes}',
  },
  zh: {
    'operator.more.modeUnknown': '模式尚未确认',
    'operator.more.modelPending': '模型待确认',
    'operator.more.stale': '状态观测已过期{age}，等待更新。',
    'operator.more.ago': '（{time}前）',
    'operator.more.weights': '权重下载',
    'operator.more.image': '镜像拉取',
    'operator.more.perHour': '/小时',
    'operator.more.machineDetails': '机器 {id} 详情',
    'operator.more.invalidStart':
      '请填写有效配置、保持时间和价格。每 GPU 价格须大于零，并且不超过管理员最高价格。',
    'operator.more.previewUnknown': '启动预览尚未确认，请重新核对。',
    'operator.more.groupUnknown': '机器组已接收，身份尚未确认；请刷新原记录。',
    'operator.more.keepExpired': '所选保持时间已过，请调整时间后重新核对。',
    'operator.more.conditionsUnknown': '最新库存或启动条件尚未确认。',
    'operator.more.startUnknown':
      '此台原启动结果待核对，后续机器尚未提交。请核对原操作，不要重新启动这一批。',
    'operator.more.invalidKeep': '请填写有效的空闲时长和未来保持时间（最长 24 小时）。',
    'operator.more.capacityLoss': '请增加兼容机器，或确认让这些任务继续等待。',
    'operator.more.config': '配置 {index}',
    'operator.more.configModel': '配置 {index} 模型与 GPU',
    'operator.more.configMode': '配置 {index} 模式',
    'operator.more.configCount': '配置 {index} 机器数',
    'operator.more.configPrice': '配置 {index} 每 GPU 价格上限',
    'operator.more.ceiling': '最高 {price}/GPU/小时',
    'operator.more.spec':
      '每台 1 GPU · 内存 ≥ {ram} GiB · 显存 ≥ {vram} GiB · 默认上限 {price}/GPU/小时',
    'operator.more.total': '{count} 台 / {count} 张 GPU',
    'operator.more.checked': '{count} 台启动条件已核对',
    'operator.more.initialHold': '初始预算预留合计 {amount}。启动时逐台重新查库存，按台显示结果。',
    'operator.more.availableOffers': '{count} 项可用参考配置 · 数量未锁定',
    'operator.more.machine': '机器 {index}',
    'operator.more.start': '启动 {count} 台',
    'operator.more.checkStart': '核对 {count} 台并启动',
    'operator.more.protections': '每小时价格、账户支出上限和原任务保护继续生效。',
    'operator.more.waitingLoss': '{count} 条 {mode} 任务将没有可用机器',
    'operator.more.generating': '正在生成 #{id}',
    'operator.more.elapsed': ' · 已运行 {time}',
    'operator.more.secondsPerVideo': '{seconds} 秒/条',
    'operator.more.perVideo': '每条生成费用',
    'operator.more.todayCost': '今日总费用',
    'operator.more.totalCost': '总费用',
    'operator.more.todaySpend': '今日支出',
    'operator.more.submissionTiming': '平均提交至交付 {seconds} 秒 · {count} 条成功样本',
    'operator.more.stageSamples': '{count} 条成功阶段样本 · {start} 至 {end}',
    'operator.more.window': ' · {start} 至 {end}',
    'operator.more.videos': '{count} 条',
    'operator.more.offerCount': '{count} 项{available}',
    'operator.more.offerAvailable': ' · {count} 项可用',
    'operator.more.accountSpend': '已结算 {spent} · 支出上限 {limit} · 预留 {reserved}',
    'operator.more.reserved': '预算预留 {amount}（不是账单）',
    'operator.more.machines': '台',
    'operator.more.queueCounts': '运行 {busy} · 就绪 {ready} · 准备 {preparing}',
    'operator.more.groupPrice': '{count} 台 · ≤ {price}/小时',
    'operator.more.until': '至 {time}',
    'operator.more.policyPending': '运行策略待更新',
    'operator.more.manualStop': '直到我手动停止',
    'operator.more.idle': '空闲 {minutes} 分钟后停止',
    'operator.more.keepUntil': '保持至 {time}',
    'operator.more.noWaiting': '没有等待任务',
    'operator.more.waitCompatible': '等待兼容机器准备好',
    'operator.more.canAccept': '有机器可接任务',
    'operator.more.roughWait': '队列粗略等待约 {time}',
    'operator.more.ungrouped': '未分组',
    'operator.more.downloadUnknown': '下载量尚未上报',
    'operator.more.pullExtract': '拉取＋解压 ',
    'operator.more.completedFiles': '已完成文件 ',
    'operator.more.downloaded': '已下载 ',
    'operator.more.downloadBytesUnknown': '下载字节数尚未上报',
    'operator.more.fileProgress': '{complete} / {total} 个文件',
    'operator.more.files': '{count} 个文件',
    'operator.more.downloadAmount': ' · 已下载 {bytes}',
    'operator.more.extractedAmount': ' · 已解压 {bytes}',
  },
};

// English singulars; Chinese count labels retain their normal form.
operatorDetailCatalogs.en['operator.more.total.one'] = '{count} machine / {count} GPU';
operatorDetailCatalogs.zh['operator.more.total.one'] =
  operatorDetailCatalogs.zh['operator.more.total'];
operatorDetailCatalogs.en['operator.more.checked.one'] =
  'Start conditions checked for {count} machine';
operatorDetailCatalogs.zh['operator.more.checked.one'] =
  operatorDetailCatalogs.zh['operator.more.checked'];
operatorDetailCatalogs.en['operator.more.start.one'] = 'Start {count} machine';
operatorDetailCatalogs.zh['operator.more.start.one'] =
  operatorDetailCatalogs.zh['operator.more.start'];
operatorDetailCatalogs.en['operator.more.checkStart.one'] = 'Check and start {count} machine';
operatorDetailCatalogs.zh['operator.more.checkStart.one'] =
  operatorDetailCatalogs.zh['operator.more.checkStart'];
operatorDetailCatalogs.en['operator.more.waitingLoss.one'] =
  '{count} {mode} job will have no available machine';
operatorDetailCatalogs.zh['operator.more.waitingLoss.one'] =
  operatorDetailCatalogs.zh['operator.more.waitingLoss'];
operatorDetailCatalogs.en['operator.more.videos.one'] = '{count} video';
operatorDetailCatalogs.zh['operator.more.videos.one'] =
  operatorDetailCatalogs.zh['operator.more.videos'];
operatorDetailCatalogs.en['operator.more.offerCount.one'] = '{count} offer{available}';
operatorDetailCatalogs.zh['operator.more.offerCount.one'] =
  operatorDetailCatalogs.zh['operator.more.offerCount'];
operatorDetailCatalogs.en['operator.more.groupPrice.one'] = '{count} machine · ≤ {price}/hour';
operatorDetailCatalogs.zh['operator.more.groupPrice.one'] =
  operatorDetailCatalogs.zh['operator.more.groupPrice'];
operatorDetailCatalogs.en['operator.more.availableOffers.one'] =
  '{count} available reference configuration · Quantity is not reserved';
operatorDetailCatalogs.zh['operator.more.availableOffers.one'] =
  operatorDetailCatalogs.zh['operator.more.availableOffers'];
operatorDetailCatalogs.en['operator.more.submissionTiming.one'] =
  'Average submission to delivery: {seconds} seconds · {count} successful sample';
operatorDetailCatalogs.zh['operator.more.submissionTiming.one'] =
  operatorDetailCatalogs.zh['operator.more.submissionTiming'];
operatorDetailCatalogs.en['operator.more.stageSamples.one'] =
  '{count} successful stage sample · {start} to {end}';
operatorDetailCatalogs.zh['operator.more.stageSamples.one'] =
  operatorDetailCatalogs.zh['operator.more.stageSamples'];
operatorDetailCatalogs.en['operator.more.files.one'] = '{count} file';
operatorDetailCatalogs.zh['operator.more.files.one'] =
  operatorDetailCatalogs.zh['operator.more.files'];
