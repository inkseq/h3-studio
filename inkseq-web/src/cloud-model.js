import { formatMoneyMicros } from './money.js';
import { translate } from './i18n/locale.js';
import { boundVideoRange } from './video-cut-model.js';
import { ancestors, validateProject } from './store.js';
import { effectiveLook } from './character-model.js';
import { shotLocationIds } from './location-model.js';
import {
  roleSupported,
  validControlValue,
  recipeFor,
  effectiveControlSchema,
  CONTROL_LABELS,
} from './quick-chat-model.js';

const clone = (value) => structuredClone(value);
// Translate only known system notices; user-authored text and diagnostics stay intact.
export function cloudSystemMessage(message, locale = 'zh') {
  const key = {
    '云项目已打开。': 'workspace.notice.cloudOpened',
    '云项目已打开；原本机作品保留。': 'workspace.notice.localPreserved',
    '已恢复尚未同步的云草稿。': 'workspace.notice.draftRestored',
    '项目版本已保存。': 'workspace.notice.saved',
    '素材已加入当前项目素材库，请明确选择镜头和用途；尚未提交生成。': 'workspace.notice.assetAdded',
    '预检已完成，请核对实际发送输入和预算后确认。': 'workspace.notice.preflightReady',
    '批次已记录。可查看任务进度和逐镜审核；不会自动采用结果。': 'workspace.notice.batchSaved',
    '粗剪任务已提交，可在本章粗剪版本查看进度；结果不替换镜头或时间线。':
      'workspace.notice.roughCutSubmitted',
    '可读取的本机素材已同步，项目版本已保存。': 'workspace.notice.materialsSynced',
    '取消请求已提交。以任务最终状态为准，已执行部分可能已产生费用。':
      'workspace.notice.cancelRequested',
    '暂无已登记且心跳有效的匹配工作机，也无有效的独立冷启动审批': 'availability.unavailableMessage',
  }[message];
  const historyKey = {
    '永久删除已受理，仍在处理中；请核对原清理状态。': 'quickHistory.noticePending',
    '原清理操作已核对，受阻记录仍保留。': 'quickHistory.noticeBlocked',
    '移到回收站已完成，任务和计费账本保留。': 'quickHistory.noticeTrash',
    '恢复创作已完成，任务和计费账本保留。': 'quickHistory.noticeRestore',
    '永久删除已完成，任务和计费账本保留。': 'quickHistory.noticePurge',
    '清空回收站已完成，任务和计费账本保留。': 'quickHistory.noticeEmpty',
  }[message];
  return key || historyKey ? translate(locale, key || historyKey) : message;
}
export const jobLabels = {
  planned: '等待批次确认',
  waiting_capacity: '等待计算容量',
  queued: '等待执行',
  claimed: '已分配',
  submitting: '正在提交',
  running: '生成中',
  collecting: '保存与核验结果',
  checking: '检查成片',
  succeeded: '已完成',
  failed: '失败',
  cancel_requested: '正在确认取消',
  cancelled: '已取消',
  blocked: '等待条件满足',
  submission_unknown: '提交结果待核对',
  recovery_hold: '恢复后待核对',
};
export const jobStatusLabel = (status, locale = 'zh') =>
  Object.hasOwn(jobLabels, status) ? translate(locale, 'jobs.status.' + status) : status;
