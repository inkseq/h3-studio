import { translate } from './i18n/locale.js';
import { controlsForRecipe, referenceSpecs } from './cloud-model.js';

const namesFor = (locale) => ({
  sampler_name: translate(locale, 'adjunct.recipe.sampler_name'),
  scheduler: translate(locale, 'adjunct.recipe.scheduler'),
  video_decode: translate(locale, 'adjunct.recipe.video_decode'),
  audio_decode: translate(locale, 'adjunct.recipe.audio_decode'),
  encoder_device: translate(locale, 'adjunct.recipe.encoder_device'),
  ref_image_size: translate(locale, 'adjunct.recipe.ref_image_size'),
});
const display = (value, locale) =>
  value === 'cpu'
    ? 'CPU'
    : value === 'tiled'
      ? translate(locale, 'adjunct.recipe.tiled')
      : String(value);
const mediaKey = (entity, range) => `${entity.id}:${range?.start ?? ''}:${range?.end ?? ''}`;

function describeInputLimits(
  limits,
  { recipe, project, shot, references, guides, issues, summary, locale },
) {
  const at = (key, values) => translate(locale, key, values);
  if (!limits) return;
  const labels = {
      image: at('adjunct.describeInputLimits.copy68'),
      video: at('adjunct.describeInputLimits.copy69'),
      audio: at('adjunct.describeInputLimits.copy70'),
    },
    kinds = { image: new Map(), video: new Map(), audio: new Map() },
    all = new Map();
  for (const ref of references) {
    const entry = { entity: ref.entity, range: ref.range },
      key = mediaKey(ref.entity, ref.range);
    all.set(key, entry);
    if (!['firstFrame', 'lastFrame'].includes(ref.purpose)) kinds[ref.entity.type]?.set(key, entry);
  }
  for (const guide of guides) {
    const entity = project?.entities.find((e) => e.id === guide.media_id);
    if (!entity) continue;
    const key = mediaKey(entity, guide.source_range),
      entry = { entity, range: guide.source_range };
    all.set(key, entry);
    kinds[entity.type]?.set(key, entry);
  }
  for (const [kind, entries] of Object.entries(kinds)) {
    const maxCount =
      limits[{ image: 'max_images', video: 'max_videos', audio: 'max_audios' }[kind]];
    if (Number.isInteger(maxCount)) {
      summary.push(at('adjunct.describeInputLimits.copy71', { v0: labels[kind], v1: maxCount }));
      if (entries.size > maxCount)
        issues.push(
          at('adjunct.describeInputLimits.copy72', {
            v0: entries.size,
            v1: labels[kind],
            v2: maxCount,
          }),
        );
    }
    const maxDuration = limits[`max_${kind}_duration_seconds`];
    if (Number.isFinite(maxDuration)) {
      summary.push(
        at('adjunct.describeInputLimits.copy73', {
          v0: labels[kind],
          v1: Number(maxDuration.toFixed(3)),
        }),
      );
      let total = 0;
      for (const { entity, range } of entries.values()) {
        const meta = entity.data.metadata || {},
          duration = range
            ? range.end - range.start
            : Math.max(meta.duration || 0, meta.source_duration || 0);
        if (Number.isFinite(duration)) total += duration;
      }
      if (total > maxDuration + 1e-6)
        issues.push(
          at('adjunct.describeInputLimits.copy74', {
            v0: labels[kind],
            v1: Number(maxDuration.toFixed(3)),
          }),
        );
    }
  }
  for (const kind of ['image', 'video']) {
    const maximum = limits[`max_${kind}_pixels`];
    if (Number.isFinite(maximum))
      summary.push(
        at('adjunct.describeInputLimits.copy75', {
          v0:
            kind === 'image'
              ? at('adjunct.describeInputLimits.copy76')
              : at('adjunct.describeInputLimits.copy69'),
          v1: maximum.toLocaleString(locale === 'en' ? 'en-US' : 'zh-CN'),
        }),
      );
  }
  for (const { entity } of all.values()) {
    const meta = entity.data.metadata || {},
      maximum = limits[`max_${entity.type}_pixels`];
    if (Number.isFinite(maximum) && Number(meta.width) * Number(meta.height) > maximum)
      issues.push(
        at('adjunct.describeInputLimits.copy78', {
          v0: entity.title,
          v1: meta.width,
          v2: meta.height,
        }),
      );
  }
  if (Array.isArray(limits.guide_recipe_ids) && !limits.guide_recipe_ids.includes(recipe.id)) {
    summary.push(at('adjunct.describeInputLimits.copy79'));
    if (guides.length) issues.push(at('adjunct.describeInputLimits.copy80'));
  }
  if (Array.isArray(limits.guide_kinds)) {
    summary.push(
      at('adjunct.describeInputLimits.copy81', {
        v0:
          limits.guide_kinds.map((kind) => labels[kind] || kind).join(' / ') ||
          at('adjunct.describeInputLimits.copy82'),
      }),
    );
    if (
      guides.some(
        (g) =>
          !limits.guide_kinds.includes(project?.entities.find((e) => e.id === g.media_id)?.type),
      )
    )
      issues.push(at('adjunct.describeInputLimits.copy83'));
  }
  if (Number.isFinite(limits.max_guide_time_seconds)) {
    summary.push(at('adjunct.describeInputLimits.copy84', { v0: limits.max_guide_time_seconds }));
    if (guides.some((g) => g.time_seconds > limits.max_guide_time_seconds))
      issues.push(at('adjunct.describeInputLimits.copy85', { v0: limits.max_guide_time_seconds }));
  }
  if (limits.allow_video_audio === false) {
    summary.push(at('adjunct.describeInputLimits.copy86'));
    const hasReference = references.some(
        (ref) =>
          ref.entity.type === 'video' &&
          ref.entity.data.metadata?.has_audio &&
          shot?.data.h3?.video_audio?.[ref.entity.id] !== false,
      ),
      hasGuide = guides.some(
        (g) => g.use_audio && project?.entities.find((e) => e.id === g.media_id)?.type === 'video',
      );
    if (hasReference || hasGuide) issues.push(at('adjunct.describeInputLimits.copy87'));
  }
  summary.push(at('adjunct.describeInputLimits.copy88'));
}

