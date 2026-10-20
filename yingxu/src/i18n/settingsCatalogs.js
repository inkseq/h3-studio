export const settingsCatalogs = {
  en: {
    'settings.testTask': 'Test GPU limit per acceptance task (USD)',
    'settings.testTaskId': 'Acceptance task ID',
    'settings.testTaskHelp':
      'Use the same ID for this issue’s machines and retries. Other tasks keep their own spending records.',
    'settings.account': 'Account',
    'settings.defaults': 'Generation defaults',
    'settings.defaultsHelp':
      'Edit take limits and folding in Quick Create. Global, story and shot recipe defaults are available in the story editor.',
    'settings.storyDefaults': 'Open stories to edit recipe defaults',
    'settings.documentTitle': 'Settings · Inkseq',
    'settings.title': 'Settings',
    'settings.back': 'Back to creation',
    'settings.budget': 'My budget',
    'settings.limits': 'Usage limits',
    'skin.title': 'Appearance',
    'skin.default': 'Inkseq',
    'skin.defaultHelp': 'Dark studio with a lime accent.',
    'skin.taisho': 'Taisho · Wafu',
    'skin.taishoHelp': 'Ink and pine green, crimson accents, checkered and hemp-leaf motifs.',
    'settings.dailyCredits': 'Daily limit (credits)',
    'settings.askCredits': 'Ask me first above this many credits per submission',
    'settings.zeroCredits': '0 means no limit. Your credit balance still applies.',
    'settings.invalidCredits': 'Enter a whole number of credits (0 or more).',
    'settings.creditsToday': 'Used or held today: {count} credits',
    'settings.assistants': 'AI assistants',
    'settings.admin': 'Admin',
    'settings.loading': 'Loading settings…',
    'settings.save': 'Save changes',
    'settings.saving': 'Saving…',
    'settings.saved': 'Saved. New admissions use these settings immediately.',
    'settings.daily': 'Daily spending limit (USD)',
    'settings.ask': 'Ask me first above this amount per submission (USD)',
    'settings.zero':
      '0 means no additional limit. Existing account credit and pending charges still apply.',
    'settings.day':
      'Daily limits follow Australia/Sydney. Pending charges from earlier days remain included until reconciled.',
    'settings.usage': 'Admission exposure: {amount}',
    'settings.noAssistants': 'No AI assistants connected yet.',
    'settings.cap': 'Daily cap (USD)',
    'settings.revoked': 'Revoked',
    'settings.testDaily': 'Test machine daily limit (USD)',
    'settings.hourly': 'Maximum GPU hourly price (USD)',
    'settings.idle': 'Idle auto-stop (minutes)',
    'settings.fleetHourly': 'Maximum combined hourly machine cost (USD)',
    'connect.dailyInstruction':
      'Daily cap: USD {amount}; 0 means no additional cap. Its owner can edit this in Settings → AI assistants. Sydney day; unresolved prior charges remain included. A limit refusal does not authorize retries or raising limits.',
    'settings.runtime': 'Maximum test machine runtime (minutes)',
    'settings.adminHelp':
      'Changes apply to new machine previews. Lower safety limits also stop ongoing test work sooner; accepted inputs and accounting remain intact.',
    'settings.invalid': 'Enter a valid non-negative amount, with at most six decimal places.',
    'settings.login': 'Sign in to edit your settings.',
    'settings.reload': 'Reload settings',
    'errors.spendingConfirmationRequired': 'This submission costs up to {amount}. Continue?',
    'errors.user_daily_limit_reached':
      'Your daily spending limit has been reached. Change it in Settings → My budget.',
    'errors.connection_daily_limit_reached':
      'This assistant’s daily spending limit has been reached. Its owner can change it in Settings → AI assistants.',
    'errors.spending_usage_unconfirmed':
      'Spending is not yet confirmed. Reconcile the existing charge before submitting again.',
  },
  zh: {
    'settings.testTask': '每次验收任务的测试 GPU 上限（美元）',
    'settings.testTaskId': '验收任务编号',
    'settings.testTaskHelp': '同一项改动的机器和重试沿用同一编号；其他任务的费用记录保持独立。',
    'settings.account': '\u8d26\u6237',
    'settings.defaults': '\u751f\u6210\u9ed8\u8ba4\u503c',
    'settings.defaultsHelp':
      '\u5728\u5feb\u901f\u521b\u4f5c\u4e2d\u8c03\u6574\u7248\u672c\u6570\u4e0a\u9650\u548c\u6298\u53e0\u6570\u3002\u5168\u5c40\u3001\u6545\u4e8b\u53ca\u955c\u5934\u914d\u65b9\u9ed8\u8ba4\u503c\u53ef\u5728\u6545\u4e8b\u7f16\u8f91\u5668\u4e2d\u4fee\u6539\u3002',
    'settings.storyDefaults': '\u6253\u5f00\u6545\u4e8b\u4fee\u6539\u914d\u65b9\u9ed8\u8ba4\u503c',
    'settings.documentTitle': '设置 · 映序',
    'settings.title': '设置',
    'settings.back': '返回创作',
    'settings.budget': '我的预算',
    'settings.limits': '用量上限',
    'skin.title': '外观',
    'skin.default': '映序',
    'skin.defaultHelp': '深色工作室，荧光绿点缀。',
    'skin.taisho': '大正 · 和风',
    'skin.taishoHelp': '墨黑与松绿，绯红点缀，市松格与麻叶纹。',
    'settings.dailyCredits': '每天最多用（积分）',
    'settings.askCredits': '单次提交超过多少积分先问我',
    'settings.zeroCredits': '0 表示不限；积分余额仍然有效。',
    'settings.invalidCredits': '请输入 0 或以上的整数积分。',
    'settings.creditsToday': '今天已用或预留：{count} 积分',
    'settings.assistants': 'AI 助手',
    'settings.admin': '管理员',
    'settings.loading': '正在读取设置…',
    'settings.save': '保存修改',
    'settings.saving': '正在保存…',
    'settings.saved': '已保存。新的提交会立即使用这些设置。',
    'settings.daily': '每日消费上限（美元）',
    'settings.ask': '单次提交超过此金额先问我（美元）',
    'settings.zero': '0 表示不额外限制。已有账户额度和未结算费用仍然有效。',
    'settings.day':
      '每日上限按 Australia/Sydney 日期计算。之前日期的未确认费用会保留，直至完成核对。',
    'settings.usage': '预算校验占用：{amount}',
    'settings.noAssistants': '尚未连接 AI 助手。',
    'settings.cap': '每日上限（美元）',
    'settings.revoked': '已撤销',
    'settings.testDaily': '测试机器每日上限（美元）',
    'settings.hourly': '每 GPU 最大小时价格（美元）',
    'settings.idle': '空闲自动关机（分钟）',
    'settings.fleetHourly': '所有机器合计最大小时费用（美元）',
    'connect.dailyInstruction':
      '每日上限为 {amount} 美元，0 表示不额外限制。账户所有者可在“设置 → AI 助手”中修改，按 Sydney 日期计算；之前日期的未确认费用继续计入。遇到上限拒绝，不得自行重试或提高限额。',
    'settings.runtime': '测试机器最长运行时间（分钟）',
    'settings.adminHelp':
      '修改适用于新的机器预检；更低的安全上限也会提前停止正在运行的测试任务。已接收的输入和账目保持不变。',
    'settings.invalid': '请输入有效的非负金额，最多保留六位小数。',
    'settings.login': '请先登录后修改设置。',
    'settings.reload': '重新读取设置',
    'errors.spendingConfirmationRequired': '本次提交最多预留 {amount}，是否继续？',
    'errors.user_daily_limit_reached': '已达到你的每日消费上限，可在“设置 → 我的预算”中修改。',
    'errors.connection_daily_limit_reached':
      '已达到此 AI 助手的每日上限，账户所有者可在“设置 → AI 助手”中修改。',
    'errors.spending_usage_unconfirmed': '当前费用尚未确认，请先核对已有费用后再提交。',
  },
};

