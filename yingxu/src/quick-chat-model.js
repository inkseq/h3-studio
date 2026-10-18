/* Presentation and input validation only. Capability/preflight remain authoritative. */
export const CHAT_MODELS = [
  { id: 'gemini-3.8-flash', label: 'Gemini 3.8 Flash' },
  { id: 'gemma-4-31b-it', label: 'Gemma 4 31B IT' },
];
export const ROLE_LABELS = {
  reference: '普通参考',
  first_frame: '首帧',
  last_frame: '尾帧',
  guide: '时间锚点',
};
export const CONTROL_LABELS = {
  duration: '生成时长（秒）',
  resolution: '原生分辨率',
  aspect_ratio: '画幅',
  generate_audio: '导出生成声音',
  seed: '随机种子',
  steps: '采样步数',
  sampler_name: '采样器',
  scheduler: '采样日程',
  denoise: '去噪日程范围',
  ref_image_size: '参考图细节',
  shift_video: '视频 sigma shift',
  shift_audio: '音频 sigma shift',
  video_decode: '视频 VAE 解码',
  audio_decode: '音频 VAE 解码',
  encoder_device: '编码器设备',
  video_tile_size: '空间块大小',
  video_overlap: '空间重叠',
  video_temporal_size: '时间块大小',
  video_temporal_overlap: '时间重叠',
  audio_tile_size: '音频分块大小',
  audio_overlap: '音频分块重叠',
  width: '自定义宽度',
  height: '自定义高度',
  export_crf: '导出压缩 CRF',
};
Object.assign(CONTROL_LABELS, {
  video_conditioning: '视频参与方式',
  audio_conditioning: '声音参与方式',
  image_refs_relative_size: '参考图相对大小（%）',
  audio_refinement: '音频细化',
  mask_mode: '控制视频遮罩',
  video_excerpts: '视频参考摘录',
  audio_excerpts: '视频原声参考摘录',
});
export const CONTROL_GROUPS = [
  ['常用设置', ['duration', 'resolution', 'aspect_ratio', 'generate_audio']],
  [
    '参考与时间',
    [
      'video_conditioning',
      'audio_conditioning',
      'image_refs_relative_size',
      'ref_image_size',
      'video_excerpts',
      'audio_excerpts',
    ],
  ],
  ['随机性与采样', ['seed', 'steps', 'sampler_name', 'scheduler', 'denoise', 'mask_mode']],
  ['音画日程 · 实验项', ['shift_video', 'shift_audio', 'audio_refinement']],
  [
    '运行与导出',
    [
      'video_decode',
      'audio_decode',
      'encoder_device',
      'video_tile_size',
      'video_overlap',
      'video_temporal_size',
      'video_temporal_overlap',
      'export_crf',
      'audio_tile_size',
      'audio_overlap',
    ],
  ],
];
export const clone = (value) => structuredClone(value);
const minimum = (...values) => {
  const known = values.filter(Number.isFinite);
  return known.length ? Math.min(...known) : null;
};
export function recipeFor(capabilities, settings = {}) {
  const base =
    capabilities?.recipes?.find((recipe) => recipe.id === settings.recipe_id) ||
    capabilities?.recipes?.find((recipe) => recipe.mode === 'fl') ||
    null;
  if (!base || !settings.deployment_profile_id) return base;
  const profile = capabilities?.deployment_profiles?.find(
      (item) => item.id === settings.deployment_profile_id,
    ),
    support = profile?.generation_support?.[base.mode];
  // An explicit deployment never inherits another pool's clamps or presets.
  // Keep authored values; backend preflight validates the selected profile.
  return {
    ...base,
    model_id: profile?.model_id || base.model_id,
    controls: support?.controls || base.controls,
    limits: support?.limits || {},
    input_support: support?.input_support || null,
    custom_canvas_constraints: support?.custom_canvas_constraints || null,
    execution_support: support || {
      configured: false,
      enabled: false,
      reason: '原部署配方当前未公开。',
    },
    deployment_preset: null,
  };
}
export function effectiveControlSchema(recipe) {
  const envelope = recipe?.execution_support?.constraints || {},
    result = {};
  for (const [field, raw] of Object.entries(recipe?.controls || {})) {
    const schema = clone(raw),
      allowed = envelope.controls?.[field];
    if (schema.enum && allowed)
      schema.enum = schema.enum.filter((value) => allowed.includes(value));
    if (field === 'steps')
      schema.maximum = minimum(schema.maximum, envelope.max_steps) ?? schema.maximum;
    if (
      field === 'duration' &&
      Number.isFinite(envelope.max_duration_seconds) &&
      schema.type === 'integer'
    ) {
      const frames = (seconds) => {
        const raw = Math.max(5, Math.round(seconds * 24));
        return raw + ((5 - (raw % 17) + 17) % 17);
      };
      let max = schema.maximum;
      while (max >= schema.minimum && frames(max) / 24 > envelope.max_duration_seconds + 1e-7)
        max--;
      schema.maximum = max;
      if (max < schema.minimum) {
        schema.available = false;
        schema.reason = '当前执行池时长范围无法容纳原生补帧，请等待配置恢复。';
      }
    }
    // Native frame alignment can exceed requested seconds. Do not treat the
    // native allowance as an exact exported-duration limit; preflight decides.
    if (schema.enum && !schema.enum.length) {
      schema.available = false;
      schema.reason = '当前执行池未开放此选项。';
    }
    if (field === 'generate_audio' && envelope.allow_audio === false) {
      schema.available = false;
      schema.reason = '当前执行池不导出声音。';
      schema.default = false;
    }
    result[field] = schema;
  }
  return result;
}
export function defaultsFor(capabilities, recipeId, deploymentProfileId = null) {
  const recipe = recipeFor(capabilities, {
    recipe_id: recipeId,
    deployment_profile_id: deploymentProfileId,
  });
  if (!recipe) return { recipe_id: recipeId || '', controls: {}, copies: 1 };
  const schema = effectiveControlSchema(recipe),
    preset = recipe.deployment_preset?.controls || {},
    controls = {};
  for (const [field, item] of Object.entries(schema))
    if (
      item.available !== false &&
      !['guides', 'video_audio'].includes(field) &&
      Object.hasOwn(item, 'default')
    ) {
      let value = clone(item.default);
      if (item.enum && !item.enum.includes(value)) value = item.enum[0];
      if (Number.isFinite(value) && Number.isFinite(item.maximum))
        value = Math.min(value, item.maximum);
      controls[field] = value;
    }
  for (const [field, value] of Object.entries(preset))
    if (schema[field]?.enum?.includes(value)) controls[field] = value;
  return { recipe_id: recipe.id, controls, copies: 1 };
}
export function nextSettingsFor(capabilities, schema, { draft, session } = {}) {
  // A changed service default never rewrites authored settings or history.
  if (draft) return draft;
  if (session) return session;
  const next = clone(schema?.default_next_settings || defaultsFor(capabilities)),
    id = next.deployment_profile_id;
  if (!id) return next;
  const base = capabilities?.recipes?.find((item) => item.id === next.recipe_id),
    profile = capabilities?.deployment_profiles?.find((item) => item.id === id),
    support = profile?.generation_support?.[base?.mode];
  // Only the server's explicit, presently enabled default may be selected.
  // Do not guess the first available profile or merge generic control defaults.
  if (
    capabilities?.default_deployment_profile_id !== id ||
    !base ||
    support?.configured !== true ||
    support?.enabled !== true ||
    settingsProblems(next, recipeFor(capabilities, next)).length
  )
    return defaultsFor(capabilities);
  return next;
}
export function effectiveLimits(recipe) {
  const base = recipe?.limits || {},
    envelope = recipe?.execution_support?.constraints || {},
    pool = envelope.input_limits || {};
  return {
    max_images: minimum(base.max_images, pool.max_images),
    max_videos: minimum(base.max_videos, pool.max_videos),
    max_audios: minimum(base.max_audios, pool.max_audios),
    max_total_files: minimum(base.max_total_files, envelope.max_reference_files),
    max_guides: minimum(base.max_guides, envelope.max_guides),
    min_clip_duration: base.min_clip_duration ?? null,
    max_clip_duration: base.max_clip_duration ?? null,
    max_video_clip_duration: minimum(
      base.max_video_clip_duration,
      base.max_clip_duration,
      pool.max_video_duration_seconds,
    ),
    max_audio_clip_duration: minimum(
      base.max_audio_clip_duration,
      base.max_clip_duration,
      pool.max_audio_duration_seconds,
    ),
    max_total_video_duration: minimum(
      base.max_total_video_duration,
      pool.max_video_duration_seconds,
    ),
    max_total_audio_duration: minimum(
      base.max_total_audio_duration,
      pool.max_audio_duration_seconds,
    ),
    guide_kinds: pool.guide_kinds || ['image', 'video', 'audio'],
    guide_recipe_ids: pool.guide_recipe_ids || null,
    max_guide_time_seconds: pool.max_guide_time_seconds ?? null,
    allow_first_last: envelope.allow_first_last !== false,
    allow_video_audio: pool.allow_video_audio !== false,
    max_image_pixels: pool.max_image_pixels ?? null,
    max_video_pixels: pool.max_video_pixels ?? null,
  };
}
export const displaySeconds = (value) =>
  Number.isFinite(value) ? String(Number(value.toFixed(2))) : '?';
