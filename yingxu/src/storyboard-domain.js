import { shotSegments } from './shot-segments.js';
import { translate } from './i18n/locale.js';
/* Layout A projection of the existing v4 document. No second project store. */
import { children } from './store.js';
import { effectiveLimits, effectiveControlSchema, roleSupported } from './quick-chat-model.js';
const uid = (kind) => kind + '-' + crypto.randomUUID();
const find = (p, id) => p.entities.find((e) => e.id === id);
const mode = (shot) => shot.data.storyboard?.mode || shot.data.h3?.inputMode || 'fl';
const list = (value) => (Array.isArray(value) ? value : []);
export const storyChapters = (project) =>
  children(project, null).filter((e) => e.type === 'chapter');
// Every shot of the story in reading order: chapter, then scene, then shot.
export const storyShots = (project) =>
  storyChapters(project).flatMap((chapter) => chapterShots(project, chapter.id));
export function chapterShots(project, chapterId) {
  return children(project, chapterId)
    .filter((e) => e.type === 'scene')
    .flatMap((scene) => children(project, scene.id).filter((e) => e.type === 'shot'));
}
export function storyboardProfile(capabilities, quality, inputMode, explicitId) {
  const profiles = capabilities?.deployment_profiles || [],
    enabled = (p) =>
      p.generation_support?.[inputMode]?.configured === true &&
      p.generation_support?.[inputMode]?.enabled === true;
  const id = explicitId || capabilities?.default_deployment_profile_id;
  const exact = profiles.find((p) => p.id === id);
  return exact && enabled(exact) ? exact : null;
}
export function storyboardGenerationProfile(capabilities, shot, { quality, sourceRequest } = {}) {
  const targetQuality = quality || shot.quality,
    inputMode = sourceRequest?.mode || shot.mode.toLowerCase();
  return storyboardProfile(
    capabilities,
    targetQuality,
    inputMode,
    sourceRequest?.deployment_profile_id || shot.entity?.data.h3?.deploymentProfileId,
  );
}
// Media the storyboard library shows, with the inbox flag for media no profile uses.
function storyboardAssets(project) {
  return project.entities
    .filter((e) => ['image', 'video', 'audio'].includes(e.type))
    .map((e) => ({
      id: e.id,
      kind: e.type,
      name: e.title,
      fileId: e.data.fileId,
      entity: e,
      seconds: e.data.metadata?.source_duration ?? e.data.metadata?.duration,
      inbox:
        e.data.storyboardInbox !== false &&
        !project.entities.some((profile) =>
          [
            ...list(profile.data.imageIds),
            ...list(profile.data.referenceAssetIds),
            ...list(profile.data.looks).flatMap((v) =>
              list(v.imageIds ?? Object.values(v.gallery || {})),
            ),
            ...list(profile.data.versions).flatMap((v) => list(v.imageIds)),
            ...list(profile.data.voices).map((v) => v.assetId),
            profile.data.ambient?.assetId,
          ].includes(e.id),
        ),
      source: e.data.source || 'upload',
    }));
}

function storyboardProfiles(project) {
  return project.entities
    .filter((e) => ['character', 'location', 'prop'].includes(e.type))
    .map((e) => {
      const variants =
        e.type === 'character'
          ? list(e.data.looks)
          : e.type === 'location'
            ? list(e.data.versions).length
              ? e.data.versions
              : [
                  {
                    id: 'legacy',
                    name: '原场景',
                    description: '',
                    imageIds: list(e.data.referenceAssetIds),
                  },
                ]
            : [{ id: 'main', name: '主图', imageIds: list(e.data.imageIds) }];
      return {
        id: e.id,
        type: e.type,
        name: e.title,
        desc: e.description,
        def: e.data.defaultLookId || e.data.defaultVersionId || variants[0]?.id || '',
        voice: list(e.data.voices).find((v) => v.id === e.data.mainVoiceId)?.assetId || '',
        voices: list(e.data.voices).map((v) => ({ asset: v.assetId, start: v.clipStart || 0 })),
        ambient: e.data.ambient?.assetId || '',
        ambientStart: e.data.ambient?.clipStart ?? 0,
        variants: variants.map((v) => ({
          id: v.id,
          name: v.name,
          desc: v.description || '',
          images: list(v.imageIds ?? Object.values(v.gallery || {})),
          voice: list(e.data.voices).find((vo) => vo.id === v.voiceId)?.assetId || '',
        })),
      };
    });
}