settingsCatalogs.en['settings.costDetails'] = 'Cost details';
settingsCatalogs.zh['settings.costDetails'] = '费用详情';

Object.assign(settingsCatalogs.en, {
  'settings.videoCache': 'Video cache',
  'settings.videoCacheClear': 'Clear',
  'settings.videoCacheUnavailable': 'Video cache is unavailable in this browser.',
  'settings.videoCacheChecking': 'Checking video cache…',
  'settings.videoCacheUsage': '{amount} MB used · {count} videos · up to 1 GB',
  'settings.videoCacheUsageOne': '{amount} MB used · 1 video · up to 1 GB',
  'settings.videoCacheHelp':
    'Played videos up to 64 MB are kept here. Access is checked before replay. Signing out clears cached copies; originals stay online.',
});
Object.assign(settingsCatalogs.zh, {
  'settings.videoCache': '视频缓存',
  'settings.videoCacheClear': '清空',
  'settings.videoCacheUnavailable': '此浏览器暂时无法使用视频缓存。',
  'settings.videoCacheChecking': '正在读取缓存…',
  'settings.videoCacheUsage': '已用 {amount} MB · {count} 条视频 · 最多 1 GB',
  'settings.videoCacheUsageOne': '已用 {amount} MB · 1 条视频 · 最多 1 GB',
  'settings.videoCacheHelp':
    '缓存已播放且不超过 64 MB 的视频，重播前仍会核对权限。退出登录会清空缓存，线上原件保留。',
});