export function clipLimits(limits, kind) {
  return {
    minimum: limits?.min_clip_duration ?? null,
    maximum: limits?.[`max_${kind}_clip_duration`] ?? limits?.max_clip_duration ?? null,
  };
}
export function mediaDuration(asset) {
  const meta = asset?.metadata || {};
  return meta.source_duration ?? meta.duration ?? meta.duration_s ?? null;
}
export function hasOriginalAudio(asset) {
  return asset?.metadata?.has_audio !== false;
}
export function referenceDuration(ref, asset) {
  return ref.end !== undefined && ref.start !== undefined
    ? ref.end - ref.start
    : mediaDuration(asset);
}
export function roleSupported(recipe, slot, kind) {
  const support = recipe?.input_support || recipe?.execution_support?.input_support;
  if (slot === 'guides' || slot === 'guide') return true; // Validation explains unavailable guide kinds.
  if (['first_frame', 'last_frame'].includes(slot))
    return typeof support?.[slot] === 'boolean' ? support[slot] : recipe?.mode === 'fl';
  if (!support) return recipe?.mode !== 'fl';
  return kind === 'video'
    ? support.reference_video === true || support.video_conditioning === true
    : kind === 'audio'
      ? support.reference_audio === true || support.kept_soundtrack === true
      : support.reference_images === true;
}
export function referenceActive(ref, recipe, kind) {
  return ref.enabled !== false && roleSupported(recipe, ref.role, kind);
}
export function inputProblems(
  refs,
  assets,
  recipe,
  { requireReference = false, controls = {} } = {},
) {
  if (!recipe) return ['尚未读取到模型能力，不能猜测支持的输入。'];
  const limits = effectiveLimits(recipe),
    errors = [],
    byId = new Map(assets.map((asset) => [asset.id || asset.asset_id, asset])),
    active = refs.filter((ref) => referenceActive(ref, recipe, byId.get(ref.asset_id)?.kind)),
    nativeRoles = recipe?.limits?.input_count_semantics === 'native_roles_v2';
  const normal = active.filter((ref) => ref.role === 'reference'),
    guides = active.filter((ref) => ref.role === 'guide');
  for (const role of ['first_frame', 'last_frame'])
    if (active.filter((ref) => ref.role === role).length > 1)
      errors.push(`${ROLE_LABELS[role]}最多一张。`);
  if (
    !limits.allow_first_last &&
    active.some((ref) => ['first_frame', 'last_frame'].includes(ref.role))
  )
    errors.push('当前执行池未开放首尾帧输入。');
  if (
    !nativeRoles &&
    Number.isFinite(limits.max_total_files) &&
    new Set(normal.map((ref) => ref.asset_id)).size > limits.max_total_files
  )
    errors.push(`普通参考合计最多 ${limits.max_total_files} 份。`);
  if (Number.isFinite(limits.max_guides) && guides.length > limits.max_guides)
    errors.push(`当前最多 ${limits.max_guides} 个时间锚点。`);
  if (nativeRoles) {
    const unique = (kind) =>
      new Map(
        active
          .filter(
            (ref) =>
              ['reference', 'guide'].includes(ref.role) && byId.get(ref.asset_id)?.kind === kind,
          )
          .map((ref) => [ref.asset_id, ref]),
      );
    const videos = unique('video'),
      audios = unique('audio'),
      tracks = [...videos.values()].filter((ref) => ref.use_audio === true),
      videoExcerpts = Array.isArray(controls.video_excerpts) ? controls.video_excerpts : [],
      audioExcerpts = Array.isArray(controls.audio_excerpts) ? controls.audio_excerpts : [];
    const images = new Set(
        normal.filter((ref) => byId.get(ref.asset_id)?.kind === 'image').map((ref) => ref.asset_id),
      ).size,
      videoCount =
        controls.video_conditioning === 'denoise' ? 0 : videoExcerpts.length || videos.size,
      audioCount =
        (controls.audio_conditioning || 'reference') === 'reference'
          ? audioExcerpts.length || audios.size + tracks.length
          : 0,
      sharedTracks =
        tracks.length > 0 && tracks.length === videos.size && !audios.size && !audioExcerpts.length;
    const rows = images + videoCount + (sharedTracks ? 0 : audioCount);
    if (Number.isFinite(limits.max_total_files) && rows > limits.max_total_files)
      errors.push(
        `原生参考合计最多 ${limits.max_total_files} 份（摘录按实际条数计，共用视频原声不重复计文件）。`,
      );
    if (Number.isFinite(limits.max_audios) && audioCount > limits.max_audios)
      errors.push(`声音参考最多 ${limits.max_audios} 份（含选用的视频原声和声音摘录）。`);
    if (Number.isFinite(limits.max_videos) && videoCount > limits.max_videos)
      errors.push(`视频参考最多 ${limits.max_videos} 份（含视频摘录）。`);
  }
  if (requireReference && recipe.mode === 'ref' && !(nativeRoles ? active.length : normal.length))
    errors.push('全能参考至少需要一份参考；纯文字可选文生 / 首尾帧。');
  for (const kind of ['image', 'video', 'audio']) {
    const entries = active.filter(
        (ref) =>
          (nativeRoles && kind === 'image'
            ? ref.role === 'reference'
            : ['reference', 'guide'].includes(ref.role)) && byId.get(ref.asset_id)?.kind === kind,
      ),
      unique = new Map(entries.map((ref) => [ref.asset_id, ref]));
    const max =
      limits[`max_${kind === 'image' ? 'images' : kind === 'video' ? 'videos' : 'audios'}`];
    if (Number.isFinite(max) && unique.size > max)
      errors.push(
        `${{ image: '图片', video: '视频', audio: '音频' }[kind]}最多 ${max} 份${nativeRoles && kind === 'image' ? '普通参考' : '（参考和锚点共用）'}。`,
      );
    if (kind !== 'image') {
      const clip = clipLimits(limits, kind);
      let total = 0;
      for (const ref of unique.values()) {
        const asset = byId.get(ref.asset_id),
          duration = referenceDuration(ref, asset),
          source = mediaDuration(asset);
        if (
          !Number.isFinite(duration) ||
          duration < (clip.minimum ?? 2) ||
          duration > (clip.maximum ?? 15) ||
          (ref.start !== undefined &&
            (!Number.isFinite(ref.start) ||
              !Number.isFinite(ref.end) ||
              ref.start < 0 ||
              (Number.isFinite(source) && ref.end > source)))
        )
          errors.push(
            `「${asset.file_name || '素材'}」需选择 ${displaySeconds(clip.minimum)}–${displaySeconds(clip.maximum)} 秒的有效片段。`,
          );
        else total += duration;
      }
      const maximum = limits[`max_total_${kind}_duration`];
      if (Number.isFinite(maximum) && total > maximum + 1e-6)
        errors.push(
          `${kind === 'video' ? '视频' : '音频'}累计片段最多 ${displaySeconds(maximum)} 秒。`,
        );
    }
  }
  for (const ref of active) {
    const asset = byId.get(ref.asset_id);
    if (!asset) {
      errors.push('有引用尚未读取到素材，请刷新会话。');
      continue;
    }
    if (asset.status && asset.status !== 'ready')
      errors.push(`「${asset.file_name || '素材'}」尚未完成上传校验。`);
    if (['first_frame', 'last_frame'].includes(ref.role) && asset.kind !== 'image')
      errors.push('首尾帧只接受图片。');
    if (ref.role === 'guide') {
      if (
        !limits.guide_kinds.includes(asset.kind) ||
        (limits.guide_recipe_ids && !limits.guide_recipe_ids.includes(recipe.id))
      )
        errors.push('当前方式尚未开放这种时间锚点。');
      const raw = Math.max(5, Math.round((controls.duration || 0) * 24)),
        outputSeconds = nativeRoles ? (raw + ((5 - (raw % 17) + 17) % 17)) / 24 : controls.duration,
        end = ref.time_seconds + (asset.kind === 'image' ? 0 : referenceDuration(ref, asset));
      if (
        !Number.isFinite(ref.time_seconds) ||
        ref.time_seconds < 0 ||
        (Number.isFinite(outputSeconds) &&
          (ref.time_seconds >= outputSeconds || (!nativeRoles && end > outputSeconds + 1e-6))) ||
        (Number.isFinite(limits.max_guide_time_seconds) &&
          ref.time_seconds > limits.max_guide_time_seconds)
      )
        errors.push('时间锚点及完整片段须落在生成时长内。');
      if (nativeRoles && asset.kind !== 'image' && ref.time_seconds !== 0)
        errors.push('当前原生视频或声音控制从第 0 秒开始；片段范围请用素材选段设置。');
    }
    if (asset.kind === 'video' && !limits.allow_video_audio && ref.use_audio !== false)
      errors.push('当前执行池未开放参考视频原声。');
  }
  return [...new Set(errors)];
}
export function settingsProblems(settings, recipe, copiesMax = 4) {
  const errors = [],
    schema = effectiveControlSchema(recipe),
    controls = settings.controls || {};
  if (!Number.isInteger(settings.copies) || settings.copies < 1 || settings.copies > copiesMax)
    errors.push('takes.invalidPreferences');
  for (const [field, value] of Object.entries(controls)) {
    const item = schema[field];
    if (!item) {
      errors.push(`服务未声明控制：${field}`);
      continue;
    }
    if (value === undefined) continue;
    if (
      value === null &&
      ((Array.isArray(item.type) && item.type.includes('null')) || field === 'seed')
    )
      continue;
    if (item.available === false) {
      if (value !== item.default)
        errors.push(item.reason || `${CONTROL_LABELS[field] || field}不可用。`);
      continue;
    }
    if (field === 'seed') {
      if (
        typeof value !== 'string' ||
        !/^\d{1,20}$/.test(value) ||
        BigInt(value) > 18446744073709551615n
      )
        errors.push('随机种子须为完整 64 位十进制字符串。');
      continue;
    }
    if (!validControlValue(value, item))
      errors.push(`${CONTROL_LABELS[field] || field}超出服务声明的类型或范围。`);
  }
  if (controls.resolution === 'custom') {
    const { width, height } = controls,
      constraints = recipe?.custom_canvas_constraints || {},
      envelope = recipe?.execution_support?.constraints || {},
      max = minimum(constraints.maximum_pixel_area, envelope.max_pixels);
    if (
      !Number.isFinite(width) ||
      !Number.isFinite(height) ||
      width % 32 ||
      height % 32 ||
      (max && width * height > max) ||
      width / height < (constraints.minimum_aspect_ratio ?? 0.4) ||
      width / height > (constraints.maximum_aspect_ratio ?? 2.5)
    )
      errors.push('自定义宽高须为 32 的倍数，并满足面积、宽高比限制。');
  }
  if (
    controls.video_decode === 'tiled' &&
    (controls.video_overlap >= controls.video_tile_size ||
      controls.video_temporal_overlap >= controls.video_temporal_size)
  )
    errors.push('解码重叠须小于对应块大小。');
  return [...new Set(errors)];
}
export function validControlValue(value, schema) {
  const types = Array.isArray(schema.type) ? schema.type : [schema.type];
  if (value === null) return types.includes('null');
  if (schema.enum && !schema.enum.some((option) => Object.is(option, value))) return false;
  if (types.includes('array'))
    return (
      Array.isArray(value) &&
      (!Number.isInteger(schema.maxItems) || value.length <= schema.maxItems) &&
      (!schema.items || value.every((item) => validControlValue(item, schema.items)))
    );
  if (types.includes('object'))
    return (
      value &&
      typeof value === 'object' &&
      !Array.isArray(value) &&
      (schema.required || []).every((key) => Object.hasOwn(value, key)) &&
      Object.entries(value).every(([key, item]) =>
        schema.properties?.[key]
          ? validControlValue(item, schema.properties[key])
          : schema.additionalProperties !== false,
      )
    );
  if (types.includes('integer') || types.includes('number'))
    return (
      typeof value === 'number' &&
      Number.isFinite(value) &&
      (!types.includes('integer') || Number.isInteger(value)) &&
      (schema.minimum === undefined || value >= schema.minimum) &&
      (schema.maximum === undefined || value <= schema.maximum) &&
      (!schema.multipleOf ||
        Math.abs(value / schema.multipleOf - Math.round(value / schema.multipleOf)) <= 1e-7)
    );
  return types.includes(typeof value);
}
export function newExcerptControl(field, schema) {
  const properties = schema.items?.properties || {},
    duration = properties.duration_seconds?.default ?? properties.duration_seconds?.minimum ?? null;
  return {
    time_seconds:
      properties.time_seconds?.default ??
      (field === 'video_excerpts' ? null : Number.isFinite(duration) ? duration / 2 : null),
    duration_seconds: duration,
  };
}
export function safeSessionId(value) {
  return typeof value === 'string' && /^[A-Za-z0-9_-]{1,160}$/.test(value) ? value : null;
}
export function sessionHref(sessionId) {
  const id = safeSessionId(sessionId);
  return id ? `/quick-chat?session=${encodeURIComponent(id)}` : '/quick-chat';
}
export function failureText(error) {
  return error?.detail?.code === 'version_conflict'
    ? '内容已被其他窗口或 Agent 更新。请刷新后再编辑；当前输入仍保留。'
    : error?.message || '请求没有完成，请核对原记录后继续。';
}
export function bindingPayload(binding) {
  return Object.fromEntries(
    Object.entries(binding).filter(([key]) =>
      [
        'binding_id',
        'version',
        'asset_id',
        'kind',
        'slot',
        'purpose',
        'enabled',
        'source_range',
        'include_audio',
        'time_seconds',
        'use_audio',
      ].includes(key),
    ),
  );
}
// A preflight trim is an execution receipt, not a new user-selected material.
// Explicit bindings stay visible; ownership and the complete catalog remain
// authoritative regardless of this presentation-only inventory filter.
export function isInternalDerivedInventory(binding) {
  const asset = binding.asset;
  return (
    binding.version === 0 &&
    binding.enabled === false &&
    !!asset?.parent_id &&
    !!asset.selection &&
    !asset.client_asset_id
  );
}
export function bindingsToInputs(bindings, recipe) {
  const inputs = {
    first_frame: null,
    last_frame: null,
    images: [],
    videos: [],
    audios: [],
    guides: [],
  };
  for (const binding of bindings.filter((item) => item.enabled !== false)) {
    const { slot, asset_id } = binding;
    if (slot === 'guides') {
      inputs.guides.push({
        media_id: asset_id,
        time_seconds: binding.time_seconds ?? 0,
        use_audio:
          binding.kind === 'video' && binding.use_audio === true && hasOriginalAudio(binding.asset),
        ...(binding.source_range ? { source_range: clone(binding.source_range) } : {}),
      });
      continue;
    }
    if (!bindingParticipates(binding, recipe)) continue;
    const entry = {
      asset_id,
      ...(['first_frame', 'last_frame'].includes(slot)
        ? {}
        : {
            purpose:
              binding.purpose || { images: 'reference', videos: 'motion', audios: 'audio' }[slot],
          }),
      ...(binding.source_range ? { source_range: clone(binding.source_range) } : {}),
      ...(slot === 'videos'
        ? { include_audio: hasOriginalAudio(binding.asset) && binding.include_audio !== false }
        : {}),
    };
    if (['first_frame', 'last_frame'].includes(slot)) inputs[slot] = entry;
    else inputs[slot]?.push(entry);
  }
  return inputs;
}
export function bindingProblems(bindings, recipe, controls, options = {}) {
  const refs = bindings.map((binding) => ({
    asset_id: binding.asset_id,
    enabled: binding.enabled,
    role:
      binding.slot === 'guides'
        ? 'guide'
        : ['first_frame', 'last_frame'].includes(binding.slot)
          ? binding.slot
          : 'reference',
    ...(binding.source_range
      ? { start: binding.source_range.start, end: binding.source_range.end }
      : {}),
    use_audio:
      binding.kind === 'video' && !hasOriginalAudio(binding.asset)
        ? false
        : binding.slot === 'videos'
          ? binding.include_audio !== false
          : binding.use_audio,
    time_seconds: binding.time_seconds,
  }));
  return inputProblems(
    refs,
    bindings.map((binding) => ({ ...binding.asset, id: binding.asset_id, kind: binding.kind })),
    recipe,
    { controls, ...options },
  );
}
export function bindingParticipates(binding, recipe) {
  return (
    binding.enabled !== false &&
    roleSupported(recipe, binding.slot, binding.kind || binding.asset?.kind)
  );
}
export function automaticInputMode(bindings, recipe, referenceRecipe) {
  const active = bindings.filter((binding) => binding.enabled !== false);
  if (active.length && recipe?.limits?.input_count_semantics === 'native_roles_v2') {
    if (active.every((binding) => bindingParticipates(binding, recipe))) return recipe.mode;
    if (referenceRecipe && active.every((binding) => bindingParticipates(binding, referenceRecipe)))
      return referenceRecipe.mode;
  }
  return active.some((binding) => ['first_frame', 'last_frame'].includes(binding.slot))
    ? 'fl'
    : active.some((binding) => ['images', 'videos', 'audios'].includes(binding.slot))
      ? 'ref'
      : 'fl';
}
export function mergeTimelineTurns(events) {
  const turns = new Map();
  for (const event of [...events].sort((a, b) => a.seq - b.seq))
    if (
      event.record?.text !== undefined &&
      (event.type === 'turn.created' || event.type.startsWith('assistant.'))
    )
      turns.set(event.record.id, event.record);
  return [...turns.values()].sort((a, b) => a.seq - b.seq);
}
export function bindingsForInputs(inputs = {}, catalog = []) {
  const result = [];
  for (const [slot, value] of Object.entries(inputs)) {
    const entries = Array.isArray(value) ? value : value ? [value] : [];
    for (const entry of entries) {
      const assetId = entry.asset_id || entry.media_id,
        known = catalog.find((item) => item.asset_id === assetId);
      result.push({
        binding_id: known?.binding_id || `${slot}-${assetId}`,
        version: known?.version ?? 0,
        asset_id: assetId,
        kind:
          known?.kind ||
          {
            images: 'image',
            first_frame: 'image',
            last_frame: 'image',
            videos: 'video',
            audios: 'audio',
          }[slot],
        asset: known?.asset,
        slot,
        purpose:
          entry.purpose ||
          {
            first_frame: 'firstFrame',
            last_frame: 'lastFrame',
            images: 'reference',
            videos: 'motion',
            audios: 'audio',
            guides: 'reference',
          }[slot],
        enabled: true,
        ...(entry.source_range ? { source_range: clone(entry.source_range) } : {}),
        ...(slot === 'videos' ? { include_audio: entry.include_audio !== false } : {}),
        ...(slot === 'guides'
          ? { time_seconds: entry.time_seconds, use_audio: entry.use_audio === true }
          : {}),
      });
    }
  }
  return result;
}
