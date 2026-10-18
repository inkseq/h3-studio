import test from 'node:test';
import assert from 'node:assert/strict';
import { recipeExecutionStatus } from './recipe-execution-model.js';

const recipe = {
  mode: 'fl',
  controls: {
    duration: { default: 5 },
    steps: { default: 50 },
    generate_audio: { default: true },
    encoder_device: { enum: ['default', 'cpu'], default: 'default' },
    video_decode: { enum: ['normal', 'tiled'], default: 'normal' },
  },
  execution_support: {
    status: 'qualified',
    capacity_checked: false,
    preflight_required: true,
    constraints: {
      max_pixels: 1344 * 768,
      max_duration_seconds: 6,
      max_steps: 50,
      max_reference_files: 0,
      max_guides: 0,
      allow_first_last: false,
      allow_audio: true,
      controls: { encoder_device: ['cpu'], video_decode: ['tiled'] },
    },
  },
  deployment_preset: {
    applies_to: 'unset_controls_only',
    controls: { encoder_device: 'cpu', video_decode: 'tiled' },
  },
};
const shot = { id: 'shot', type: 'shot', data: { seconds: 5 } },
  project = { entities: [shot], links: [] };
test('implemented model and enabled channel do not masquerade as qualified recipe or ready GPU', () => {
  assert.equal(recipeExecutionStatus({ implemented: true, enabled: true }).kind, 'unknown');
  const result = recipeExecutionStatus({
    ...recipe,
    mode: 'ref',
    execution_support: { status: 'not_qualified', reason: '当前云端仅开放纯文字' },
  });
  assert.match(result.title, /仅可编辑/);
  assert.match(result.detail, /纯文字/);
  assert.equal(result.summary.length, 0);
  const qualified = recipeExecutionStatus(recipe, { project, shot });
  assert.equal(qualified.kind, 'qualified');
  assert.match(qualified.detail, /预检.*实际计算容量/);
});
test('selected inputs and explicit controls get specific warnings without changing or deleting them', () => {
  const selected = {
      ...shot,
      data: {
        seconds: 12,
        h3: {
          controls: { encoder_device: 'default', video_decode: 'normal', steps: 60 },
          guides: [{ media_id: 'image', time_seconds: 1 }],
        },
      },
    },
    p = {
      entities: [
        selected,
        { id: 'image', type: 'image', title: '首帧', data: { fileId: 'asset' } },
      ],
      links: [{ id: 'frame', source: 'image', target: 'shot', role: 'firstFrame' }],
    },
    before = structuredClone(p),
    status = recipeExecutionStatus(recipe, { project: p, shot: selected });
  assert.equal(status.kind, 'outside');
  assert.match(status.issues.join(' '), /12 秒/);
  assert.match(status.issues.join(' '), /60 步/);
  assert.match(status.issues.join(' '), /首尾帧/);
  assert.match(status.issues.join(' '), /时间锚点/);
  assert.doesNotMatch(
    status.issues.join(' '),
    /编码器设备当前为 default|视频 VAE 解码当前为 normal/,
  );
  assert.deepEqual(p, before);
});
test('expired qualification is unavailable rather than a promise based on old capabilities', () => {
  const result = recipeExecutionStatus(
    { ...recipe, execution_support: { ...recipe.execution_support, expires_at: 100 } },
    { now: 100000 },
  );
  assert.equal(result.kind, 'unavailable');
  assert.match(result.title, /过期/);
});

test('runtime authorization describes pending GPU validation without pretending it is historical acceptance', () => {
  const r = {
      ...recipe,
      execution_support: {
        ...recipe.execution_support,
        status: 'runtime_required',
        runtime_verification_required: true,
      },
    },
    status = recipeExecutionStatus(r, { project, shot });
  assert.equal(status.kind, 'runtime_required');
  assert.match(status.title, /可提交.*GPU启动后先验证/);
  assert.match(status.detail, /验证通过后才执行原任务/);
  assert.doesNotMatch(status.title, /已验收|已验证/);
  const selected = { ...shot, data: { ...shot.data, seconds: 15 } },
    outside = recipeExecutionStatus(r, {
      project: { ...project, entities: [selected] },
      shot: selected,
    });
  assert.equal(outside.kind, 'outside');
  assert.match(outside.issues.join(' '), /15 秒/);
});

test('per-kind cloud scope explains oversized references and counts reused guides once while retaining inputs', () => {
  const r = {
      ...recipe,
      id: 'ref',
      mode: 'ref',
      execution_support: {
        ...recipe.execution_support,
        constraints: {
          ...recipe.execution_support.constraints,
          max_reference_files: 3,
          max_guides: 1,
          allow_first_last: true,
          input_limits: {
            max_images: 1,
            max_videos: 1,
            max_audios: 1,
            max_image_pixels: 512 * 512,
            max_video_pixels: 832 * 480,
            max_video_duration_seconds: 107 / 24,
            max_audio_duration_seconds: 4.45,
            guide_kinds: ['image'],
            guide_recipe_ids: ['ref'],
            max_guide_time_seconds: 5,
            allow_video_audio: false,
          },
        },
      },
    },
    selected = {
      ...shot,
      data: { ...shot.data, h3: { guides: [{ media_id: 'img', time_seconds: 1 }] } },
    },
    p = {
      entities: [
        selected,
        {
          id: 'img',
          type: 'image',
          title: '人物图',
          data: { fileId: 'img-file', metadata: { width: 512, height: 512 } },
        },
        {
          id: 'video',
          type: 'video',
          title: '动作',
          data: {
            fileId: 'video-file',
            metadata: { width: 1920, height: 1080, duration: 6, has_audio: true },
          },
        },
      ],
      links: [
        { source: 'img', target: 'shot', role: 'identity' },
        { source: 'video', target: 'shot', role: 'motion' },
      ],
    },
    before = structuredClone(p),
    result = recipeExecutionStatus(r, { project: p, shot: selected });
  assert.equal(result.kind, 'outside');
  assert.match(result.issues.join(' '), /总时长/);
  assert.match(result.issues.join(' '), /1920×1080/);
  assert.match(result.issues.join(' '), /视频原声/);
  assert.doesNotMatch(result.issues.join(' '), /2 份图片/);
  assert.match(result.summary.join(' '), /首尾帧另计/);
  assert.deepEqual(p, before);
  const fl = { ...r, id: 'fl', mode: 'fl' },
    twoFrames = {
      entities: [
        selected,
        {
          id: 'img',
          type: 'image',
          title: '首帧',
          data: { fileId: 'img-file', metadata: { width: 512, height: 512 } },
        },
        {
          id: 'last',
          type: 'image',
          title: '尾帧',
          data: { fileId: 'last-file', metadata: { width: 512, height: 512 } },
        },
      ],
      links: [
        { source: 'img', target: 'shot', role: 'firstFrame' },
        { source: 'last', target: 'shot', role: 'lastFrame' },
      ],
    };
  const warning = recipeExecutionStatus(fl, { project: twoFrames, shot: selected });
  assert.match(warning.issues.join(' '), /当前模式尚未开放时间锚点/);
  assert.doesNotMatch(warning.issues.join(' '), /2 份图片/);
});