export const activeJobStatuses = new Set([
  'waiting_capacity',
  'queued',
  'claimed',
  'submitting',
  'running',
  'collecting',
  'checking',
  'cancel_requested',
]);
export function recoveryMessage(job, locale = 'zh') {
  return job?.error_code === 'cancellation_requires_reconciliation'
    ? translate(locale, 'jobs.cancelReview')
    : translate(locale, 'jobs.review');
}
export function capacityWaitMessage(job, locale = 'zh') {
  if (
    job?.status !== 'waiting_capacity' &&
    !(job?.status === 'queued' && job?.error_code === 'capacity_queued_task_repair_required')
  )
    return '';
  const reasons = {
    capacity_no_matching_gpu:
      '暂时没有符合型号、显存和价格上限的可用 GPU。系统会继续查找其他合适机器；原任务保留，无需重新提交。',
    capacity_inventory_check_failed:
      '暂时无法确认 GPU 库存，系统会重试查询。目前尚未确认有可用机器，请保留原任务。',
    capacity_rental_reconciliation:
      'GPU 租赁结果需要核对，当前尚未开始生成。为避免重复收费，已暂停再次租机；原任务保留，等待处理。',
    capacity_configuration_unavailable:
      '当前 GPU 执行配置暂不可用，需修复后才能继续。原任务保留，可取消等待。',
    capacity_bootstrap_repair_required: 'GPU 环境准备失败，正在修复；原任务已保留，无需重新提交。',
    capacity_queued_task_repair_required:
      'GPU 工作机需要恢复，已暂停接新任务。你的任务仍保留，等待恢复；可取消等待，请勿重复提交。',
    capacity_budget_or_limit:
      '当前 GPU 预算或并发额度不足，暂时无法开机。原任务保留，不会自动提高预算。',
    capacity_gpu_starting:
      '正在准备 GPU、加载模型并检查运行条件，通过后自动执行原任务，无需再次提交。',
    capacity_provider_preparing:
      'GPU 供应商正在准备机器，尚未开始加载模型或生成。原任务保留，无需重新提交。',
    capacity_provider_configuring_ssh:
      'GPU 供应商正在配置远程连接，尚未开始加载模型或生成。原任务保留，无需重新提交。',
    capacity_provider_preparation_failed:
      'GPU 供应商准备机器失败，尚未开始生成。机器回收与费用仍需核对，原任务保留；无需重新提交。',
    capacity_provider_preparation_timeout:
      'GPU 供应商准备机器超时，尚未开始生成。机器回收与费用仍需核对，原任务保留；无需重新提交。',
    capacity_provider_preparation_retry_limit:
      'GPU 供应商准备机器连续失败，已暂停自动租机，等待管理员处理。尚未开始生成，原任务仍保留；可取消等待，无需重新提交。',
    capacity_gpu_busy: 'GPU 正在处理队列中的任务，当前任务等待空闲执行位置，无需重新提交。',
    capacity_searching_gpu:
      '正在查找符合条件的 GPU，尚未开始生成。找到后会自动准备机器，可取消等待。',
  };
  return Object.hasOwn(reasons, job.error_code)
    ? translate(locale, 'jobs.reason.' + job.error_code)
    : translate(locale, 'jobs.waitDefault');
}
export function jobMessage(job, locale = 'zh') {
  if (
    job?.status === 'waiting_capacity' ||
    (job?.status === 'queued' && job?.error_code === 'capacity_queued_task_repair_required')
  )
    return capacityWaitMessage(job, locale);
  if (job?.status === 'recovery_hold') return recoveryMessage(job, locale);
  if (job?.error_code === 'render_cache_capacity_exhausted')
    return translate(locale, 'jobs.cacheFull');
  return (
    job?.failure_diagnostic?.message || job?.message || job?.error?.message || job?.error_code || ''
  );
}
// Diagnostics show only short machine tokens and numbers, never free text from the worker.
const SAFE_TOKEN = /^[A-Za-z0-9_.:-]{1,200}$/;
function diagnosticCode(value, at) {
  return typeof value === 'string' && SAFE_TOKEN.test(value)
    ? value
    : at('adjunct.failureDetails.copy18');
}
function diagnosticScalar(value, at) {
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  if (typeof value === 'boolean')
    return at(value ? 'adjunct.failureDetails.copy19' : 'adjunct.failureDetails.copy20');
  return typeof value === 'string' && SAFE_TOKEN.test(value) ? value : null;
}
// Input rows: file counts and clip lengths per kind, frames, sound tracks and time anchors.
function inputRows(inputs, { at, add, scalar, code, rows }) {
  for (const [field, label] of [
    ['images', at('adjunct.failureDetails.copy28')],
    ['videos', at('adjunct.failureDetails.copy29')],
    ['audios', at('adjunct.failureDetails.copy30')],
  ]) {
    const entry = inputs?.[field];
    add(label + at('adjunct.failureDetails.copy31'), entry?.count);
    if (Array.isArray(entry?.durations_seconds) && entry.durations_seconds.length)
      rows.push([
        label + at('adjunct.failureDetails.copy32'),
        entry.durations_seconds.map((value) => scalar(value) ?? '?').join('、'),
      ]);
    add(label + at('adjunct.failureDetails.copy33'), entry?.total_duration_seconds);
  }
  add(at('adjunct.failureDetails.copy34'), inputs?.first_frame);
  add(at('adjunct.failureDetails.copy35'), inputs?.last_frame);
  add(at('adjunct.failureDetails.copy36'), inputs?.video_audio_enabled_count);
  if (Array.isArray(inputs?.guides))
    rows.push([
      at('adjunct.failureDetails.copy37'),
      inputs.guides
        .map((guide) =>
          at('adjunct.failureDetails.copy38', {
            v0: code(guide.kind),
            v1: scalar(guide.time_seconds) ?? '?',
            v2: guide.use_audio ? at('adjunct.failureDetails.copy39') : '',
          }),
        )
        .join('；') || at('adjunct.failureDetails.copy27'),
    ]);
}
export function failureDetails(job, locale = 'zh') {
  const at = (key, values) => translate(locale, key, values);
  const diagnostic = job?.failure_diagnostic;
  if (!diagnostic || diagnostic.version !== 1) return null;
  const code = (value) => diagnosticCode(value, at),
    scalar = (value) => diagnosticScalar(value, at),
    rows = [];
  const add = (label, value) => {
    const text = scalar(value);
    if (text !== null) rows.push([label, text]);
  };
  add(at('adjunct.failureDetails.copy21'), job.id);
  add(at('adjunct.failureDetails.copy22'), diagnostic.mode);
  add(at('adjunct.failureDetails.copy23'), diagnostic.profile_id);
  for (const field of [
    'duration',
    'resolution',
    'aspect_ratio',
    'steps',
    'sampler_name',
    'scheduler',
    'seed',
    'denoise',
    'shift_video',
    'generate_audio',
    'video_conditioning',
    'audio_conditioning',
    'image_refs_relative_size',
    'audio_refinement',
    'mask_mode',
    'export_crf',
  ])
    add(
      Object.hasOwn(CONTROL_LABELS, field) ? at('adjunct.control.' + field) : field,
      diagnostic.controls?.[field],
    );
  for (const [field, label] of [
    ['video_excerpts', at('adjunct.failureDetails.copy24')],
    ['audio_excerpts', at('adjunct.failureDetails.copy25')],
  ])
    if (Array.isArray(diagnostic.controls?.[field]))
      rows.push([
        label,
        diagnostic.controls[field]
          .map((entry) =>
            at('adjunct.failureDetails.copy26', {
              v0: scalar(entry.time_seconds) ?? '?',
              v1: scalar(entry.duration_seconds) ?? '?',
            }),
          )
          .join('；') || at('adjunct.failureDetails.copy27'),
      ]);
  inputRows(diagnostic.inputs, { at, add, scalar, code, rows });
  for (const [field, label] of [
    ['width', at('adjunct.failureDetails.copy41')],
    ['height', at('adjunct.failureDetails.copy42')],
    ['frames', at('adjunct.failureDetails.copy43')],
    ['fps', at('adjunct.failureDetails.copy44')],
    ['duration_s', at('adjunct.failureDetails.copy45')],
  ])
    add(label, diagnostic.output?.[field]);
  for (const [field, label] of [
    ['provider', at('adjunct.failureDetails.copy46')],
    ['machine_id', at('adjunct.failureDetails.copy47')],
    ['offer_id', at('adjunct.failureDetails.copy48')],
    ['provider_instance_id', at('adjunct.failureDetails.copy49')],
    ['node_id', at('adjunct.failureDetails.copy50')],
    ['gpu_type', 'GPU'],
  ])
    add(label, diagnostic.host?.[field]);
  return {
    stage:
      {
        preflight: at('adjunct.failureDetails.copy51'),
        input: at('adjunct.failureDetails.copy52'),
        submit: at('adjunct.failureDetails.copy53'),
        generation: at('adjunct.failureDetails.copy54'),
        collect: at('adjunct.failureDetails.copy55'),
        export: at('adjunct.failureDetails.copy56'),
        runtime: at('adjunct.failureDetails.copy57'),
      }[diagnostic.stage] || code(diagnostic.stage),
    reason: code(diagnostic.reason_code),
    rows,
  };
}
export function estimateLabel(estimate, locale = 'zh') {
  if (!estimate || estimate.cost_microusd == null) return translate(locale, 'jobs.noQuote');
  if (estimate.source === 'mock' || estimate.kind === 'simulation')
    return translate(locale, 'jobs.mockCost');
  if (estimate.kind === 'included') return translate(locale, 'jobs.includedCost');
  if (estimate.kind === 'budget_reservation')
    return translate(locale, 'jobs.reservedCost', {
      amount: formatMoneyMicros(estimate.cost_microusd),
      currency: estimate.currency || 'USD',
    });
  if (typeof estimate.cost_microusd === 'number')
    return translate(locale, 'jobs.estimatedCost', {
      amount: formatMoneyMicros(estimate.cost_microusd),
      currency: estimate.currency || 'USD',
    });
  return translate(locale, 'jobs.costUnavailable');
}
export function stableJSON(value) {
  if (Array.isArray(value)) return '[' + value.map(stableJSON).join(',') + ']';
  if (value && typeof value === 'object')
    return (
      '{' +
      Object.keys(value)
        .sort()
        .map((key) => JSON.stringify(key) + ':' + stableJSON(value[key]))
        .join(',') +
      '}'
    );
  return JSON.stringify(value);
}
export async function digest(value) {
  const bytes = new TextEncoder().encode(stableJSON(value));
  return [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))]
    .map((n) => n.toString(16).padStart(2, '0'))
    .join('');
}
export function blankCloudProject(title) {
  return {
    schemaVersion: 4,
    id: 'project-' + crypto.randomUUID(),
    title: title.trim(),
    logline: '',
    entities: [],
    links: [],
    layout: { positions: {}, viewport: { x: 0, y: 0, zoom: 1 } },
    jobs: [],
    updatedAt: new Date().toISOString(),
    journey: { stage: 1 },
  };
}
export function freestyleProject(title) {
  const p = blankCloudProject(title),
    chapter = 'chapter-' + crypto.randomUUID(),
    scene = 'scene-' + crypto.randomUUID(),
    shot = 'shot-' + crypto.randomUUID();
  p.entities = [
    { id: chapter, type: 'chapter', title: '快速创作', parentId: null, data: {} },
    { id: scene, type: 'scene', title: '单镜场景', parentId: chapter, data: { script: '' } },
    {
      id: shot,
      type: 'shot',
      title: '我的视频',
      parentId: scene,
      data: { seconds: 5, prompt: '' },
    },
  ].map((e) => ({ ...e, description: '', order: 0, version: 1, status: 'draft' }));
  p.journey = { workspace: 'freestyle', stage: 4, reviewShotId: shot };
  return p;
}
export function copyForCloud(project) {
  const p = clone(project);
  p.id = 'project-' + crypto.randomUUID();
  p.jobs = [];
  p.updatedAt = new Date().toISOString();
  for (const e of p.entities) {
    const asset = project.entities.find((a) => a.id === e.data.selectedAssetId);
    if (boundVideoRange(e.data.selectedVideoRange, asset)) {
      e.data.selectedVideoRange.cloudAssetId = null;
      e.data.selectedVideoRange.cloudArtifactId = null;
    }
  }
  for (const e of p.entities) {
    delete e.data.cloudAssetId;
    delete e.data.cloudArtifactId;
    delete e.data.cloudContentPath;
  }
  for (const tracks of Object.values(p.journey?.soundTracks || {}))
    if (Array.isArray(tracks))
      for (const track of tracks) if (track.generatedFrom) track.needsReview = true;
  return p;
}
export function checkedProjectRecord(record) {
  const project = record?.project,
    checked = validateProject(project);
  if (
    !checked.ok ||
    !Number.isInteger(record.version) ||
    record.version < 1 ||
    record.id !== project.id
  )
    throw Error(checked.error || '云项目响应无效，未替换当前作品。');
  return { ...record, project: checked.project };
}