// Suggest the one profile whose name appears in an asset's file name.
function suggestProfiles(assets, profiles) {
  for (const a of assets) {
    const stem = (a.entity.data.fileName || a.name).replace(/\.[^.]+$/, '').toLocaleLowerCase();
    const matches = profiles.filter(
      (p) => p.name.length >= 2 && stem.includes(p.name.toLocaleLowerCase()),
    );
    if (matches.length === 1)
      a.suggest = { pid: matches[0].id, vid: matches[0].def, label: matches[0].name };
  }
}

// Every take of a shot: its jobs (local and server) in order, then imported candidates.
function shotVersions(project, s, jobs) {
  const shotJobs = [
    ...new Map(
      [...project.jobs, ...jobs]
        .filter((j) => j.client_ref?.shot_id === s.id)
        .map((j) => [j.id, j]),
    ).values(),
  ].sort((a, b) => Number(a.created_at) - Number(b.created_at));
  const versions = shotJobs.map((j, n) => {
    const artifact = j.artifacts?.find((a) => a.kind === 'video'),
      entity = project.entities.find(
        (e) => e.data.cloudArtifactId === artifact?.id && e.type === 'video',
      );
    return {
      id: entity?.id || j.id,
      n: n + 1,
      quality: j.effective_request?.model?.includes('Pruned') ? 'draft' : 'final',
      seed: j.effective_request?.seed,
      mode: j.effective_request?.mode === 'ref' ? 'REF' : 'FL',
      status: j.status === 'succeeded' ? 'done' : j.status,
      jobId: j.id,
      artifactId: artifact?.id,
      asset: entity ? { fileId: entity.data.fileId, entity, kind: 'video' } : null,
      duration: artifact?.metadata?.duration ?? artifact?.metadata?.duration_seconds,
      error: j.error_code || j.message || '',
      audio: j.artifacts?.find((a) => a.kind === 'audio'),
      effectiveRequest: j.effective_request,
    };
  });
  for (const id of list(s.data.candidateIds)) {
    if (versions.some((v) => v.id === id)) continue;
    const e = find(project, id);
    if (e?.type === 'video')
      versions.push({
        id: e.id,
        n: versions.length + 1,
        status: 'done',
        quality: 'draft',
        mode: mode(s) === 'ref' ? 'REF' : 'FL',
        artifactId: e.data.cloudArtifactId,
        jobId: e.data.sourceJobId,
        asset: { fileId: e.data.fileId, entity: e, kind: 'video' },
        duration: e.data.metadata?.duration,
      });
  }
  return versions;
}

// A storyboard field wins; a legacy shot falls back to its project link.
const shotFrame = (project, s, b, field, role) =>
  Object.hasOwn(b, field)
    ? b[field]
    : project.links.find((l) => l.target === s.id && l.role === role)?.source || '';

function shotRefs(project, s, b, assets) {
  if (b.extraRefs) return b.extraRefs.map((r) => r.entityId);
  return project.links
    .filter(
      (l) =>
        l.target === s.id &&
        ['reference', 'identity', 'motion', 'audio'].includes(l.role) &&
        assets.some((a) => a.id === l.source),
    )
    .map((l) => l.source);
}

function shotPlace(s, b, scene) {
  if (Object.hasOwn(b, 'place'))
    return b.place ? { id: b.place.locationId, variant: b.place.versionId } : null;
  if (s.data.locationId) return { id: s.data.locationId, variant: '' };
  return scene?.data.locationId ? { id: scene.data.locationId, variant: '' } : null;
}

function shotView(project, s, { jobs, assets }) {
  const b = s.data.storyboard || {},
    versions = shotVersions(project, s, jobs);
  const scene = find(project, s.parentId),
    cast = new Map(
      (s.data.storyboard && Object.hasOwn(s.data, 'cast')
        ? list(s.data.cast)
        : [...list(scene?.data.cast), ...list(s.data.cast)]
      ).map((c) => [c.characterId, { id: c.characterId, variant: c.lookId }]),
    );
  return {
    id: s.id,
    title: s.title,
    script: s.data.script || '',
    prompt: s.data.prompt ?? s.description,
    mode: mode(s) === 'ref' ? 'REF' : 'FL',
    duration: s.data.seconds ?? 5,
    start: shotFrame(project, s, b, 'firstFrameId', 'firstFrame'),
    end: shotFrame(project, s, b, 'lastFrameId', 'lastFrame'),
    refs: shotRefs(project, s, b, assets),
    cast: [...cast.values()],
    place: shotPlace(s, b, scene),
    props: b.propIds || [],
    continuePrev: !!b.continuePrevious,
    previousFrame: b.previousFrame?.frameAssetId,
    quality: b.quality ?? s.data.qualityIntent ?? 'draft',
    seedLock: !!s.data.h3?.controls?.seed,
    // Creator generation choices (owner spec 2026-10-26); the tier fixes everything else.
    aspect: s.data.h3?.controls?.aspect_ratio || '16:9',
    keepAudio: s.data.h3?.controls?.generate_audio !== false,
    versions,
    chosen: s.data.selectedAssetId || '',
    // Kept parts of the chosen take, in playback order (#512); empty = the whole take.
    segments: shotSegments(s.data.videoSegments),
    chapterId: find(project, s.parentId)?.parentId || null,
    entity: s,
  };
}