Object.assign(settingsCatalogs.en, {
  'settings.h3Defaults': 'Video generation defaults',
  'settings.storyDefaultsScope':
    'These account defaults are used by story shots that inherit them. Quick Create keeps its own next-generation settings.',
  'settings.defaultsRecommended': 'Using recommended values',
  'settings.defaultsChangedOne': '1 saved override',
  'settings.defaultsChanged': '{count} saved overrides',
  'settings.allDefaultParameters': 'All default parameters',
  'settings.editStoryDefaults': 'Edit defaults in the current story',
  'settings.chooseStoryDefaults': 'Choose a story to edit defaults',
  'settings.defaultsFailed':
    'Generation defaults could not be loaded. Existing settings are retained.',
  'settings.recommended': 'Recommended',
  'settings.enabled': 'On',
  'settings.disabled': 'Off',
});
Object.assign(settingsCatalogs.zh, {
  'settings.h3Defaults': '视频生成默认值',
  'settings.storyDefaultsScope':
    '账户默认值用于继承它们的故事镜头；快速创作保留自己的下一次生成设置。',
  'settings.defaultsRecommended': '全部使用推荐值',
  'settings.defaultsChangedOne': '已保存 1 项修改',
  'settings.defaultsChanged': '已保存 {count} 项修改',
  'settings.allDefaultParameters': '全部默认参数',
  'settings.editStoryDefaults': '在当前故事中修改默认值',
  'settings.chooseStoryDefaults': '选择故事并修改默认值',
  'settings.defaultsFailed': '暂时无法读取生成默认值，原设置保留。',
  'settings.recommended': '推荐值',
  'settings.enabled': '开',
  'settings.disabled': '关',
});

