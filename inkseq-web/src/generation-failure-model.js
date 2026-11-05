// A failed job's diagnostic rows, for admins (#1020). Kept apart from cloud-model.js so these
// words stay out of the first script.
import { translate } from './i18n/locale.js';
import { CONTROL_LABELS } from './quick-chat-model.js';

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