// A chosen clip range belongs to the current file and runs forward from zero or later.
function validRange(range, asset) {
  return (
    range.fileId === asset.data.fileId &&
    Number.isFinite(range.start) &&
    Number.isFinite(range.end) &&
    range.start >= 0 &&
    range.end > range.start
  );
}
// All-reference shots also send each cast member's look gallery and each place's references.
function identityReferences(project, shot, { find, references, issues, add, at }) {
  const shotId = shot.id;
  const scene = find(shot.parentId),
    characters = new Set(
      [...(scene?.data.cast || []), ...(shot.data.cast || [])].map((c) => c.characterId),
    );
  for (const link of project.links.filter(
    (l) => l.target === shotId && l.role === 'identity' && find(l.source)?.type === 'character',
  ))
    characters.add(link.source);
  for (const id of characters) {
    const character = find(id),
      binding = effectiveLook(project, shot, id),
      look = character?.data.looks?.find((l) => l.id === binding?.lookId);
    if (!look) {
      issues.push(
        at('adjunct.referenceSpecs.copy64', {
          v0: character?.title || at('adjunct.referenceSpecs.copy63'),
        }),
      );
      continue;
    }
    const gallery = Object.values(look.gallery || {}).filter(Boolean);
    if (!gallery.length) issues.push(at('adjunct.referenceSpecs.copy66', { v0: look.name }));
    for (const assetId of gallery) {
      const asset = find(assetId);
      if (!references.some((r) => r.entity.id === assetId)) add(asset, 'identity');
    }
  }
  for (const id of shotLocationIds(project, shot)) {
    const location = find(id);
    if (location?.type !== 'location') {
      issues.push(at('adjunct.referenceSpecs.copy67'));
      continue;
    }
    for (const assetId of location.data.referenceAssetIds || [])
      if (!references.some((ref) => ref.entity.id === assetId)) add(find(assetId), 'reference');
  }
}
export function referenceSpecs(project, shotId, { mode, locale = 'zh' } = {}) {
  const at = (key, values) => translate(locale, key, values);
  const shot = project.entities.find((e) => e.id === shotId);
  if (!shot || !['shot', 'generation'].includes(shot.type))
    return { references: [], issues: [at('adjunct.referenceSpecs.copy58')] };
  const find = (id) => project.entities.find((e) => e.id === id),
    references = [],
    issues = [];
  const add = (asset, purpose, range = null, linkId = null) => {
    if (!asset || !['image', 'video', 'audio'].includes(asset.type)) {
      issues.push(at('adjunct.referenceSpecs.copy59'));
      return;
    }
    if (!asset.data.fileId || asset.data.missingFile) {
      issues.push(at('adjunct.referenceSpecs.copy60', { v0: asset.title }));
      return;
    }
    if (range && !validRange(range, asset)) {
      issues.push(at('adjunct.referenceSpecs.copy61', { v0: asset.title }));
      return;
    }
    references.push({
      entity: asset,
      purpose,
      range,
      linkId,
      key: [asset.id, asset.data.fileId, purpose, range?.start ?? '', range?.end ?? ''].join('|'),
    });
  };
  for (const link of project.links.filter((l) => l.target === shotId)) {
    const asset = find(link.source);
    if (['image', 'video', 'audio'].includes(asset?.type))
      add(asset, link.role, shot.data.referenceRanges?.[link.id], link.id);
    else if (link.role === 'dependency')
      issues.push(
        at('adjunct.referenceSpecs.copy62', {
          v0: asset?.title || at('adjunct.referenceSpecs.copy63'),
        }),
      );
  }
  // Shared visual identity is an ordinary REF input. FL retains the story's
  // cast/location assignments but only sends explicitly chosen frame inputs.
  if ((mode ?? shot.data.h3?.inputMode) !== 'fl')
    identityReferences(project, shot, { find, references, issues, add, at });
  return { references, issues: [...new Set(issues)] };
}
export function selectedRecipe(recipes, h3 = {}) {
  return h3.recipeId
    ? recipes.find((recipe) => recipe.id === h3.recipeId)
    : h3.inputMode
      ? recipes.find((recipe) => recipe.mode === h3.inputMode)
      : recipes[0];
}
export function selectedProfileRecipe(capabilities, h3 = {}) {
  const base = selectedRecipe(capabilities?.recipes || [], h3);
  if (!base) return base;
  const resolved = recipeFor(capabilities, {
    recipe_id: base.id,
    deployment_profile_id: h3.deploymentProfileId || null,
  });
  return {
    ...resolved,
    controls: effectiveControlSchema(resolved),
    deployment_profile_id: h3.deploymentProfileId || null,
  };
}
export function effectiveReferenceCount(project, shotId, kind) {
  const { references } = referenceSpecs(project, shotId);
  // The service counts ordinary inputs separately from reusable time guides.
  return new Set(
    references
      .filter(
        (ref) => ref.entity.type === kind && !['firstFrame', 'lastFrame'].includes(ref.purpose),
      )
      .map((ref) => ref.entity.id),
  ).size;
}
export function shotSnapshot(project, shotId) {
  const shot = project.entities.find((e) => e.id === shotId);
  if (!shot) return null;
  const scene = project.entities.find((e) => e.id === shot.parentId),
    { references } = referenceSpecs(project, shotId);
  return {
    id: shot.id,
    version: shot.version,
    description: shot.description,
    prompt: shot.data.prompt || '',
    seconds: shot.data.seconds ?? 5,
    h3: shot.data.h3 || {},
    cast: shot.data.cast || [],
    scene: {
      id: scene?.id || null,
      script: scene?.data.script || '',
      cast: scene?.data.cast || [],
    },
    references: references.map((r) => ({
      key: r.key,
      assetId: r.entity.id,
      fileId: r.entity.data.fileId,
      cloudAssetId: r.entity.data.cloudAssetId || null,
      version: r.entity.version,
    })),
    guides: (shot.data.h3?.guides || []).map((g) => {
      const e = project.entities.find((e) => e.id === g.media_id);
      return {
        id: g.media_id,
        fileId: e?.data.fileId || null,
        cloudAssetId: e?.data.cloudAssetId || null,
        version: e?.version || null,
      };
    }),
    links: project.links.filter((l) => l.target === shot.id),
    ...(shot.data.storyboard
      ? {
          storyboard: shot.data.storyboard,
          selectedAssetId: shot.data.selectedAssetId || null,
          style: project.journey?.storyboard?.style || '',
          sources: storyboardSources(project, shotId),
        }
      : {}),
  };
}
// UI staleness checks cover this shot's source closure. An unrelated upload or
// another shot's completed candidate must not invalidate a confirmed batch.
// The shot just before this one in reading order across its chapter's scenes.
function previousShotInChapter(project, scene, shotId) {
  const chapter = project.entities.find(
      (c) =>
        c.type === 'chapter' &&
        project.entities.some((s) => s.parentId === c.id && s.id === scene?.id),
    ),
    scenes = project.entities
      .filter((e) => e.parentId === chapter?.id && e.type === 'scene')
      .sort((a, b) => a.order - b.order),
    shots = scenes.flatMap((s) =>
      project.entities
        .filter((e) => e.parentId === s.id && e.type === 'shot')
        .sort((a, b) => a.order - b.order),
    );
  return shots[shots.findIndex((e) => e.id === shotId) - 1];
}
// Media an entity points at: images, references, look galleries, versions, voices, ambience.
function entitySources(d) {
  return [
    ...(d.imageIds || []),
    ...(d.referenceAssetIds || []),
    ...(d.looks || []).flatMap((v) => v.imageIds ?? Object.values(v.gallery || {})),
    ...(d.versions || []).flatMap((v) => v.imageIds || []),
    ...(d.voices || []).map((v) => v.assetId),
    d.ambient?.assetId,
  ];
}
// What the shot names itself: cast, place, props, frames, extra refs and time anchors.
function directSources(shot, scene, b) {
  return [
    ...(Object.hasOwn(shot?.data || {}, 'cast') ? shot.data.cast : scene?.data.cast || []).map(
      (c) => c.characterId,
    ),
    b.place?.locationId,
    ...(b.propIds || []),
    b.firstFrameId,
    b.lastFrameId,
    ...(b.extraRefs || []).map((r) => r.entityId),
    b.previousFrame?.frameAssetId,
    ...(shot?.data.h3?.guides || []).map((g) => g.media_id),
  ];
}
export function storyboardSources(project, shotId) {
  const lookup = new Map(project.entities.map((e) => [e.id, e])),
    shot = lookup.get(shotId),
    scene = lookup.get(shot?.parentId),
    b = shot?.data.storyboard || {},
    ids = new Set(),
    pending = directSources(shot, scene, b);
  if (!Object.hasOwn(b, 'place')) pending.push(shot?.data.locationId || scene?.data.locationId);
  if (b.continuePrevious)
    pending.push(previousShotInChapter(project, scene, shotId)?.data.selectedAssetId);
  while (pending.length) {
    const id = pending.pop(),
      e = lookup.get(id);
    if (!e || ids.has(id)) continue;
    ids.add(id);
    pending.push(...entitySources(e.data || {}));
  }
  return [...ids].sort().map((id) => lookup.get(id));
}
// Only operational settings with one advertised executable choice are automatic.
// Never change creative controls or infer a pool requirement from model defaults.
export function executionPresetControls(recipe) {
  const preset = recipe?.deployment_preset,
    required = recipe?.execution_support?.constraints?.controls || {};
  if (preset?.applies_to !== 'unset_controls_only') return {};
  return Object.fromEntries(
    ['encoder_device', 'video_decode']
      .filter((field) => {
        const value = preset.controls?.[field],
          allowed = required[field],
          schema = recipe.controls?.[field];
        return (
          schema?.available !== false &&
          schema?.enum?.includes(value) &&
          Array.isArray(allowed) &&
          allowed.length === 1 &&
          allowed[0] === value
        );
      })
      .map((field) => [field, preset.controls[field]]),
  );
}
export function controlsForRecipe(recipe, saved = {}, seconds = 5) {
  const result = {};
  for (const [field, schema] of Object.entries(recipe?.controls || {})) {
    if (schema.available === false || ['guides', 'video_audio'].includes(field)) continue;
    if (Object.hasOwn(saved, field) && saved[field] !== '' && saved[field] !== null)
      result[field] = saved[field];
    else if (
      !Object.hasOwn(saved, field) &&
      recipe?.deployment_preset?.applies_to === 'unset_controls_only' &&
      ['encoder_device', 'video_decode'].includes(field) &&
      schema.enum?.includes(recipe.deployment_preset.controls?.[field])
    )
      result[field] = recipe.deployment_preset.controls[field];
    else if (field === 'duration') result[field] = seconds;
    else if (Object.hasOwn(schema, 'default')) result[field] = schema.default;
  }
  return { ...result, ...executionPresetControls(recipe) };
}
export function validateControlValues(recipe, controls) {
  const issues = [];
  for (const [field, value] of Object.entries(controls)) {
    const schema = recipe?.controls?.[field];
    if (!schema) {
      issues.push(`当前配方不支持 ${field}。`);
      continue;
    }
    if (schema.enum && !schema.enum.includes(value)) {
      issues.push(`${schema.title || field} 不在支持的选项内。`);
      continue;
    }
    if (value === null && Array.isArray(schema.type) && schema.type.includes('null')) continue;
    if (field === 'seed') {
      if (
        typeof value !== 'string' ||
        !/^\d{1,20}$/.test(value) ||
        BigInt(value) > 18446744073709551615n
      )
        issues.push('种子须为 uint64 十进制字符串。');
      continue;
    }
    if (!validControlValue(value, schema))
      issues.push(`${schema.title || field} 超出支持的类型或范围。`);
  }
  return issues;
}
// The shot's linked media as plan inputs: one first/last frame, each other file once.
function referenceInputs(shot, references, assetMap, issues) {
  const inputs = { images: [], videos: [], audios: [], guides: [] },
    seen = new Set();
  for (const ref of references) {
    const assetId = assetMap[ref.key] || (!ref.range ? ref.entity.data.cloudAssetId : null);
    if (!assetId) {
      issues.push(`「${ref.entity.title}」还未同步或生成实际选段。`);
      continue;
    }
    const kind = ref.entity.type,
      role = ref.purpose;
    if (['firstFrame', 'lastFrame'].includes(role)) {
      const key = role === 'firstFrame' ? 'first_frame' : 'last_frame';
      if (inputs[key]) issues.push('同一个镜头只能选择一张首帧和一张尾帧。');
      inputs[key] = { asset_id: assetId };
      continue;
    }
    if (seen.has(assetId)) continue;
    seen.add(assetId);
    const entry = { asset_id: assetId, purpose: role };
    if (kind === 'video')
      entry.include_audio = shot.data.h3?.video_audio?.[ref.entity.id] !== false;
    inputs[{ image: 'images', video: 'videos', audio: 'audios' }[kind]].push(entry);
  }
  return inputs;
}
// Time anchors on the shot, as uploaded media ids.
function guideInputs(project, shot, assetMap, inputs, issues) {
  for (const [guideIndex, guide] of (shot.data.h3?.guides || []).entries()) {
    const entity = project.entities.find((e) => e.id === guide.media_id),
      assetId =
        assetMap['guide:' + guideIndex] || (!guide.source_range ? entity?.data.cloudAssetId : null);
    if (!assetId) {
      issues.push('有时间锚点的素材还未同步。');
      continue;
    }
    inputs.guides.push({
      media_id: assetId,
      time_seconds: guide.time_seconds,
      use_audio: entity?.type === 'video' && !!guide.use_audio,
    });
  }
}
// Which input roles the chosen recipe accepts, and that all-reference has a reference.
function inputRoleIssues(recipe, inputs, issues) {
  const extended = recipe.limits?.input_count_semantics === 'native_roles_v2';
  if (extended) {
    for (const [slot, kind] of [
      ['images', 'image'],
      ['videos', 'video'],
      ['audios', 'audio'],
      ['first_frame', 'image'],
      ['last_frame', 'image'],
    ])
      if (
        (Array.isArray(inputs[slot]) ? inputs[slot].length : inputs[slot]) &&
        !roleSupported(recipe, slot, kind)
      )
        issues.push(`所选配方不接受此输入用途：${slot}，请调整用途或生成方式。`);
  } else {
    if (
      recipe.mode === 'fl' &&
      (inputs.images.length || inputs.videos.length || inputs.audios.length)
    )
      issues.push('首尾帧配方不能混用全能参考；请切换配方或解除这些关联。');
    if (recipe.mode === 'ref' && (inputs.first_frame || inputs.last_frame))
      issues.push('全能参考配方不接受首尾帧约束；请切换配方或更改参考用途。');
  }
  if (
    recipe.mode === 'ref' &&
    !inputs.images.length &&
    !inputs.videos.length &&
    !inputs.audios.length &&
    !(extended && (inputs.first_frame || inputs.last_frame || inputs.guides.length))
  )
    issues.push('全能参考至少需要一份参考素材；纯文字请选择文生/首尾帧配方。');
}
// Where the plan belongs: project, chapter, scene and the exact shot version.
function planClientRef(project, shot, sourceHash) {
  const parents = ancestors(project, shot.id);
  return {
    project_id: project.id,
    chapter_id: parents.find((e) => e.type === 'chapter')?.id || null,
    scene_id: parents.find((e) => e.type === 'scene')?.id || null,
    shot_id: shot.id,
    shot_version: shot.version,
    ...(sourceHash ? { source_hash: sourceHash } : {}),
  };
}
export function buildPlanPayload(
  project,
  shotId,
  { recipe, capabilitiesVersion, assetMap = {}, sourceHash } = {},
) {
  const shot = project.entities.find((e) => e.id === shotId);
  if (!shot) throw Error('镜头已被删除。');
  if (!recipe) throw Error('先选择有明确能力说明的配方。');
  const { references, issues } = referenceSpecs(project, shotId, { mode: recipe.mode }),
    controls = controlsForRecipe(recipe, shot.data.h3?.controls, shot.data.seconds);
  issues.push(...validateControlValues(recipe, controls));
  const prompt = String(shot.data.prompt ?? shot.description ?? '').trim();
  if (!prompt) issues.push('先写清这个镜头的画面与动作。');
  const inputs = referenceInputs(shot, references, assetMap, issues);
  guideInputs(project, shot, assetMap, inputs, issues);
  inputRoleIssues(recipe, inputs, issues);
  if (issues.length) throw Error([...new Set(issues)].join(' '));
  return {
    client_ref: planClientRef(project, shot, sourceHash),
    recipe_id: recipe.id,
    ...(recipe.deployment_profile_id
      ? { deployment_profile_id: recipe.deployment_profile_id }
      : {}),
    capabilities_version: capabilitiesVersion,
    prompt,
    inputs,
    controls,
    client_edit: { edit_duration_s: shot.data.seconds },
  };
}

