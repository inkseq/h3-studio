import { translate } from './i18n/locale.js';
import { roleSupported } from './quick-chat-model.js';
import { referenceSpecs } from './cloud-model.js';
export function executionStatus(capabilities, locale = 'zh') {
  const at = (key, values) => translate(locale, key, values);
  if (!capabilities)
    return {
      kind: 'unknown',
      title: at('adjunct.executionStatus.copy0'),
      detail: at('adjunct.executionStatus.copy1'),
    };
  if (!capabilities.execution_enabled)
    return {
      kind: 'offline',
      title: at('adjunct.executionStatus.copy2'),
      detail: at('adjunct.executionStatus.copy3'),
    };
  if (capabilities.simulation)
    return {
      kind: 'simulation',
      title: at('adjunct.executionStatus.copy4'),
      detail: at('adjunct.executionStatus.copy5'),
    };
  return {
    kind: 'enabled',
    title: at('adjunct.executionStatus.copy6'),
    detail: at('adjunct.executionStatus.copy7'),
  };
}
export function inputUsage(project, shotId, recipe, locale = 'zh') {
  const at = (key, values) => translate(locale, key, values);
  const shot = project.entities.find((e) => e.id === shotId),
    limits = recipe?.limits || {},
    spec = referenceSpecs(project, shotId, { mode: recipe?.mode, locale }),
    frames = spec.references.filter((ref) => ['firstFrame', 'lastFrame'].includes(ref.purpose)),
    items = new Map(),
    issues = [...spec.issues];
  for (const ref of spec.references.filter(
    (ref) => !['firstFrame', 'lastFrame'].includes(ref.purpose),
  )) {
    const key = [ref.entity.id, ref.range?.start ?? '', ref.range?.end ?? ''].join('|');
    if (!items.has(key)) {
      const meta = ref.entity.data.metadata || {},
        duration = ref.range
          ? ref.range.end - ref.range.start
          : (meta.source_duration ?? meta.duration ?? meta.duration_s);
      items.set(key, {
        id: ref.entity.id,
        title: ref.entity.title,
        kind: ref.entity.type,
        duration: Number.isFinite(duration) ? duration : null,
        purpose: ref.purpose,
      });
    }
  }
  const groups = Object.fromEntries(
    ['image', 'video', 'audio'].map((kind) => {
      const entries = [...items.values()].filter((item) => item.kind === kind),
        duration = entries.reduce((n, e) => n + (e.duration || 0), 0),
        unknown = kind !== 'image' ? entries.filter((e) => e.duration === null).length : 0;
      return [kind, { count: entries.length, duration, unknown, entries }];
    }),
  );
  for (const [kind, key] of [
    ['image', 'images'],
    ['video', 'videos'],
    ['audio', 'audios'],
  ]) {
    const group = groups[kind],
      name = {
        image: at('adjunct.inputUsage.copy8'),
        video: at('adjunct.inputUsage.copy9'),
        audio: at('adjunct.inputUsage.copy10'),
      }[kind];
    if (Number.isFinite(limits['max_' + key]) && group.count > limits['max_' + key])
      issues.push(
        at('adjunct.inputUsage.copy11', { v0: name, v1: group.count, v2: limits['max_' + key] }),
      );
    if (kind !== 'image') {
      for (const item of group.entries)
        if (
          item.duration !== null &&
          (item.duration < limits.min_clip_duration || item.duration > limits.max_clip_duration)
        )
          issues.push(
            at('adjunct.inputUsage.copy12', {
              v0: item.title,
              v1: Number(item.duration.toFixed(3)),
              v2: limits.min_clip_duration,
              v3: limits.max_clip_duration,
            }),
          );
      const max = limits['max_total_' + kind + '_duration'];
      if (Number.isFinite(max) && group.duration > max)
        issues.push(
          at('adjunct.inputUsage.copy13', {
            v0: name,
            v1: Number(group.duration.toFixed(3)),
            v2: max,
          }),
        );
    }
  }
  if (Number.isFinite(limits.max_total_files) && items.size > limits.max_total_files)
    issues.push(at('adjunct.inputUsage.copy14', { v0: items.size, v1: limits.max_total_files }));
  if (recipe?.mode === 'ref' && frames.length && !roleSupported(recipe, 'first_frame', 'image'))
    issues.push(at('adjunct.inputUsage.copy15'));
  if (
    recipe?.mode === 'fl' &&
    [...items.values()].some(
      (item) =>
        !roleSupported(
          recipe,
          { image: 'images', video: 'videos', audio: 'audios' }[item.kind],
          item.kind,
        ),
    )
  )
    issues.push(at('adjunct.inputUsage.copy16'));
  const guides = shot?.data.h3?.guides || [];
  if (Number.isFinite(limits.max_guides) && guides.length > limits.max_guides)
    issues.push(at('adjunct.inputUsage.copy17', { v0: limits.max_guides }));
  return {
    frames: frames.length,
    groups,
    total: items.size,
    guides: guides.length,
    issues: [...new Set(issues)],
    unknownDurations: groups.video.unknown + groups.audio.unknown,
  };
}