export function storyboardView(
  project,
  {
    chapterId,
    capabilities,
    jobs = [],
    queue = null,
    busy = false,
    error = '',
    allChapters = false,
  } = {},
) {
  const chapter =
    find(project, chapterId) || children(project, null).find((e) => e.type === 'chapter');
  const assets = storyboardAssets(project),
    profiles = storyboardProfiles(project);
  suggestProfiles(assets, profiles);
  const shots = (
    allChapters ? storyShots(project) : chapter ? chapterShots(project, chapter.id) : []
  ).map((s) => shotView(project, s, { jobs, assets }));
  return {
    project: {
      id: project.id,
      title: project.title,
      chapter: chapter?.title || '第一章',
      chapterId: chapter?.id || null,
      chapters: storyChapters(project).map((e) => ({ id: e.id, title: e.title })),
      style: project.journey?.storyboard?.style || '',
      aspect: project.journey?.brief?.aspect || '16:9',
    },
    shots,
    assets,
    profiles,
    queue,
    busy,
    error,
    capabilities,
    modelLabelKeys: Object.fromEntries(
      ['draft', 'final']
        .filter((q) => !storyboardProfile(capabilities, q, 'fl')?.label)
        .map((q) => [q, 'storyboard.modelUnloaded']),
    ),
    models: Object.fromEntries(
      ['draft', 'final'].map((q) => [
        q,
        storyboardProfile(capabilities, q, 'fl')?.label || '服务能力尚未读取',
      ]),
    ),
  };
}
// The recipe this shot will run with and what it accepts.
function shotSupport(shot, state, { effectiveProfile, effectiveRecipe, inputMode }) {
  const chosen =
      effectiveProfile ||
      (effectiveRecipe
        ? null
        : storyboardProfile(
            state.capabilities,
            shot.quality,
            inputMode,
            shot.entity?.data.h3?.deploymentProfileId,
          )),
    support = chosen?.generation_support?.[inputMode] || effectiveRecipe;
  const constraints =
    support?.execution_policy_constraints ||
    support?.constraints ||
    support?.execution_support?.constraints ||
    {};
  const advertised = support
    ? { ...support, mode: inputMode, execution_support: { ...support, constraints } }
    : null;
  return {
    chosen,
    support,
    advertised,
    schema: effectiveControlSchema(advertised),
    limits: support ? effectiveLimits(advertised) : null,
    nativeRoles: support?.limits?.input_count_semantics === 'native_roles_v2',
  };
}

// The text parts of the prompt: style, place, cast, props and action.
function describeShot(shot, state, ctx) {
  const { t, parts, warnings, profile, variant } = ctx;
  if (state.project.style.trim())
    parts.push({ label: '全片风格', text: state.project.style.trim() });
  const place = profile(shot.place?.id),
    pv = variant(place, shot.place?.variant);
  if (shot.place?.id && !place) warnings.push('保存的场景不存在，请重新选择。');
  if (place) {
    if (shot.place.variant && !pv) warnings.push('保存的场景版本不存在，请重新选择。');
    parts.push({
      label: '场景',
      text: `${place.name}：${place.desc}${pv ? '；' + pv.name + '：' + (pv.desc || '') : ''}`,
    });
  }
  const cast = shot.cast.map((c) => ({
    binding: c,
    p: profile(c.id),
    v: variant(profile(c.id), c.variant),
  }));
  cast.forEach(({ binding, p, v }) => {
    if (!p) {
      warnings.push('保存的人物不存在，请重新选择。');
      return;
    }
    if (binding.variant && !v) warnings.push(t('projection.missingLook', { name: p.name }));
    parts.push({
      label: '人物',
      text: `${p.name}：${p.desc}${v ? '；' + v.name + '：' + (v.desc || '') : ''}`,
    });
  });
  const props = shot.props.map(profile).filter(Boolean);
  props.forEach((p) => parts.push({ label: '道具', text: `${p.name}：${p.desc}` }));
  if (shot.prompt.trim()) parts.push({ label: '动作', text: shot.prompt.trim() });
  else warnings.push('还没写这一镜的动作描述。');
  return { place, pv, cast, props };
}