Object.assign(settingsCatalogs.en, {
  'settings.accountCosts': 'Account cost status',
  'settings.connectionCosts': 'From this assistant',
  'settings.costDay': '{day} · Sydney',
  'settings.costConfirmed': 'Confirmed today',
  'settings.costAwaiting': 'Awaiting reconciliation',
  'settings.costReserved': 'Reserved',
  'settings.costUnavailable':
    'The cost breakdown is unavailable. Existing commitments remain recorded.',
  'settings.costAwaitingCountOne': '1 record awaits cost reconciliation.',
  'settings.costAwaitingCount': '{count} records await cost reconciliation.',
  'settings.costPriorPending':
    'Includes {amount} pending from earlier days; this is not today’s paid spend.',
  'settings.costUnknownDate':
    'Confirmed with no recorded settlement date: {amount}. Excluded from today’s confirmed amount.',
  'settings.costUnconfirmedOne': '1 record has unconfirmed amounts. The breakdown needs review.',
  'settings.costUnconfirmed':
    '{count} records have unconfirmed amounts. The breakdown needs review.',
  'settings.costUnconfirmedUnknown': 'Some amounts are unconfirmed. The breakdown needs review.',
  'settings.costUnconfirmedEstimate': 'Known estimate: {amount}.',
  'settings.costMachinesAwaiting': 'Machines · awaiting provider invoice',
  'settings.costJobsAwaiting': 'Generation · estimate awaiting cost allocation',
  'settings.costExposure': 'Attributed costs and commitments',
  'settings.costUnattributedShared': 'Shared records awaiting owner attribution (separate)',
  'settings.costUnattributedActor': 'Included above · origin not recorded',
  'settings.costAllocationHelp':
    'Generation estimates are not separate provider invoices. Actual machine costs must be reconciled before allocation.',
  'settings.admissionHelp':
    'Includes unresolved earlier records and records awaiting attribution. This protects your cap; it is not today’s paid spend.',
  'settings.invoiceCheck.not_needed': 'No pending machine records require a provider check.',
  'settings.invoiceCheck.not_checked': 'Provider costs have not been checked yet.',
  'settings.invoiceCheck.current':
    'A recent provider check is recorded; final costs still await reconciliation.',
  'settings.invoiceCheck.overdue':
    'The provider cost check is overdue. Pending costs remain unconfirmed.',
  'settings.invoiceCheck.unavailable':
    'The provider cost check is unavailable. Existing pending records are retained.',
  'settings.invoiceCheck.unknown': 'Provider cost check status is unavailable.',
  'settings.invoiceCheckedAt': 'Last checked: {date} · Sydney',
  'settings.invoiceCheckedCount': 'Pending machine records checked: {checked} / {pending}',
  'settings.invoiceReported':
    'Provider-reported estimate: {amount}. This is not a final bill or a settled charge.',
});
Object.assign(settingsCatalogs.zh, {
  'settings.accountCosts': '账户费用状态',
  'settings.connectionCosts': '此助手发起',
  'settings.costDay': '{day} · 悉尼',
  'settings.costConfirmed': '今日已确认',
  'settings.costAwaiting': '待核对费用',
  'settings.costReserved': '已预留',
  'settings.costUnavailable': '暂时无法读取费用明细，已有费用和预留记录保持不变。',
  'settings.costAwaitingCountOne': '1 条记录等待费用核对。',
  'settings.costAwaitingCount': '{count} 条记录等待费用核对。',
  'settings.costPriorPending': '其中 {amount} 来自较早日期，不代表今日已支付。',
  'settings.costUnknownDate': '已确认但结算日期未记录：{amount}，未计入今日已确认。',
  'settings.costUnconfirmedOne': '1 条记录的金额尚未确认，费用明细需要核对。',
  'settings.costUnconfirmed': '{count} 条记录的金额尚未确认，费用明细需要核对。',
  'settings.costUnconfirmedUnknown': '部分金额尚未确认，费用明细需要核对。',
  'settings.costUnconfirmedEstimate': '已知估算：{amount}。',
  'settings.costMachinesAwaiting': '机器 · 等待供应商账单',
  'settings.costJobsAwaiting': '生成 · 估算等待费用分摊',
  'settings.costExposure': '已归属费用与预留',
  'settings.costUnattributedShared': '尚未确认账户的共享记录（另列）',
  'settings.costUnattributedActor': '已包含在上方 · 发起来源未记录',
  'settings.costAllocationHelp': '生成任务的估算不是单独的供应商账单，须先核对机器实际费用再分摊。',
  'settings.admissionHelp':
    '包含较早日期未核对、以及来源待确认的记录，用于保护预算上限，不代表今日已支付。',
  'settings.invoiceCheck.not_needed': '没有待核对的机器记录需要查询供应商。',
  'settings.invoiceCheck.not_checked': '尚未查询供应商费用。',
  'settings.invoiceCheck.current': '已记录近期供应商查询，最终费用仍待核对。',
  'settings.invoiceCheck.overdue': '供应商费用查询已超时未更新，待核对费用尚未确认。',
  'settings.invoiceCheck.unavailable': '暂时无法查询供应商费用，已有待核对记录保留。',
  'settings.invoiceCheck.unknown': '暂时无法读取供应商费用查询状态。',
  'settings.invoiceCheckedAt': '上次查询：{date} · 悉尼',
  'settings.invoiceCheckedCount': '已查询待核对机器记录：{checked} / {pending}',
  'settings.invoiceReported': '供应商返回的估算：{amount}，不是最终账单，也未视为已结算。',
});
Object.assign(settingsCatalogs.en, {
  'settings.accountCharges': 'Your charges',
  'settings.connectionCharges': 'Charges from this assistant',
  'settings.chargeConfirmed': 'Confirmed user charge today',
  'settings.chargeAwaiting': 'Charge awaiting reconciliation',
  'settings.chargeReserved': 'Reserved user charge',
  'settings.chargeRetainedCosts':
    'User charges reflect audited waivers. Original costs, provider records and budget safety exposure remain unchanged.',
  'settings.grossConfirmed': 'Retained confirmed costs today',
  'settings.grossAwaiting': 'Retained costs awaiting reconciliation',
  'settings.grossExposure': 'Retained original cost exposure',
  'settings.chargeWaived': '{count} references waived',
});
Object.assign(settingsCatalogs.zh, {
  'settings.accountCharges': '我的应付费用',
  'settings.connectionCharges': '此助手产生的应付费用',
  'settings.chargeConfirmed': '今日已确认应付',
  'settings.chargeAwaiting': '待核对应付',
  'settings.chargeReserved': '应付预留',
  'settings.chargeRetainedCosts':
    '应付费用已计入有审核记录的免单。原始成本、供应商记录和预算安全占用保持不变。',
  'settings.grossConfirmed': '保留的今日已确认成本',
  'settings.grossAwaiting': '保留的待核对成本',
  'settings.grossExposure': '保留的原始成本占用',
  'settings.chargeWaived': '已免除 {count} 条记录的用户费用',
});
