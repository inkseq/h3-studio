import test from 'node:test';
import assert from 'node:assert/strict';
import {
  recipeFor,
  bindingParticipates,
  bindingsToInputs,
  bindingProblems,
  settingsProblems,
  validControlValue,
  automaticInputMode,
  newExcerptControl,
} from './quick-chat-model.js';
import { failureDetails, jobMessage } from './cloud-model.js';

const limits = {
  input_count_semantics: 'native_roles_v2',
  max_images: 9,
  max_videos: 3,
  max_audios: 3,
  max_total_files: 12,
  max_guides: 8,
  min_clip_duration: 2,
  max_clip_duration: 15,
  max_total_video_duration: 362 / 24,
  max_total_audio_duration: 15,
};
const support = {
  first_frame: true,
  last_frame: true,
  reference_images: true,
  reference_video: true,
  reference_audio: true,
  video_conditioning: true,
  kept_soundtrack: true,
};
const ref = { id: 'ref', mode: 'ref', limits, input_support: support };
const binding = (id, kind = 'image', slot = 'images', extra = {}) => ({
  binding_id: id,
  asset_id: id,
  kind,
  slot,
  enabled: true,
  asset: { id, kind, status: 'ready', metadata: { source_duration: 3, has_audio: false } },
  ...extra,
});
test('published profile roles preserve REF anchors and FL controls without widening legacy profiles', () => {
  const capabilities = {
      recipes: [ref],
      deployment_profiles: [
        { id: 'native-v2', generation_support: { ref: { limits, input_support: support } } },
      ],
    },
    recipe = recipeFor(capabilities, { recipe_id: 'ref', deployment_profile_id: 'native-v2' }),
    anchor = binding('first', 'image', 'first_frame');
  assert.equal(bindingParticipates(anchor, recipe), true);
  assert.equal(bindingsToInputs([anchor], recipe).first_frame.asset_id, 'first');
  assert.equal(bindingParticipates(anchor, { mode: 'ref' }), false);
  const fl = {
    mode: 'fl',
    input_support: {
      ...support,
      reference_images: false,
      reference_video: false,
      reference_audio: false,
    },
  };
  assert.equal(bindingParticipates(binding('control', 'video', 'videos'), fl), true);
  assert.equal(bindingParticipates(binding('track', 'audio', 'audios'), fl), true);
  assert.equal(bindingParticipates(binding('plain'), fl), false);
  assert.equal(automaticInputMode([anchor, binding('plain')], { ...fl, limits }, ref), 'ref');
  assert.equal(automaticInputMode([anchor], ref, ref), 'ref');
  assert.equal(automaticInputMode([], { ...ref }), 'fl');
});
test('ordinary REF image limits exclude FI and anchors; shared video soundtracks do not double native rows', () => {
  const images = Array.from({ length: 9 }, (_, i) => binding('image' + i)),
    guides = Array.from({ length: 8 }, (_, i) =>
      binding('guide' + i, 'image', 'guides', { time_seconds: (i + 1) / 4 }),
    ),
    anchors = [binding('first', 'image', 'first_frame'), binding('last', 'image', 'last_frame')];
  assert.deepEqual(bindingProblems([...images, ...guides, ...anchors], ref, { duration: 5 }), []);
  const videos = Array.from({ length: 3 }, (_, i) =>
    binding('video' + i, 'video', 'videos', {
      include_audio: true,
      asset: { status: 'ready', metadata: { duration: 3, source_duration: 3, has_audio: true } },
    }),
  );
  assert.deepEqual(bindingProblems([...images, ...videos], ref, { duration: 5 }), []);
  const audios = Array.from({ length: 3 }, (_, i) => binding('audio' + i, 'audio', 'audios'));
  assert.ok(
    bindingProblems(
      [...images, ...videos.map((v) => ({ ...v, include_audio: false })), ...audios],
      ref,
      { duration: 5 },
    ).some((text) => text.includes('原生参考合计')),
  );
});
test('native guide positions use aligned output duration while legacy guide duration remains unchanged', () => {
  const guide = binding('late', 'image', 'guides', { time_seconds: 5.1 });
  assert.deepEqual(bindingProblems([guide], ref, { duration: 5 }), []);
  assert.ok(
    bindingProblems(
      [guide],
      { ...ref, limits: { ...limits, input_count_semantics: undefined } },
      { duration: 5 },
    ).length,
  );
  assert.ok(
    bindingProblems([binding('late-video', 'video', 'guides', { time_seconds: 1 })], ref, {
      duration: 5,
    }).some((text) => text.includes('第 0 秒')),
  );
});
test('typed excerpt controls reject strings, malformed rows and overflow without a raw JSON editor', () => {
  const excerpt = {
    type: 'array',
    default: [],
    maxItems: 3,
    items: {
      type: 'object',
      additionalProperties: false,
      required: ['time_seconds', 'duration_seconds'],
      properties: {
        time_seconds: { type: 'number', minimum: 0 },
        duration_seconds: { type: 'number', minimum: 2, maximum: 15 },
      },
    },
  };
  assert.equal(validControlValue([{ time_seconds: 5, duration_seconds: 3 }], excerpt), true);
  for (const value of [
    '[]',
    [{ time_seconds: '5', duration_seconds: 3 }],
    [{ time_seconds: 5, duration_seconds: 3, hidden: true }],
    Array(4).fill({ time_seconds: 5, duration_seconds: 3 }),
  ])
    assert.equal(validControlValue(value, excerpt), false);
  const recipe = {
    controls: { video_excerpts: excerpt, size: { type: 'integer', enum: [50, 100, 400] } },
  };
  assert.deepEqual(
    settingsProblems(
      {
        copies: 1,
        controls: { video_excerpts: [{ time_seconds: 5, duration_seconds: 3 }], size: 400 },
      },
      recipe,
    ),
    [],
  );
  assert.ok(settingsProblems({ copies: 1, controls: { size: '400' } }, recipe).length);
  assert.deepEqual(newExcerptControl('video_excerpts', excerpt), {
    time_seconds: null,
    duration_seconds: 2,
  });
  assert.equal(validControlValue([newExcerptControl('video_excerpts', excerpt)], excerpt), false);
  assert.deepEqual(newExcerptControl('audio_excerpts', excerpt), {
    time_seconds: 1,
    duration_seconds: 2,
  });
});
test('only video guides select an original audio track; independent audio guides remain the audio source', () => {
  const guides = [
    binding('audio', 'audio', 'guides', { use_audio: true, time_seconds: 0 }),
    binding('video', 'video', 'guides', {
      use_audio: true,
      time_seconds: 0,
      asset: { metadata: { has_audio: true } },
    }),
  ];
  assert.deepEqual(
    bindingsToInputs(guides, ref).guides.map((guide) => guide.use_audio),
    [false, true],
  );
});
test('failure view prefers actionable diagnostics and renders only named safe summary fields', () => {
  const job = {
    id: 'job-1',
    message: 'generic',
    failure_diagnostic: {
      version: 1,
      stage: 'generation',
      reason_code: 'wangp_model_failed',
      message: '请核对原任务后手动重试',
      profile_id: 'profile-v2',
      mode: 'ref',
      controls: { steps: 100, prompt: 'private prompt', seed: '42' },
      inputs: {
        images: { count: 9 },
        videos: { count: 1, durations_seconds: [3] },
        first_frame: true,
      },
      host: {
        machine_id: '123',
        access_token: 'secret',
        provider_instance_id: 'https://secret/path',
      },
      prompt: 'private',
    },
  };
  assert.equal(jobMessage(job), '请核对原任务后手动重试');
  const result = failureDetails(job),
    text = JSON.stringify(result);
  assert.equal(result.stage, '模型生成');
  assert.ok(text.includes('100'));
  assert.ok(text.includes('123'));
  for (const secret of ['private prompt', 'access_token', 'secret/path'])
    assert.ok(!text.includes(secret));
});