const INPUT_KINDS = { image: '图片', video: '视频', audio: '声音' };

// Why a reference cannot be sent as this input, or '' when it can.
function inputProblem(a, kind, forSync) {
  const expected = ['开头画面', '结尾画面', '图片'].includes(kind)
    ? 'image'
    : kind === '视频'
      ? 'video'
      : 'audio';
  if (!a) return '参考素材不存在，请重新选择';
  if (a.kind !== expected) return '素材类型不符合这个输入位置';
  if (!a.fileId || a.entity?.data.missingFile) return '素材文件缺失，请补传';
  if (!forSync && !a.entity?.data.cloudAssetId) return '素材尚未同步为可用的云端输入';
  return '';
}

function addInput(ctx, id, kind, why, start) {
  if (!id) return;
  const a = ctx.asset(id),
    reason = inputProblem(a, kind, ctx.forSync);
  ctx.items.push({
    a: a || { id, name: '已失效参考' },
    kind,
    why,
    sent: !reason,
    reason,
    clipStart: start,
  });
}

// First and last frames; "continue from previous" never crosses a chapter boundary
// (generation is per chapter).
function addFrames(shot, state, ctx) {
  if (shot.continuePrev) {
    const before = state.shots[state.shots.findIndex((s) => s.id === shot.id) - 1],
      previous = before?.chapterId === shot.chapterId ? before : undefined,
      selected = previous?.versions.find((v) => v.id === previous.chosen),
      frame = ctx.asset(shot.previousFrame),
      binding = shot.entity?.data.storyboard?.previousFrame,
      selection = frame?.entity?.data.sourceSelection;
    const currentFrame =
      frame &&
      selected &&
      binding?.shotId === previous.id &&
      binding.selectedAssetId === previous.chosen &&
      selection?.kind === 'artifact_frame' &&
      selection.position === 'last' &&
      selection.artifact_id === selected.artifactId;
    if (!selected) ctx.warnings.push('请先选定上一镜的成片版本。');
    if (currentFrame) addInput(ctx, frame.id, '开头画面', '接上一镜');
    else
      ctx.items.push({
        a: { name: '上一镜选定版本的最后一帧' },
        kind: '开头画面',
        why: '接上一镜',
        sent: false,
        reason: !selected ? '上一镜尚未选定版本' : '预检前从上一镜当前选定版本提取最后一帧',
      });
  } else addInput(ctx, shot.start, '开头画面', '首帧');
  addInput(ctx, shot.end, '结尾画面', '尾帧');
}

// Same ordering as the saved-draft compiler: each character's main image and voice, then
// place, props, authored extras, and ambient sound.
function addReferenceInputs(shot, ctx, { cast, place, pv, props }) {
  const { t, asset } = ctx;
  cast
    .filter((c) => c.p)
    .forEach(({ p, v }) => {
      addInput(ctx, v?.images[0], '图片', p.name + ' · ' + (v?.name || ''));
      const voice = p.voices.find((vo) => vo.asset === (v?.voice || p.voice));
      if (voice)
        addInput(ctx, voice.asset, '声音', t('projection.voice', { name: p.name }), voice.start);
    });
  if (place) addInput(ctx, pv?.images[0], '图片', '场景');
  props.forEach((p) =>
    addInput(ctx, p.variants[0]?.images[0], '图片', t('projection.prop', { name: p.name })),
  );
  shot.refs.forEach((id) => {
    const a = asset(id),
      range = shot.entity?.data.storyboard?.extraRefs?.find((ref) => ref.entityId === id);
    addInput(
      ctx,
      id,
      INPUT_KINDS[a?.kind] || '图片',
      '附加素材',
      a && a.kind !== 'image' ? (range?.clipStart ?? 0) : undefined,
    );
  });
  if (place?.ambient) addInput(ctx, place.ambient, '声音', '场景环境声', place.ambientStart ?? 0);
}

