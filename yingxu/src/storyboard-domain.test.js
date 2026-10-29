import test from 'node:test';
import assert from 'node:assert/strict';
import {
  chapterShots,
  storyboardView,
  expandStoryboard,
  storyboardDrop,
  storyboardProfile,
  storyboardGenerationProfile,
  patchStoryboardShot,
} from './storyboard-domain.js';
import { shotSnapshot, digest, mergeJobCandidate } from './cloud-model.js';
import { applyStoryboardAction } from './storyboard-actions.js';

const entity = (id, type, data = {}, parentId = null, order = 0) => ({
  id,
  type,
  title: id,
  description: id + ' description',
  parentId,
  order,
  version: 1,
  status: 'draft',
  data,
});
const image = (id) => entity(id, 'image', { fileId: 'cloud_asset_' + id, cloudAssetId: id });
const audio = (id) =>
  entity(id, 'audio', { fileId: 'cloud_asset_' + id, cloudAssetId: id, metadata: { duration: 8 } });
const video = (id) =>
  entity(id, 'video', {
    fileId: 'cloud_artifact_' + id,
    cloudArtifactId: id,
    sourceJobId: 'job-' + id,
    metadata: { duration: 5 },
  });
function fixture() {
  return {
    schemaVersion: 4,
    id: 'project',
    title: 'Story',
    logline: '',
    journey: { storyboard: { style: 'Ink and rain' } },
    entities: [
      entity('chapter', 'chapter'),
      entity('scene', 'scene', {}, 'chapter'),
      entity(
        'shot',
        'shot',
        {
          seconds: 5,
          prompt: 'Run across the roof',
          storyboard: {
            version: 1,
            mode: 'ref',
            quality: 'draft',
            place: null,
            propIds: [],
            firstFrameId: '',
            lastFrameId: '',
            continuePrevious: false,
            extraRefs: [],
          },
        },
        'scene',
      ),
    ],
    links: [],
    jobs: [],
  };
}
function capabilities({ images = 5, videos = 2, audios = 3, total = 8, constraints = {} } = {}) {
  const support = {
    enabled: true,
    configured: true,
    controls: {
      duration: { type: 'integer', minimum: 4, maximum: 15, multipleOf: 1 },
      resolution: { default: '480P' },
      steps: { maximum: 50 },
    },
    limits: {
      max_images: images,
      max_videos: videos,
      max_audios: audios,
      max_total_files: total,
      max_total_audio_duration: 12,
      max_total_video_duration: 12,
    },
    constraints,
  };
  return {
    default_deployment_profile_id: 'draft',
    deployment_profiles: [
      {
        id: 'draft',
        label: 'Pruned live profile',
        model_id: 'MiniMax-H3-Pruned-Rank8-INT8',
        generation_support: { fl: structuredClone(support), ref: structuredClone(support) },
      },
      {
        id: 'final',
        label: 'Base live profile',
        model_id: 'MiniMax-H3-Base-INT8',
        generation_support: { fl: structuredClone(support), ref: structuredClone(support) },
      },
    ],
  };
}
const view = (project, cap = capabilities()) =>
  storyboardView(project, { chapterId: 'chapter', capabilities: cap });
const expanded = (project, cap) => {
  const state = view(project, cap);
  return expandStoryboard(state.shots[0], state);
};
const shotOf = (project) => project.entities.find((e) => e.id === 'shot');
const character = (id, photo, voice) =>
  entity(id, 'character', {
    defaultLookId: 'look',
    looks: [
      { id: 'look', name: 'Rain coat', description: 'blue coat', gallery: {}, imageIds: [photo] },
    ],
    ...(voice
      ? { mainVoiceId: 'voice', voices: [{ id: 'voice', assetId: voice, clipStart: 1 }] }
      : {}),
  });