// Describes the server's qualified envelope, never grants admission or changes
// user inputs. The authenticated preflight remains authoritative for execution.
export function recipeExecutionStatus(
  recipe,
  { project, shot, now = Date.now(), locale = 'zh' } = {},
) {
  const at = (key, values) => translate(locale, key, values);
  const names = namesFor(locale),
    support = recipe?.execution_support;
  if (!support)
    return {
      kind: 'unknown',
      title: at('adjunct.recipeExecutionStatus.copy89'),
      detail: at('adjunct.recipeExecutionStatus.copy90'),
      issues: [],
      summary: [],
    };
  if (Number.isFinite(support.expires_at) && support.expires_at * 1000 <= now)
    return {
      kind: 'unavailable',
      title: at('adjunct.recipeExecutionStatus.copy91'),
      detail: at('adjunct.recipeExecutionStatus.copy92'),
      issues: [],
      summary: [],
    };
  if (!['qualified', 'runtime_required'].includes(support.status))
    return {
      kind: support.status,
      title:
        support.status === 'not_qualified'
          ? at('adjunct.recipeExecutionStatus.copy93')
          : support.status === 'simulation'
            ? at('adjunct.recipeExecutionStatus.copy94')
            : at('adjunct.recipeExecutionStatus.copy95'),
      detail: support.reason || at('adjunct.recipeExecutionStatus.copy96'),
      issues: [],
      summary: [],
    };
  const limits = support.constraints;
  if (!limits)
    return {
      kind: 'unknown',
      title: at('adjunct.recipeExecutionStatus.copy97'),
      detail: at('adjunct.recipeExecutionStatus.copy98'),
      issues: [],
      summary: [],
    };
  const values = controlsForRecipe(recipe, shot?.data.h3?.controls, shot?.data.seconds),
    issues = [],
    summary = [];
  if (Number.isFinite(limits.max_duration_seconds)) {
    summary.push(at('adjunct.recipeExecutionStatus.copy99', { v0: limits.max_duration_seconds }));
    if (Number(values.duration) > limits.max_duration_seconds)
      issues.push(
        at('adjunct.recipeExecutionStatus.copy100', {
          v0: values.duration,
          v1: limits.max_duration_seconds,
        }),
      );
  }
  if (Number.isFinite(limits.max_steps)) {
    summary.push(at('adjunct.recipeExecutionStatus.copy101', { v0: limits.max_steps }));
    if (Number(values.steps) > limits.max_steps)
      issues.push(
        at('adjunct.recipeExecutionStatus.copy102', { v0: values.steps, v1: limits.max_steps }),
      );
  }
  if (Number.isFinite(limits.max_pixels)) {
    summary.push(
      at('adjunct.recipeExecutionStatus.copy103', {
        v0: limits.max_pixels.toLocaleString(locale === 'en' ? 'en-US' : 'zh-CN'),
      }),
    );
    if (
      values.resolution === 'custom' &&
      Number(values.width) * Number(values.height) > limits.max_pixels
    )
      issues.push(
        at('adjunct.recipeExecutionStatus.copy104', { v0: values.width, v1: values.height }),
      );
  }
  const references =
      project && shot ? referenceSpecs(project, shot.id, { mode: recipe.mode }).references : [],
    guides = shot?.data.h3?.guides || [];
  const mediaKeys = new Set(references.map((ref) => mediaKey(ref.entity, ref.range)));
  for (const guide of guides)
    mediaKeys.add(
      `${guide.media_id}:${guide.source_range?.start ?? ''}:${guide.source_range?.end ?? ''}`,
    );
  if (Number.isFinite(limits.max_reference_files)) {
    summary.push(
      limits.max_reference_files === 0
        ? at('adjunct.recipeExecutionStatus.copy105')
        : at('adjunct.recipeExecutionStatus.copy106', { v0: limits.max_reference_files }),
    );
    if (mediaKeys.size > limits.max_reference_files)
      issues.push(at('adjunct.recipeExecutionStatus.copy107', { v0: limits.max_reference_files }));
  }
  if (Number.isFinite(limits.max_guides)) {
    summary.push(
      limits.max_guides === 0
        ? at('adjunct.recipeExecutionStatus.copy108')
        : at('adjunct.recipeExecutionStatus.copy109', { v0: limits.max_guides }),
    );
    if (guides.length > limits.max_guides)
      issues.push(
        at('adjunct.recipeExecutionStatus.copy110', { v0: guides.length, v1: limits.max_guides }),
      );
  }
  if (limits.allow_first_last === false) {
    summary.push(at('adjunct.recipeExecutionStatus.copy111'));
    if (references.some((ref) => ['firstFrame', 'lastFrame'].includes(ref.purpose)))
      issues.push(at('adjunct.recipeExecutionStatus.copy112'));
  }
  if (limits.allow_audio === false) {
    summary.push(at('adjunct.recipeExecutionStatus.copy113'));
    if (values.generate_audio) issues.push(at('adjunct.recipeExecutionStatus.copy114'));
  }
  describeInputLimits(limits.input_limits, {
    recipe,
    project,
    shot,
    references,
    guides,
    issues,
    summary,
    locale,
  });
  for (const [field, options] of Object.entries(limits.controls || {}))
    if (names[field] && Array.isArray(options) && options.length) {
      summary.push(
        `${names[field]}：${options.map((value) => display(value, locale)).join(' / ')}`,
      );
      if (Object.hasOwn(values, field) && !options.includes(values[field]))
        issues.push(
          at('adjunct.recipeExecutionStatus.copy115', {
            v0: names[field],
            v1: display(values[field], locale),
            v2: options.map((value) => display(value, locale)).join(' / '),
          }),
        );
    }
  const runtime = support.status === 'runtime_required';
  return {
    kind: issues.length ? 'outside' : runtime ? 'runtime_required' : 'qualified',
    title: issues.length
      ? at('adjunct.recipeExecutionStatus.copy116')
      : runtime
        ? at('adjunct.recipeExecutionStatus.copy117')
        : at('adjunct.recipeExecutionStatus.copy118'),
    detail: runtime
      ? at('adjunct.recipeExecutionStatus.copy119')
      : at('adjunct.recipeExecutionStatus.copy120'),
    issues,
    summary,
  };
}