// Why one more reference of this kind would exceed the recipe's limits, or ''.
function referenceLimitReason(item, limits, used, t) {
  const max = limits?.[{ 图片: 'max_images', 视频: 'max_videos', 声音: 'max_audios' }[item.kind]],
    total = limits?.max_total_files;
  if (used.seen.has(used.identity)) return '同一输入区域的素材已发送，不重复发送';
  if (!Number.isInteger(max) || !Number.isInteger(total)) return '当前参考输入范围尚未读取';
  if (used.counts[item.kind] >= max || used.sent >= total)
    return t('projection.countLimit', {
      kind:
        {
          图片: t('legacy.projection6'),
          视频: t('legacy.projection7'),
          声音: t('legacy.projection8'),
        }[item.kind] || item.kind,
      max,
      total,
    });
  const maximumSeconds =
    limits?.[item.kind === '视频' ? 'max_total_video_duration' : 'max_total_audio_duration'];
  if (
    used.duration &&
    Number.isFinite(maximumSeconds) &&
    used.durations[item.kind] + used.duration > maximumSeconds + 0.01
  )
    return '当前能力总参考时长上限；按优先级未发送';
  return '';
}

// REF sends references in priority order until a count, total or duration limit is reached.
function applyReferenceLimits(items, limits, t) {
  const used = {
    seen: new Set(),
    counts: { 图片: 0, 视频: 0, 声音: 0 },
    durations: { 视频: 0, 声音: 0 },
    sent: 0,
  };
  for (const item of items) {
    used.identity = JSON.stringify([
      item.kind,
      item.a.entity?.data.cloudAssetId || item.a.id,
      item.clipStart ?? null,
    ]);
    used.duration = Object.hasOwn(used.durations, item.kind) ? 3 : 0;
    item.reason = referenceLimitReason(item, limits, used, t) || item.reason;
    item.sent = !item.reason;
    if (!item.sent) continue;
    used.seen.add(used.identity);
    used.counts[item.kind]++;
    used.sent++;
    if (used.duration) used.durations[item.kind] += used.duration;
  }
}

function addReferenceMode(shot, state, ctx, described, { advertised, limits }) {
  addReferenceInputs(shot, ctx, described);
  applyReferenceLimits(ctx.items, limits, ctx.t);
  if (
    roleSupported(advertised, 'first_frame', 'image') ||
    roleSupported(advertised, 'last_frame', 'image')
  )
    addFrames(shot, state, ctx);
  else if (shot.start || shot.end || shot.continuePrev)
    ctx.notes.push('首尾帧设置保留，此旧配方的全能参考中不发送。');
}

// Extra control media an FL recipe with native roles accepts; others are listed as not sent.
function addControlRefs(shot, ctx, advertised) {
  const { asset, items } = ctx;
  for (const id of shot.refs) {
    const a = asset(id),
      slot = { image: 'images', video: 'videos', audio: 'audios' }[a?.kind],
      range = shot.entity?.data.storyboard?.extraRefs?.find((ref) => ref.entityId === id);
    if (a && roleSupported(advertised, slot, a.kind)) {
      addInput(
        ctx,
        id,
        INPUT_KINDS[a.kind],
        '附加控制素材',
        a.kind !== 'image' ? (range?.clipStart ?? 0) : undefined,
      );
      if (
        a.kind === 'video' &&
        ['denoise', 'in_context'].includes(shot.entity?.data.h3?.controls?.video_conditioning)
      )
        items.at(-1).clipDuration = shot.entity?.data.h3?.controls?.duration || shot.duration;
    } else if (a)
      items.push({
        a,
        kind: INPUT_KINDS[a.kind],
        why: '附加素材',
        sent: false,
        reason: '当前FL配方不接收普通参考图片；需要时请设为时间锚点或改用REF。',
      });
  }
}

// FL: frames, then (native roles only) extra control media the recipe accepts.
function addFrameMode(shot, state, ctx, described, { advertised, nativeRoles }) {
  const { notes } = ctx;
  addFrames(shot, state, ctx);
  if (nativeRoles) addControlRefs(shot, ctx, advertised);
  else if (shot.refs.length) notes.push('附加参考保留，切回全能参考时使用。');
  if (described.cast.length || described.place || shot.props.length)
    notes.push('人物、场景和道具在首尾帧模式中使用文字描述。');
}