test('chapter card order follows scene and shot order without mutating the document', () => {
  const p = fixture();
  p.entities.push(
    entity('later-scene', 'scene', {}, 'chapter', 1),
    entity('later-shot', 'shot', { seconds: 5 }, 'later-scene'),
    entity('second-shot', 'shot', { seconds: 5 }, 'scene', 1),
  );
  const before = structuredClone(p);
  assert.deepEqual(
    chapterShots(p, 'chapter').map((s) => s.id),
    ['shot', 'second-shot', 'later-shot'],
  );
  assert.deepEqual(p, before);
});
test('explicit BF16 identity wins across quality labels and unavailable pins never fall back', () => {
  const cap = capabilities(),
    bf = structuredClone(cap.deployment_profiles[1]);
  bf.id = 'exact-bf16';
  bf.model_id = 'MiniMax-H3-Base-BF16';
  cap.deployment_profiles.push(bf);
  for (const quality of ['draft', 'final'])
    assert.equal(storyboardProfile(cap, quality, 'ref', bf.id), bf);
  assert.equal(storyboardProfile(cap, 'draft', 'ref', 'missing'), null);
  bf.generation_support.ref.enabled = false;
  assert.equal(storyboardProfile(cap, 'final', 'ref', bf.id), null);
  assert.equal(storyboardProfile(cap, 'final', 'fl').id, 'draft');
  delete cap.default_deployment_profile_id;
  assert.equal(storyboardProfile(cap, 'draft', 'fl'), null);
  cap.default_deployment_profile_id = bf.id;
  assert.equal(storyboardProfile(cap, 'draft', 'ref'), null);
  assert.equal(storyboardProfile(cap, 'draft', 'fl'), bf);
});

test('quality label changes preserve exact profile and accepted recipe history', () => {
  const p = fixture(),
    cap = capabilities();
  shotOf(p).data.h3 = { deploymentProfileId: 'draft', controls: { steps: 50 } };
  const request = { deployment_profile_id: 'draft', mode: 'ref', prompt: 'original', seed: '123' };
  p.jobs = [{ effective_request: request }];
  const before = structuredClone(p);
  assert.equal(
    storyboardGenerationProfile(cap, view(p, cap).shots[0], {
      quality: 'final',
      sourceRequest: request,
    }).id,
    'draft',
  );
  assert.deepEqual(p, before);
});

test('generation preserves a qualified exact profile even when another profile becomes default', () => {
  const p = fixture(),
    cap = capabilities(),
    old = structuredClone(cap.deployment_profiles[0]);
  old.id = 'qualified-v3';
  cap.deployment_profiles.push(old);
  shotOf(p).data.h3 = { deploymentProfileId: old.id };
  const shot = view(p, cap).shots[0];
  assert.equal(storyboardGenerationProfile(cap, shot).id, old.id);
  // People see the tier, never the internal profile label (trade secret, owner 2026-10-29).
  const machine = expandStoryboard(shot, view(p, cap)).machine;
  assert.notEqual(machine, old.label);
  assert.doesNotMatch(machine, /Pruned|INT8|RTX|steps/i);
  shot.entity.data.h3.deploymentProfileId = '';
  assert.equal(storyboardGenerationProfile(cap, shot).id, 'draft');
  shot.entity.data.h3.deploymentProfileId = 'retired';
  assert.equal(storyboardGenerationProfile(cap, shot), null);
  shot.entity.data.h3.deploymentProfileId = old.id;
  old.generation_support.ref.enabled = false;
  assert.equal(storyboardGenerationProfile(cap, shot), null);
  assert.equal(storyboardGenerationProfile(cap, shot, { quality: 'final' }), null);
});