// A separate sound stem the worker returns next to the video (FLAC).
function isStemAudio(artifact) {
  return (
    artifact.kind === 'audio' &&
    ['audio/flac', 'audio/x-flac'].includes(
      artifact.mime || artifact.content_type || artifact.metadata?.mime,
    )
  );
}
// Guided artifact.adopt may choose its own entity ID. Keep that identity, selection and review
// data instead of manufacturing a duplicate result; returns whether the shot's takes changed.
function keepAdopted(shot, attached, artifact, isAudio) {
  if (
    !shot ||
    isAudio ||
    attached.type !== artifact.kind ||
    shot.data.candidateIds?.includes(attached.id)
  )
    return false;
  shot.data.candidateIds = [...(shot.data.candidateIds || []), attached.id];
  return true;
}
// The library entity for one generated file; never adopted automatically.
function resultEntity(project, job, artifact, { id, shot, old, isAudio, now }) {
  return {
    id,
    type: artifact.kind,
    title: `${(shot?.title || '历史镜头').slice(0, 120)} · ${job.simulation ? '模拟 ' : ''}${old ? '旧版 ' : ''}v${job.client_ref.shot_version} ${isAudio ? '独立声音' : '候选'}`,
    description: job.simulation
      ? '模拟任务产物，不代表 H3 真实生成；未自动采用。'
      : '实际任务结果；未自动采用。',
    parentId: null,
    order: project.entities.filter((e) => e.parentId === null).length,
    version: 1,
    status: 'draft',
    data: {
      fileId: 'cloud_artifact_' + artifact.id,
      cloudArtifactId: artifact.id,
      cloudContentPath: artifact.content_url || null,
      fileName:
        artifact.filename ||
        `${artifact.id}.${artifact.kind === 'image' ? 'png' : isAudio ? 'flac' : 'mp4'}`,
      mime:
        artifact.mime ||
        artifact.content_type ||
        (artifact.kind === 'image' ? 'image/png' : isAudio ? 'audio/flac' : 'video/mp4'),
      bytes: artifact.size_bytes || artifact.size || 0,
      metadata: artifact.metadata || {},
      simulation: !!job.simulation,
      source: 'generation',
      sourceJobId: job.id,
      sourceShotId: job.client_ref.shot_id,
      sourceShotVersion: job.client_ref.shot_version,
      sourceHash: job.client_ref.source_hash || null,
      oldVersion: old,
      missingFile: false,
      createdAt: now,
    },
  };
}
// The take was made from an older version of the shot (or a different storyboard snapshot).
function staleTake(shot, job, snapshotHash) {
  return !!(
    shot &&
    (shot.version !== job.client_ref.shot_version ||
      (job.client_ref.source_hash && snapshotHash && job.client_ref.source_hash !== snapshotHash))
  );
}
// Adds one generated file to the library and the shot's takes. Returns the new entity id,
// 'adopted' when an existing adopted entity was linked instead, or null when nothing changed.
function importArtifact(project, job, artifact, { shot, old, same, audioSeen, now }) {
  const isAudio = isStemAudio(artifact);
  if (!artifact.id || (!['video', 'image'].includes(artifact.kind) && !isAudio)) return null;
  const id = 'result-' + artifact.id;
  if (isAudio && audioSeen.has(artifact.id)) return null;
  if (same && !isAudio) return null;
  if (isAudio) audioSeen.add(artifact.id);
  const attached = project.entities.find((e) => e.data?.cloudArtifactId === artifact.id);
  if (attached) return keepAdopted(shot, attached, artifact, isAudio) ? 'adopted' : null;
  if (project.entities.some((e) => e.id === id)) return null;
  project.entities.push(resultEntity(project, job, artifact, { id, shot, old, isAudio, now }));
  if (shot && !isAudio) {
    shot.data.candidateIds = [...new Set([...(shot.data.candidateIds || []), id])];
    if (old) shot.status = 'review';
  }
  return id;
}
export function mergeJobCandidate(
  project,
  job,
  { snapshotHash, now = new Date().toISOString() } = {},
) {
  if (!job?.id || job.client_ref?.project_id !== project.id)
    return { changed: false, reason: 'foreign-project' };
  const before = project.jobs.find((j) => j.id === job.id),
    { clientImportedAudioIds: importedAudio = [], ...previous } = before || {},
    same = !!before && stableJSON(previous) === stableJSON(job),
    audioSeen = new Set(importedAudio);
  project.jobs = [...project.jobs.filter((j) => j.id !== job.id), clone(job)].slice(-10000);
  if (job.recipe_id === 'chapter-roughcut-v1')
    return { changed: !same, added: [], reason: 'chapter-deliverable' };
  if (job.status !== 'succeeded') return { changed: !same, added: [] };
  const shot = project.entities.find((e) => e.id === job.client_ref.shot_id),
    old = staleTake(shot, job, snapshotHash),
    added = [];
  let candidateChanged = false;
  for (const artifact of job.artifacts || []) {
    const outcome = importArtifact(project, job, artifact, { shot, old, same, audioSeen, now });
    if (outcome === 'adopted') candidateChanged = true;
    else if (outcome) added.push(outcome);
  }
  const markerChanged = audioSeen.size !== importedAudio.length;
  if (audioSeen.size)
    project.jobs.find((j) => j.id === job.id).clientImportedAudioIds = [...audioSeen];
  return {
    changed: !same || added.length > 0 || markerChanged || candidateChanged,
    added,
    oldVersion: old,
    ...(same && !added.length && !markerChanged && !candidateChanged
      ? { reason: 'unchanged' }
      : {}),
  };
}