// Time anchors (guides), within the recipe's guide limit.
function addGuides(shot, ctx, limits) {
  const guides = shot.entity?.data.h3?.guides || [];
  for (const guide of guides) {
    const a = ctx.asset(guide.media_id),
      before = ctx.items.length;
    addInput(ctx, guide.media_id, INPUT_KINDS[a?.kind] || '图片', '时间锚点');
    if (ctx.items.length === before) continue;
    const item = ctx.items.at(-1);
    Object.assign(item, {
      slot: 'guides',
      time_seconds: guide.time_seconds,
      use_audio: a?.kind === 'video' && guide.use_audio === true,
      ...(guide.source_range ? { source_range: guide.source_range } : {}),
    });
    if (!limits || (Number.isInteger(limits.max_guides) && guides.length > limits.max_guides)) {
      item.sent = false;
      item.reason = '当前能力的时间锚点数量上限';
      ctx.warnings.push(item.reason);
    }
  }
}

function generationKind(shot) {
  if (shot.mode === 'REF') return '全能参考';
  if (shot.continuePrev) return '接上一镜';
  if (shot.start && shot.end) return '首尾帧';
  if (shot.start) return '首帧生成';
  if (shot.end) return '尾帧生成';
  return '文字生成';
}

function limitsLabel(limits, t) {
  if (!limits) return '能力尚未读取';
  const known = (value) => (Number.isFinite(value) ? value : '—');
  return t('projection.limits', {
    images: known(limits.max_images),
    videos: known(limits.max_videos),
    audios: known(limits.max_audios),
    total: known(limits.max_total_files),
  });
}

// Which input areas the editor shows for this recipe.
function inputFlags(support, inputMode) {
  const accepts = support?.input_support || {};
  return {
    frameInputs: inputMode === 'fl' || accepts.first_frame === true || accepts.last_frame === true,
    controlInputs:
      inputMode === 'fl' &&
      (accepts.video_conditioning === true || accepts.kept_soundtrack === true),
  };
}

const shotResolution = (shot, schema, t) =>
  shot.entity?.data.h3?.controls?.resolution ||
  schema.resolution?.default ||
  t('projection.unconfirmed');

const durationRange = (schema) => ({
  minimum: schema.duration?.minimum,
  maximum: schema.duration?.maximum,
  step: schema.duration?.multipleOf || 1,
});

export function expandStoryboard(
  shot,
  state,
  { forSync = false, locale = 'zh', effectiveProfile = null, effectiveRecipe = null } = {},
) {
  const t = (key, values) => translate(locale, key, values);
  const inputMode = shot.mode.toLowerCase();
  const ctx = {
    t,
    forSync,
    parts: [],
    items: [],
    notes: [],
    warnings: [],
    asset: (id) => state.assets.find((a) => a.id === id),
    profile: (id) => state.profiles.find((p) => p.id === id),
    // Explicit saved look/version IDs are exact. Missing IDs are not a request
    // to silently supply another look's image; only an unset ID uses defaults.
    variant: (p, id) =>
      id
        ? p?.variants.find((v) => v.id === id)
        : p?.variants.find((v) => v.id === p.def) || p?.variants[0],
  };
  const recipe = shotSupport(shot, state, { effectiveProfile, effectiveRecipe, inputMode });
  const { chosen, support, schema, limits, nativeRoles } = recipe;
  if (!support) ctx.warnings.push(t('projection.noCapabilities'));
  const described = describeShot(shot, state, ctx);
  if (shot.mode === 'REF') addReferenceMode(shot, state, ctx, described, recipe);
  else addFrameMode(shot, state, ctx, described, recipe);
  if (nativeRoles) addGuides(shot, ctx, limits);
  if (shot.mode === 'REF' && !ctx.items.some((i) => i.sent))
    ctx.warnings.push('全能参考至少需要一份可用参考。');
  return {
    mode: shot.mode,
    ...inputFlags(support, inputMode),
    kind: generationKind(shot),
    machine: chosen?.label || t('projection.noModel'),
    sec: shot.duration,
    res: shotResolution(shot, schema, t),
    parts: ctx.parts,
    items: ctx.items,
    warnings: ctx.warnings,
    notes: ctx.notes,
    durationRange: durationRange(schema),
    limitsLabel: limitsLabel(limits, t),
  };
}
// Dropping a profile: its main image as a frame, or the person, place or prop in the cast.
function dropProfile(shot, p, slot) {
  const v = p.variants.find((v) => v.id === p.def) || p.variants[0];
  if (['start', 'end'].includes(slot) && p.type !== 'character') {
    if (!v?.images[0]) return { patch: null, msg: '档案还没有主图。' };
    return {
      patch: { [slot]: v.images[0], ...(slot === 'start' ? { continuePrev: false } : {}) },
      msg: '已使用档案主图。',
    };
  }
  if (p.type === 'character')
    return shot.cast.some((c) => c.id === p.id)
      ? { patch: null, msg: '人物已经在这一镜中。' }
      : {
          patch: { cast: [...shot.cast, { id: p.id, variant: p.def }] },
          msg: '已加入出场人物。',
          hint: '松开：加入人物',
        };
  if (p.type === 'location')
    return {
      patch: { place: { id: p.id, variant: p.def } },
      msg: '已设置场景。',
      hint: '松开：设置场景',
    };
  return {
    patch: { props: [...new Set([...shot.props, p.id])] },
    msg: '已加入道具。',
    hint: '松开：加入道具',
  };
}