test('intentional mode/quality edits retain explicit profile identity without changing accepted history or controls', () => {
  const p = fixture(),
    shot = shotOf(p),
    cap = capabilities();
  cap.deployment_profiles[0].generation_support.fl.enabled = false;
  shot.data.h3 = { deploymentProfileId: 'draft', controls: { steps: 50, resolution: '480P' } };
  p.jobs = [{ id: 'accepted', effective_request: { deployment_profile_id: 'draft', mode: 'ref' } }];
  const jobs = structuredClone(p.jobs);
  patchStoryboardShot(p, 'shot', { mode: 'REF' });
  assert.equal(shot.data.h3.deploymentProfileId, 'draft');
  const incompatible = { ...view(p, cap).shots[0], mode: 'FL' };
  assert.equal(storyboardGenerationProfile(cap, incompatible), null);
  patchStoryboardShot(p, 'shot', { mode: 'FL' });
  assert.equal(shot.data.h3.deploymentProfileId, 'draft');
  shot.data.h3.deploymentProfileId = 'draft';
  patchStoryboardShot(p, 'shot', { quality: 'final' });
  assert.equal(shot.data.h3.deploymentProfileId, 'draft');
  assert.equal(storyboardGenerationProfile(cap, view(p, cap).shots[0]), null);
  assert.deepEqual(shot.data.h3.controls, { steps: 50, resolution: '480P' });
  assert.deepEqual(p.jobs, jobs);
});
test('mode changes preserve both inactive frame choices and reference clip metadata', () => {
  const p = fixture();
  p.entities.push(image('first'), image('last'), audio('voice'));
  const s = shotOf(p);
  s.data.storyboard = {
    ...s.data.storyboard,
    firstFrameId: 'first',
    lastFrameId: 'last',
    extraRefs: [{ entityId: 'voice', clipStart: 2 }],
  };
  patchStoryboardShot(p, 'shot', { mode: 'FL' });
  const fl = expanded(p);
  assert.deepEqual(
    fl.items.filter((i) => i.sent).map((i) => i.a.id),
    ['first', 'last'],
  );
  assert.match(fl.notes.join(' '), /参考保留/);
  patchStoryboardShot(p, 'shot', { mode: 'REF' });
  assert.deepEqual(s.data.storyboard.extraRefs, [{ entityId: 'voice', clipStart: 2 }]);
  assert.equal(s.data.storyboard.firstFrameId, 'first');
  assert.equal(s.data.storyboard.lastFrameId, 'last');
  const ref = expanded(p);
  assert.equal(
    ref.items.some((i) => ['first', 'last'].includes(i.a.id) && i.sent),
    false,
  );
  assert.equal(ref.items.find((i) => i.a.id === 'voice').clipStart, 2);
});
test('full-control REF previews first/last anchors and FI separately from ordinary reference limits', () => {
  const p = fixture(),
    cap = capabilities({ images: 1, total: 1 });
  p.entities.push(image('first'), image('last'), image('reference'), image('injection'));
  shotOf(p).data.storyboard = {
    ...shotOf(p).data.storyboard,
    firstFrameId: 'first',
    lastFrameId: 'last',
    extraRefs: [{ entityId: 'reference' }],
  };
  shotOf(p).data.h3 = {
    deploymentProfileId: 'draft',
    guides: [{ media_id: 'injection', time_seconds: 2, use_audio: false }],
  };
  const support = cap.deployment_profiles[0].generation_support.ref;
  support.input_support = {
    first_frame: true,
    last_frame: true,
    reference_images: true,
    reference_video: true,
    reference_audio: true,
  };
  support.limits = { ...support.limits, input_count_semantics: 'native_roles_v2', max_guides: 8 };
  const info = expanded(p, cap);
  assert.equal(info.frameInputs, true);
  assert.deepEqual(
    info.items.filter((item) => item.sent).map((item) => item.a.id),
    ['reference', 'first', 'last', 'injection'],
  );
  assert.equal(info.items.find((item) => item.a.id === 'injection').slot, 'guides');
  assert.equal(
    info.notes.some((note) => note.includes('不发送')),
    false,
  );
  assert.equal(shotOf(p).data.storyboard.firstFrameId, 'first');
  assert.equal(shotOf(p).data.h3.guides[0].media_id, 'injection');
});
test('full-control FL sync includes explicit video/soundtrack and image guides without promoting ordinary images', () => {
  const p = fixture(),
    cap = capabilities();
  p.entities.push(
    image('first'),
    image('unused'),
    image('injection'),
    audio('soundtrack'),
    entity('control', 'video', { fileId: 'local-control' }),
  );
  shotOf(p).data.storyboard = {
    ...shotOf(p).data.storyboard,
    mode: 'fl',
    firstFrameId: 'first',
    extraRefs: [
      { entityId: 'control', clipStart: 1 },
      { entityId: 'soundtrack', clipStart: 2 },
      { entityId: 'unused' },
    ],
  };
  shotOf(p).data.h3 = {
    deploymentProfileId: 'draft',
    guides: [{ media_id: 'injection', time_seconds: 2, use_audio: false }],
  };
  const support = cap.deployment_profiles[0].generation_support.fl;
  support.input_support = {
    first_frame: true,
    last_frame: true,
    reference_images: false,
    reference_video: false,
    reference_audio: false,
    video_conditioning: true,
    kept_soundtrack: true,
  };
  support.limits = { ...support.limits, input_count_semantics: 'native_roles_v2', max_guides: 8 };
  const state = view(p, cap),
    info = expandStoryboard(state.shots[0], state, { forSync: true });
  assert.equal(info.controlInputs, true);
  assert.deepEqual(
    info.items.filter((item) => item.sent).map((item) => item.a.id),
    ['first', 'control', 'soundtrack', 'injection'],
  );
  assert.equal(info.items.find((item) => item.a.id === 'control').clipStart, 1);
  assert.equal(info.items.find((item) => item.a.id === 'unused').sent, false);
  assert.match(info.items.find((item) => item.a.id === 'unused').reason, /普通参考图片/);
});
test('explicitly cleared storyboard frames do not revive old guide/canvas links', () => {
  const p = fixture();
  p.entities.push(image('old-first'), image('old-last'));
  p.links.push(
    { id: 'first-link', source: 'old-first', target: 'shot', role: 'firstFrame' },
    { id: 'last-link', source: 'old-last', target: 'shot', role: 'lastFrame' },
  );
  const s = view(p).shots[0];
  assert.equal(s.start, '');
  assert.equal(s.end, '');
});
test('explicitly removed scene and inherited cast stay removed in the view', () => {
  const p = fixture();
  p.entities.push(
    character('hero', 'portrait'),
    image('portrait'),
    entity('place', 'location', { referenceAssetIds: [] }),
  );
  p.entities.find((e) => e.id === 'scene').data = {
    locationId: 'place',
    cast: [{ characterId: 'hero', lookId: 'look' }],
  };
  shotOf(p).data.cast = [];
  const s = view(p).shots[0];
  assert.equal(s.place, null);
  assert.deepEqual(s.cast, []);
});
test('legacy frame inputs survive first storyboard edit of unrelated prompt', () => {
  const p = fixture();
  delete shotOf(p).data.storyboard;
  p.entities.push(image('first'));
  p.links.push({ id: 'frame-link', source: 'first', target: 'shot', role: 'firstFrame' });
  assert.ok(
    applyStoryboardAction(p, { type: 'update', id: 'shot', patch: { prompt: 'A new action' } }),
  );
  assert.equal(view(p).shots[0].start, 'first');
  assert.equal(shotOf(p).data.storyboard, undefined);
  assert.equal(p.links.find((l) => l.id === 'frame-link').source, 'first');
});
test('REF duplicate references do not consume unique file or total capacity', () => {
  const p = fixture();
  p.entities.push(
    image('shared'),
    image('extra'),
    character('a', 'shared'),
    character('b', 'shared'),
  );
  shotOf(p).data.cast = [
    { characterId: 'a', lookId: 'look' },
    { characterId: 'b', lookId: 'look' },
  ];
  shotOf(p).data.storyboard.extraRefs = [{ entityId: 'extra' }];
  const info = expanded(p, capabilities({ images: 2, total: 2 }));
  assert.deepEqual(
    info.items.filter((i) => i.sent).map((i) => i.a.id),
    ['shared', 'extra'],
  );
  assert.equal(info.items.find((i, n) => n > 0 && i.a.id === 'shared').sent, false);
});
test('REF clipping never counts future items and preserves the first supported unique references', () => {
  const p = fixture();
  p.entities.push(
    image('shared'),
    image('second'),
    image('third'),
    character('a', 'shared'),
    character('b', 'shared'),
  );
  shotOf(p).data.cast = [
    { characterId: 'a', lookId: 'look' },
    { characterId: 'b', lookId: 'look' },
  ];
  shotOf(p).data.storyboard.extraRefs = [{ entityId: 'second' }, { entityId: 'third' }];
  const info = expanded(p, capabilities({ images: 8, total: 2 }));
  assert.deepEqual(
    info.items.filter((i) => i.sent).map((i) => i.a.id),
    ['shared', 'second'],
  );
  assert.ok(info.items.find((i) => i.a.id === 'third').reason);
});
test('REF priority matches the authoritative per-character photo and voice order', () => {
  const p = fixture();
  p.entities.push(
    image('photo-a'),
    image('photo-b'),
    audio('voice-a'),
    character('a', 'photo-a', 'voice-a'),
    character('b', 'photo-b'),
  );
  shotOf(p).data.cast = [
    { characterId: 'a', lookId: 'look' },
    { characterId: 'b', lookId: 'look' },
  ];
  assert.deepEqual(
    expanded(p, capabilities({ total: 2 }))
      .items.filter((i) => i.sent)
      .map((i) => i.a.id),
    ['photo-a', 'voice-a'],
  );
});
test('REF capability policy clamps advertised reference limits instead of legacy or history values', () => {
  const p = fixture();
  p.entities.push(image('a'), image('b'));
  shotOf(p).data.storyboard.extraRefs = [{ entityId: 'a' }, { entityId: 'b' }];
  const cap = capabilities({
    images: 9,
    total: 12,
    constraints: { input_limits: { max_images: 1 }, max_reference_files: 1 },
  });
  const info = expanded(p, cap);
  assert.deepEqual(
    info.items.filter((i) => i.sent).map((i) => i.a.id),
    ['a'],
  );
  assert.match(info.limitsLabel, /图片 1/);
});
test('unknown capabilities retain REF authoring references but never pretend they will be sent', () => {
  const p = fixture();
  p.entities.push(image('a'));
  shotOf(p).data.storyboard.extraRefs = [{ entityId: 'a' }];
  const info = expanded(p, null);
  assert.equal(info.items.length, 1);
  assert.equal(info.items[0].sent, false);
  assert.ok(info.items[0].reason);
});
test('a stale explicitly selected look never silently supplies another looks photo', () => {
  const p = fixture();
  p.entities.push(character('hero', 'photo'), image('photo'));
  shotOf(p).data.cast = [{ characterId: 'hero', lookId: 'deleted-look' }];
  const info = expanded(p);
  assert.equal(
    info.items.some((i) => i.a.id === 'photo' && i.sent),
    false,
  );
  assert.ok(info.warnings.length);
});
test('two distinct voice selections from the same file keep their own clip starts', () => {
  const p = fixture();
  p.entities.push(character('hero', 'photo', 'voice'), image('photo'), audio('voice'));
  shotOf(p).data.cast = [{ characterId: 'hero', lookId: 'look' }];
  shotOf(p).data.storyboard.extraRefs = [{ entityId: 'voice', clipStart: 4 }];
  const voices = expanded(p).items.filter((i) => i.kind === '声音' && i.sent);
  assert.deepEqual(
    voices.map((i) => i.clipStart),
    [1, 4],
  );
});
test('REF duration policy omits later voice clips without deleting saved references', () => {
  const p = fixture();
  p.entities.push(audio('first'), audio('later'));
  shotOf(p).data.storyboard.extraRefs = [
    { entityId: 'first', clipStart: 1 },
    { entityId: 'later', clipStart: 2 },
  ];
  const cap = capabilities({ constraints: { input_limits: { max_audio_duration_seconds: 3 } } });
  const info = expanded(p, cap);
  assert.deepEqual(
    info.items.filter((i) => i.sent).map((i) => i.a.id),
    ['first'],
  );
  assert.match(info.items[1].reason, /参考时长/);
  assert.equal(shotOf(p).data.storyboard.extraRefs.length, 2);
});
test('an unavailable or wrong-kind profile main image is visible but never marked sent', () => {
  const p = fixture();
  p.entities.push(character('hero', 'wrong'), audio('wrong'));
  shotOf(p).data.cast = [{ characterId: 'hero', lookId: 'look' }];
  let info = expanded(p);
  assert.equal(info.items[0].sent, false);
  assert.match(info.items[0].reason, /类型/);
  p.entities.find((e) => e.id === 'wrong').type = 'image';
  p.entities.find((e) => e.id === 'wrong').data.missingFile = true;
  info = expanded(p);
  assert.equal(info.items[0].sent, false);
  assert.match(info.items[0].reason, /文件缺失/);
});
test('continue previous only previews a frame bound to the currently chosen real artifact', () => {
  const p = fixture();
  const current = shotOf(p);
  current.order = 1;
  current.data.storyboard.mode = 'fl';
  current.data.storyboard.continuePrevious = true;
  current.data.storyboard.previousFrame = {
    shotId: 'previous',
    selectedAssetId: 'chosen',
    frameAssetId: 'frame',
  };
  p.entities.push(
    entity('previous', 'shot', { seconds: 5, selectedAssetId: 'chosen' }, 'scene'),
    video('chosen'),
    video('changed'),
    image('frame'),
  );
  p.entities.find((e) => e.id === 'frame').data.sourceSelection = {
    kind: 'artifact_frame',
    position: 'last',
    artifact_id: 'chosen',
  };
  for (const id of ['chosen', 'changed'])
    p.jobs.push({
      id: 'job-' + id,
      status: 'succeeded',
      client_ref: { project_id: 'project', shot_id: 'previous' },
      effective_request: { mode: 'fl', model: 'MiniMax-H3-Pruned-Rank8-INT8' },
      artifacts: [{ id, kind: 'video' }],
    });
  let state = view(p),
    info = expandStoryboard(
      state.shots.find((s) => s.id === 'shot'),
      state,
    );
  assert.equal(info.items[0].a.id, 'frame');
  assert.equal(info.items[0].sent, true);
  p.entities.find((e) => e.id === 'previous').data.selectedAssetId = 'changed';
  state = view(p);
  info = expandStoryboard(
    state.shots.find((s) => s.id === 'shot'),
    state,
  );
  assert.equal(info.items[0].sent, false);
  assert.match(info.items[0].reason, /当前选定/);
});
test('drop classification rejects non-image first frames and preserves inactive FL audio/video refs', () => {
  const p = fixture();
  p.entities.push(audio('voice'), video('motion'));
  const state = view(p),
    s = state.shots[0];
  s.mode = 'FL';
  assert.equal(
    storyboardDrop(s, { asset: state.assets.find((a) => a.id === 'voice') }, 'start', state).patch,
    null,
  );
  assert.deepEqual(
    storyboardDrop(s, { asset: state.assets.find((a) => a.id === 'motion') }, 'smart', state).patch,
    { refs: ['motion'] },
  );
  assert.equal(s.refs.length, 0);
});
test('profile look imageIds empty explicitly clears gallery while absent imageIds keeps legacy images', () => {
  const p = fixture();
  const hero = character('hero', 'photo');
  hero.data.looks = [
    { id: 'clear', name: 'clear', gallery: { main: 'photo' }, imageIds: [] },
    { id: 'legacy', name: 'legacy', gallery: { main: 'photo' } },
  ];
  p.entities.push(hero, image('photo'));
  const looks = view(p).profiles[0].variants;
  assert.deepEqual(looks[0].images, []);
  assert.deepEqual(looks[1].images, ['photo']);
});
test('look voice binding and chosen asset remain explicit; succeeded new jobs do not auto-adopt', () => {
  const p = fixture();
  const hero = character('hero', 'photo', 'main-voice');
  hero.data.voices.push({ id: 'other', assetId: 'look-voice', clipStart: 3 });
  hero.data.looks[0].voiceId = 'other';
  p.entities.push(hero, image('photo'), audio('main-voice'), audio('look-voice'), video('keep'));
  shotOf(p).data.cast = [{ characterId: 'hero', lookId: 'look' }];
  shotOf(p).data.selectedAssetId = 'keep';
  const info = expanded(p);
  assert.equal(info.items.find((i) => i.kind === '声音').a.id, 'look-voice');
  assert.equal(info.items.find((i) => i.kind === '声音').clipStart, 3);
  mergeJobCandidate(p, {
    id: 'new-job',
    status: 'succeeded',
    client_ref: { project_id: 'project', shot_id: 'shot', shot_version: 1 },
    artifacts: [{ id: 'new-result', kind: 'video', mime: 'video/mp4' }],
  });
  assert.equal(view(p).shots[0].chosen, 'keep');
  assert.ok(view(p).shots[0].versions.some((v) => v.artifactId === 'new-result'));
});
test('storyboard preflight snapshot invalidates when shared look, clip start or global style changes', async () => {
  const p = fixture();
  p.entities.push(character('hero', 'photo', 'voice'), image('photo'), audio('voice'));
  shotOf(p).data.cast = [{ characterId: 'hero', lookId: 'look' }];
  let before = await digest(shotSnapshot(p, 'shot'));
  p.entities.find((e) => e.id === 'hero').data.voices[0].clipStart = 2;
  assert.notEqual(await digest(shotSnapshot(p, 'shot')), before);
  before = await digest(shotSnapshot(p, 'shot'));
  p.entities.find((e) => e.id === 'hero').data.looks[0].description = 'black coat';
  assert.notEqual(await digest(shotSnapshot(p, 'shot')), before);
  before = await digest(shotSnapshot(p, 'shot'));
  p.journey.storyboard.style = 'Watercolor';
  assert.notEqual(await digest(shotSnapshot(p, 'shot')), before);
});
test('an unrelated completed shot or profile never invalidates this shots frozen preflight', async () => {
  const p = fixture();
  p.entities.push(image('photo'), character('hero', 'photo'));
  shotOf(p).data.cast = [{ characterId: 'hero', lookId: 'look' }];
  const before = await digest(shotSnapshot(p, 'shot'));
  p.entities.push(
    entity('other-shot', 'shot', { seconds: 5, prompt: 'A different scene' }, 'scene', 1),
    character('unused', 'photo'),
  );
  mergeJobCandidate(p, {
    id: 'other-job',
    status: 'succeeded',
    client_ref: { project_id: 'project', shot_id: 'other-shot', shot_version: 1 },
    artifacts: [{ id: 'other-result', kind: 'video', mime: 'video/mp4' }],
  });
  assert.equal(await digest(shotSnapshot(p, 'shot')), before);
});