// Any other asset becomes an extra reference; the recipe decides whether it is sent.
function dropReference(shot, a, state) {
  const selected = storyboardProfile(
      state.capabilities,
      shot.quality,
      shot.mode.toLowerCase(),
      shot.entity?.data.h3?.deploymentProfileId,
    ),
    supported = roleSupported(
      {
        mode: shot.mode.toLowerCase(),
        input_support: selected?.generation_support?.[shot.mode.toLowerCase()]?.input_support,
      },
      { image: 'images', video: 'videos', audio: 'audios' }[a.kind],
      a.kind,
    );
  return {
    patch: { refs: [...new Set([...shot.refs, a.id])] },
    msg:
      shot.mode === 'FL' && !supported
        ? '参考已保存，切到全能参考时发送。'
        : '已加入附加参考，控制方式由预检核对。',
    hint: '松开：加入参考',
  };
}

export function storyboardDrop(shot, item, slot, state) {
  if (item.profile) return dropProfile(shot, item.profile, slot);
  const a = item.asset;
  if (!a) return { patch: null, msg: '素材不存在。' };
  if (['start', 'end'].includes(slot)) {
    if (a.kind !== 'image') return { patch: null, msg: '首尾帧需要图片。' };
    return {
      patch: { [slot]: a.id, ...(slot === 'start' ? { continuePrev: false } : {}) },
      msg: '已设置' + (slot === 'start' ? '首帧' : '尾帧') + '。',
    };
  }
  if (slot !== 'refs' && shot.mode === 'FL' && a.kind === 'image')
    return {
      patch: { [!shot.start && !shot.continuePrev ? 'start' : 'end']: a.id },
      msg: '已设置画面。',
      hint: '松开：设置画面',
    };
  return dropReference(shot, a, state);
}
// Aspect, "keep sound" and a fresh seed for "another version" (owner spec 2026-10-26).
// generate_audio defaults to on, so only "off" is stored.
const CREATOR_KEYS = ['aspect', 'keepAudio', 'newSeed'];
function patchCreatorControls(shot, patch) {
  if (!CREATOR_KEYS.some((key) => Object.hasOwn(patch, key))) return;
  const controls = { ...shot.data.h3?.controls };
  if (Object.hasOwn(patch, 'aspect')) controls.aspect_ratio = patch.aspect;
  if (Object.hasOwn(patch, 'keepAudio')) {
    if (patch.keepAudio) delete controls.generate_audio;
    else controls.generate_audio = false;
  }
  if (patch.newSeed && controls.seed)
    controls.seed = String(1 + Math.floor(Math.random() * 2147483646));
  shot.data.h3 = { ...shot.data.h3, controls };
}
// A legacy shot (no storyboard block) stays legacy unless the patch needs storyboard fields.
const STORYBOARD_ONLY = ['cast', 'place', 'props', 'continuePrev'];
const staysLegacy = (shot, patch) =>
  !shot.data.storyboard &&
  !Object.keys(patch).some((key) => STORYBOARD_ONLY.includes(key)) &&
  (!Object.hasOwn(patch, 'mode') || shot.data.h3?.explicitReferences);
const lastSeed = (project, id) =>
  project.jobs.findLast((j) => j.client_ref?.shot_id === id)?.effective_request?.seed;

function patchDuration(shot, patch) {
  if (!Object.hasOwn(patch, 'duration')) return;
  shot.data.seconds = patch.duration;
  shot.data.h3 = {
    ...shot.data.h3,
    controls: { ...shot.data.h3?.controls, duration: patch.duration },
  };
}

function setSeed(shot, seed) {
  shot.data.h3 = { ...shot.data.h3, controls: { ...shot.data.h3?.controls, seed } };
}

function setMode(shot, inputMode) {
  shot.data.h3 = {
    ...shot.data.h3,
    inputMode,
    recipeId: inputMode === 'ref' ? 'h3-base-ref2va-v1' : 'h3-base-fl2va-v1',
  };
}