test('creator shape, keep-sound and another-version patches touch only their controls', () => {
  const p = fixture(),
    shot = shotOf(p),
    cap = capabilities();
  shot.data.h3 = { controls: { duration: 5, resolution: '480P' } };
  assert.equal(view(p, cap).shots[0].aspect, '16:9');
  assert.equal(view(p, cap).shots[0].keepAudio, true);
  patchStoryboardShot(p, 'shot', { aspect: '9:16', keepAudio: false });
  assert.deepEqual(shot.data.h3.controls, {
    duration: 5,
    resolution: '480P',
    aspect_ratio: '9:16',
    generate_audio: false,
  });
  assert.equal(view(p, cap).shots[0].aspect, '9:16');
  assert.equal(view(p, cap).shots[0].keepAudio, false);
  patchStoryboardShot(p, 'shot', { keepAudio: true });
  assert.equal('generate_audio' in shot.data.h3.controls, false);
  // A new version only changes a locked seed; an unlocked seed is random anyway.
  patchStoryboardShot(p, 'shot', { newSeed: true });
  assert.equal('seed' in shot.data.h3.controls, false);
  shot.data.h3.controls.seed = '7';
  patchStoryboardShot(p, 'shot', { newSeed: true });
  assert.match(shot.data.h3.controls.seed, /^\d+$/);
  assert.notEqual(shot.data.h3.controls.seed, '7');
  assert.equal(
    applyStoryboardAction(p, { type: 'update', id: 'shot', patch: { aspect: '4:3' } }),
    false,
  );
});

test('submit hints: video or many references, and audio without written lines', async () => {
  const { submitHints } = await import('./storyboard-domain.js');
  const t = (key) => key;
  const item = (kind) => ({ kind, sent: true });
  assert.deepEqual(submitHints({ prompt: 'a cat' }, [item('图片')], t), []);
  assert.deepEqual(submitHints({ prompt: 'a cat' }, [item('视频')], t), ['hint.focusRefs']);
  assert.deepEqual(
    submitHints(
      { prompt: 'x' },
      Array.from({ length: 7 }, () => item('图片')),
      t,
    ),
    ['hint.focusRefs'],
  );
  assert.deepEqual(submitHints({ prompt: 'she sings' }, [item('声音')], t), ['hint.writeLines']);
  assert.deepEqual(submitHints({ prompt: '她说：“我们走吧”' }, [item('声音')], t), []);
});