// Legacy frames and references are project links.
function patchLegacyLinks(project, id, patch) {
  for (const [field, role] of [
    ['start', 'firstFrame'],
    ['end', 'lastFrame'],
  ])
    if (Object.hasOwn(patch, field)) {
      project.links = project.links.filter((l) => !(l.target === id && l.role === role));
      if (patch[field])
        project.links.push({ id: uid('link'), source: patch[field], target: id, role });
    }
  if (!Object.hasOwn(patch, 'refs')) return;
  const roles = ['reference', 'identity', 'motion', 'audio'],
    old = project.links.filter((l) => l.target === id && roles.includes(l.role));
  project.links = project.links.filter(
    (l) => !(l.target === id && roles.includes(l.role)) || patch.refs.includes(l.source),
  );
  for (const source of patch.refs)
    if (!old.some((l) => l.source === source))
      project.links.push({
        id: uid('link'),
        source,
        target: id,
        role: find(project, source)?.type === 'audio' ? 'audio' : 'reference',
      });
}

function patchLegacyShot(project, shot, id, patch) {
  for (const key of ['script', 'prompt'])
    if (Object.hasOwn(patch, key)) shot.data[key] = patch[key];
  if (Object.hasOwn(patch, 'title')) shot.title = patch.title;
  if (Object.hasOwn(patch, 'quality')) shot.data.qualityIntent = patch.quality;
  patchDuration(shot, patch);
  if (Object.hasOwn(patch, 'seedLock'))
    setSeed(
      shot,
      patch.seedLock ? (shot.data.h3?.controls?.seed ?? lastSeed(project, id) ?? '1') : null,
    );
  if (Object.hasOwn(patch, 'mode')) setMode(shot, patch.mode.toLowerCase());
  patchLegacyLinks(project, id, patch);
}

const STORYBOARD_FIELDS = {
  mode: 'mode',
  quality: 'quality',
  start: 'firstFrameId',
  end: 'lastFrameId',
  continuePrev: 'continuePrevious',
  props: 'propIds',
};

// The storyboard block, cast and extra references for a storyboard shot.
function storyboardBlock(shot, patch) {
  const b = {
    version: 1,
    mode: mode(shot),
    quality: 'draft',
    place: null,
    propIds: [],
    firstFrameId: '',
    lastFrameId: '',
    continuePrevious: false,
    extraRefs: [],
    ...shot.data.storyboard,
  };
  for (const [from, to] of Object.entries(STORYBOARD_FIELDS))
    if (Object.hasOwn(patch, from))
      b[to] = from === 'mode' ? patch[from].toLowerCase() : patch[from];
  if (Object.hasOwn(patch, 'cast'))
    shot.data.cast = patch.cast.map((c) => ({ characterId: c.id, lookId: c.variant || '' }));
  if (Object.hasOwn(patch, 'place'))
    b.place = patch.place
      ? { locationId: patch.place.id, versionId: patch.place.variant || '' }
      : null;
  if (Object.hasOwn(patch, 'refs'))
    b.extraRefs = patch.refs.map(
      (entityId) => b.extraRefs.find((r) => r.entityId === entityId) || { entityId },
    );
  return b;
}

function patchStoryboardBlockShot(project, shot, id, patch) {
  if (Object.hasOwn(patch, 'title')) shot.title = patch.title;
  if (Object.hasOwn(patch, 'script')) shot.data.script = patch.script;
  if (Object.hasOwn(patch, 'prompt')) shot.data.prompt = patch.prompt;
  patchDuration(shot, patch);
  const b = storyboardBlock(shot, patch);
  if (Object.hasOwn(patch, 'seedLock'))
    setSeed(shot, patch.seedLock ? lastSeed(project, id) || '1' : null);
  if (Object.hasOwn(patch, 'mode')) setMode(shot, b.mode);
  return b;
}

export function patchStoryboardShot(project, id, patch) {
  const shot = find(project, id);
  if (!shot) throw Error('镜头不存在。');
  if (staysLegacy(shot, patch)) patchLegacyShot(project, shot, id, patch);
  else shot.data.storyboard = patchStoryboardBlockShot(project, shot, id, patch);
  patchCreatorControls(shot, patch);
  shot.version++;
  if (shot.data.selectedAssetId) shot.status = 'review';
}
export function newStoryboardEntity(project, type, title, parentId = null, data = {}) {
  const entity = {
    id: uid(type),
    type,
    title,
    description: '',
    parentId,
    order: children(project, parentId).length,
    version: 1,
    status: 'draft',
    data,
  };
  project.entities.push(entity);
  return entity;
}
