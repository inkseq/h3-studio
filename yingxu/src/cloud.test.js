import { upsertGeneratedSound } from './generated-sound-model.js';
import test from 'node:test';
import { captionSignature } from './caption-model.js';
import { videoBinding, boundVideoRange } from './video-cut-model.js';
import assert from 'node:assert/strict';
import { createStore, validateProject } from './store.js';
import { createCloudClient, CloudError, safeCloudPath } from './cloud-client.js';
import {
  cloudSystemMessage,
  blankCloudProject,
  freestyleProject,
  removeFreestyleInput,
  copyForCloud,
  controlsForRecipe,
  selectedProfileRecipe,
  buildPlanPayload,
  digest,
  shotSnapshot,
  mergeJobCandidate,
  estimateLabel,
  recoveryMessage,
} from './cloud-model.js';
import { createCloudController } from './cloud-controller.js';
import { HISTORY_ACTIONS } from './creation-history-model.js';

test('native media targets stream through exact account-fenced routes without reading bytes or storing access links', async () => {
  let reads = 0;
  const native = createCloudClient({
    fetcher: () => {
      throw Error('native URL creation must not fetch');
    },
  });
  const h = await harness({
    setAccount: native.setAccount,
    nativeContentURL: native.nativeContentURL,
    download: async () => {
      reads++;
      return new Blob(['processing bytes']);
    },
  }).start();
  try {
    const preview = h.getHandlers().native('cloud_asset_reference'),
      attachment = h.getHandlers().native('cloud_artifact_video', { download: true });
    assert.equal(preview.href, '/v1/assets/reference/content?expected_account=superdan');
    assert.equal(
      attachment.href,
      '/v1/artifacts/video/content?expected_account=superdan&download=1',
    );
    assert.equal(preview.account, 'superdan');
    assert.equal(preview.projectId, h.store.getState().project.id);
    assert.ok(Object.isFrozen(preview));
    h.store.updateProject({ logline: 'An ordinary edit keeps the source valid' });
    assert.doesNotThrow(preview.assertCurrent);
    for (const id of [
      'cloud_asset_bad/path',
      'cloud_artifact_a?token=secret',
      'cloud_asset_../foreign',
      'local-file',
    ])
      assert.throws(() => h.getHandlers().native(id), /client.mediaPath/);
    assert.throws(() => h.getHandlers().native('cloud_asset_reference', { download: 'yes' }));
    assert.equal(reads, 0);
    assert.doesNotMatch(
      JSON.stringify(h.store.exportProject()),
      /expected_account|r2\.cloudflarestorage|signature=/,
    );
    assert.equal(
      await (await h.getHandlers().read('cloud_artifact_video')).text(),
      'processing bytes',
    );
    assert.equal(reads, 1);
  } finally {
    h.controller.destroy();
  }
});

for (const change of ['account', 'project', 'same-project reopening'])
  test(`native media target stays bound and rejects after ${change}`, async () => {
    const native = createCloudClient(),
      h = await harness({
        setAccount: native.setAccount,
        nativeContentURL: native.nativeContentURL,
      }).start();
    try {
      const original = h.store.getState().project.id,
        target = h.getHandlers().native('cloud_artifact_original', { download: true });
      if (change === 'account') {
        h.api.me = async () => ({ username: 'supervan' });
        await h.controller.recheckSession();
        assert.equal(h.getHandlers().native, null);
      } else if (change === 'project') await h.controller.create('Another cloud story');
      else {
        h.controller.leave();
        await h.controller.open(original);
        assert.equal(h.store.getState().project.id, original);
      }
      assert.throws(target.assertCurrent, /账户或项目已经切换/);
      assert.equal(
        target.href,
        '/v1/artifacts/original/content?expected_account=superdan&download=1',
      );
      assert.equal(target.projectId, original);
    } finally {
      h.controller.destroy();
    }
  });

function memory() {
  const map = new Map();
  return {
    map,
    getItem: (key) => map.get(key) ?? null,
    setItem: (key, value) => map.set(key, value),
  };
}
function fixture() {
  const storage = memory(),
    store = createStore({ storage });
  store.newProject('原本机作品');
  const chapter = store.addEntity('chapter'),
    scene = store.addEntity('scene', chapter),
    shot = store.addEntity('shot', scene, {
      description: '雨夜女孩走入车站',
      data: { seconds: 5 },
    });
  return { storage, store, chapter, scene, shot };
}
const schema = {
  duration: { type: 'integer', minimum: 4, maximum: 15, default: 5 },
  resolution: { type: 'string', enum: ['768P', '512P'], default: '768P' },
  seed: { type: ['string', 'null'], default: null },
  generate_audio: { type: 'boolean', default: true },
  shift_video: { type: ['number', 'null'], default: null },
  guides: { type: 'array', default: [] },
  video_audio: { type: 'object', default: {} },
  audio_tile_size: { type: 'integer', default: 512, available: false },
};
const recipe = { id: 'h3-fl', mode: 'fl', controls: schema },
  capabilities = {
    capabilities_version: 'test-v1',
    execution_enabled: true,
    simulation: true,
    recipes: [recipe, { id: 'h3-ref', mode: 'ref', controls: schema }],
  };
function harness(overrides = {}) {
  const f = fixture(),
    remote = new Map(),
    calls = [],
    jobs = [],
    api = {
      reset() {},
      authConfig: async () => ({ authentication: 'password', auth_ready: true }),
      me: async () => {
        throw new CloudError('login', { status: 401 });
      },
      login: async (username) => ({ username }),
      logout: async () => ({ ok: true }),
      capabilities: async () => capabilities,
      generationAvailability: async () => ({
        version: 1,
        observed_at: Date.now() / 1000,
        expires_at: Date.now() / 1000 + 10,
        poll_after_seconds: 10,
        advisory_only: true,
        profiles: [
          {
            deployment_profile_id: null,
            modes: Object.fromEntries(
              capabilities.recipes.map((r) => [
                r.mode,
                { recipe_id: r.id, state: 'ready', available: true, reason_code: null },
              ]),
            ),
          },
        ],
      }),
      projects: async () => ({
        projects: [...remote.values()].map((r) => ({
          id: r.id,
          title: r.project.title,
          version: r.version,
        })),
      }),
      createProject: async (project) => {
        const r = { id: project.id, version: 1, project: structuredClone(project) };
        remote.set(project.id, r);
        return structuredClone(r);
      },
      project: async (id) => structuredClone(remote.get(id)),
      saveProject: async (id, project, version) => {
        calls.push(['save', id, version]);
        const old = remote.get(id);
        if (old.version !== version) throw new CloudError('conflict', { status: 409 });
        const r = { id, version: version + 1, project: structuredClone(project) };
        remote.set(id, r);
        return structuredClone(r);
      },
      jobs: async () => ({ jobs: structuredClone(jobs) }),
      artifacts: async () => ({ artifacts: [] }),
      upload: async (file, project, id, key) => {
        calls.push(['upload', file.name, project, id, key]);
        return {
          asset_id: 'asset1',
          status: 'ready',
          kind: 'image',
          metadata: { mime: file.type },
          content_url: '/v1/assets/asset1/content',
        };
      },
      derivative: async (id, range, key) => {
        calls.push(['clip', id, range, key]);
        return { asset_id: 'clip1', status: 'ready' };
      },
      plan: async (payload, key) => {
        calls.push(['plan', payload, key]);
        return {
          plan_id: 'plan1',
          status: 'ready',
          expires_at: Date.now() / 1000 + 900,
          effective_request: payload.controls,
        };
      },
      submit: async (id, key) => {
        calls.push(['submit', id, key]);
        const payload = calls.filter((c) => c[0] === 'plan').at(-1)[1],
          job = {
            id: 'job1',
            plan_id: id,
            client_ref: payload.client_ref,
            status: 'queued',
            artifacts: [],
          };
        if (!jobs.length) jobs.push(job);
        return structuredClone(job);
      },
      cancel: async (id) => {
        jobs.find((j) => j.id === id).status = 'cancelled';
      },
      download: async () => new Blob(['private']),
      ...overrides,
    };
  let handlers;
  const controller = createCloudController({
    store: f.store,
    storage: f.storage,
    client: api,
    readFile: async () => new Blob(['asset'], { type: 'image/png' }),
    mediaHandlers: (value) => (handlers = value),
  });
  return {
    ...f,
    controller,
    api,
    remote,
    calls,
    jobs,
    getHandlers: () => handlers,
    async start() {
      await controller.login('superdan', 'test-only');
      await controller.create('云副本', { copy: true });
      return this;
    },
  };
}

test('cloud workspace preserves original local bytes, undo history and separate account draft', () => {
  const { store, storage, shot } = fixture(),
    before = store.exportProject(),
    raw = storage.getItem('yingxu-studio-v4'),
    undo = store.getState().undoCount,
    cloud = copyForCloud(before);
  assert.equal(store.enterCloudProject(cloud, { account: 'superdan', version: 1 }), true);
  store.updateEntity(shot, { description: '云端修改' });
  assert.equal(storage.getItem('yingxu-studio-v4'), raw);
  assert.equal(store.getState().workspace.dirty, true);
  assert.equal(store.getCloudDraft('supervan', cloud.id), null);
  assert.equal(
    store.getCloudDraft('superdan', cloud.id).project.entities.find((e) => e.id === shot)
      .description,
    '云端修改',
  );
  assert.equal(store.newProject('不得覆盖'), false);
  store.leaveCloudProject();
  assert.deepEqual(store.exportProject(), before);
  assert.equal(store.getState().undoCount, undo);
  assert.equal(storage.getItem('yingxu-studio-v4'), raw);
});
test('workspace epoch supplements account epoch for late same-ID upload and private media response', async () => {
  const h = await harness().start();
  let finishUpload, finishRead;
  h.api.upload = () => new Promise((r) => (finishUpload = r));
  h.api.download = () => new Promise((r) => (finishRead = r));
  const handlers = h.getHandlers(),
    upload = handlers.upload(new File(['x'], 'test.png', { type: 'image/png' }), 'image'),
    read = handlers.read('cloud_asset_test'),
    noUpload = assert.rejects(upload, /账户或项目已经切换/),
    noRead = assert.rejects(read, /账户或项目已经切换/);
  h.store.enterCloudProject(h.store.exportProject(), { account: 'superdan', version: 1 });
  finishUpload({ asset_id: 'late', status: 'ready' });
  finishRead(new Blob(['late-private']));
  await Promise.all([noUpload, noRead]);
  assert.equal(
    h.store.getState().project.entities.some((e) => e.data.cloudAssetId === 'late'),
    false,
  );
  h.controller.destroy();
});
test('ambiguous cancellation explains preserved evidence without treating it as a free retry', () => {
  const message = recoveryMessage({
    status: 'recovery_hold',
    error_code: 'cancellation_requires_reconciliation',
  });
  assert.match(message, /取消意愿/);
  assert.match(message, /费用记录保留/);
  assert.match(message, /勿重复提交/);
  assert.doesNotMatch(message, /cancellation_requires/);
});
test('cloud save acknowledges submitted snapshot without losing newer local edits', () => {
  const { store } = fixture();
  const cloud = copyForCloud(store.exportProject());
  store.enterCloudProject(cloud, { account: 'superdan', version: 1 });
  store.updateProject({ title: '版本二' });
  const submitted = store.exportProject();
  store.updateProject({ title: '继续编辑' });
  store.markCloudSaved(2, submitted);
  assert.equal(store.getState().workspace.serverVersion, 2);
  assert.equal(store.getState().workspace.dirty, true);
  assert.equal(store.getState().project.title, '继续编辑');
});
test('cloud copying keeps readable file identity but removes old project scoped asset ids', () => {
  const p = blankCloudProject('复制');
  p.entities = [
    {
      data: {
        fileId: 'cloud_asset_old',
        cloudAssetId: 'old',
        cloudContentPath: '/v1/assets/old/content',
      },
    },
  ];
  const copy = copyForCloud(p);
  assert.equal(copy.entities[0].data.fileId, 'cloud_asset_old');
  assert.equal(copy.entities[0].data.cloudAssetId, undefined);
  assert.notEqual(copy.id, p.id);
  assert.equal(p.entities[0].data.cloudAssetId, 'old');
});
test('API refuses remote, normalized escape and non-API URLs and uses same-origin cookies', async () => {
  for (const value of [
    'https://evil.example/x',
    '//evil.example/v1/a',
    '/v1/../outside',
    '/v1/../../api/evil',
    '/v1/a#token',
    '/v1/a\\x',
  ])
    assert.throws(() => safeCloudPath(value));
  let call;
  const client = createCloudClient({
    fetcher: async (...args) => {
      call = args;
      return new Response('{"ok":true}', { status: 200 });
    },
  });
  await client.submit('plan1', 'same-intent');
  assert.equal(call[0], '/v1/jobs');
  assert.equal(call[1].headers['Idempotency-Key'], 'same-intent');
  assert.equal(call[1].credentials, 'same-origin');
  assert.deepEqual(JSON.parse(call[1].body), { plan_id: 'plan1' });
});
test('API never retries submit on network failure; reset rejects late account response', async () => {
  let count = 0;
  const failed = createCloudClient({
    fetcher: async () => {
      count++;
      throw Error('offline');
    },
  });
  await assert.rejects(failed.submit('p', 'k'));
  assert.equal(count, 1);
  let resolve;
  const client = createCloudClient({ fetcher: () => new Promise((r) => (resolve = r)) }),
    pending = client.projects();
  client.reset();
  resolve(new Response('{"projects":[]}'));
  await assert.rejects(pending, /account has changed/);
});
test('control contract omits unavailable fields, duplicate guide fields, keeps null seed and uint64 string', () => {
  const values = controlsForRecipe(recipe, { seed: '18446744073709551615' }, 5);
  assert.equal(values.seed, '18446744073709551615');
  assert.equal(Object.hasOwn(values, 'audio_tile_size'), false);
  assert.equal(Object.hasOwn(values, 'guides'), false);
  assert.equal(Object.hasOwn(values, 'video_audio'), false);
  const { store, shot } = fixture(),
    payload = buildPlanPayload(store.exportProject(), shot, {
      recipe,
      capabilitiesVersion: 'test',
    });
  assert.equal(payload.controls.seed, null);
  assert.equal(payload.inputs.guides.length, 0);
});
test('live cloud preset fills only unset encoder and decode controls while preserving explicit choices', () => {
  const deployed = {
    ...recipe,
    controls: {
      ...recipe.controls,
      encoder_device: { type: 'string', enum: ['default', 'cpu'], default: 'default' },
      video_decode: { type: 'string', enum: ['normal', 'tiled'], default: 'normal' },
    },
    deployment_preset: {
      controls: { encoder_device: 'cpu', video_decode: 'tiled' },
      applies_to: 'unset_controls_only',
    },
  };
  const { store, shot } = fixture(),
    payload = buildPlanPayload(store.exportProject(), shot, { recipe: deployed });
  assert.equal(payload.controls.encoder_device, 'cpu');
  assert.equal(payload.controls.video_decode, 'tiled');
  assert.equal(store.exportProject().entities.find((e) => e.id === shot).data.h3, undefined);
  store.updateEntity(shot, {
    data: { h3: { controls: { encoder_device: 'default', video_decode: 'normal' } } },
  });
  const explicit = buildPlanPayload(store.exportProject(), shot, { recipe: deployed });
  assert.equal(explicit.controls.encoder_device, 'default');
  assert.equal(explicit.controls.video_decode, 'normal');
  assert.equal(
    controlsForRecipe(deployed, { encoder_device: 'unknown-explicit' }).encoder_device,
    'unknown-explicit',
  );
  assert.equal(
    controlsForRecipe({
      ...deployed,
      deployment_preset: {
        controls: { encoder_device: 'unsupported', video_decode: 'unsupported' },
      },
    }).encoder_device,
    'default',
  );
  assert.equal(
    controlsForRecipe({ ...deployed, deployment_preset: undefined }).video_decode,
    'normal',
  );
});
test('mixed first/last frame and reference modes fail before submission', () => {
  const { store, shot } = fixture(),
    image = store.addEntity('image', null, {
      data: { fileId: 'cloud_asset_i', cloudAssetId: 'i' },
    });
  store.addLink(image, shot, 'firstFrame');
  assert.throws(
    () => buildPlanPayload(store.exportProject(), shot, { recipe: { ...recipe, mode: 'ref' } }),
    /不接受首尾帧/,
  );
  const video = store.addEntity('video', null, {
    data: { fileId: 'cloud_asset_v', cloudAssetId: 'v' },
  });
  store.addLink(video, shot, 'motion');
  assert.throws(() => buildPlanPayload(store.exportProject(), shot, { recipe }), /不能混用/);
});
test('guide file replacement invalidates source hash and guides use canonical media_id', async () => {
  const { store, shot } = fixture(),
    image = store.addEntity('image', null, {
      data: { fileId: 'cloud_asset_i', cloudAssetId: 'i' },
    });
  store.updateEntity(shot, {
    data: { h3: { guides: [{ media_id: image, time_seconds: 1, use_audio: false }] } },
  });
  const before = await digest(shotSnapshot(store.exportProject(), shot)),
    payload = buildPlanPayload(store.exportProject(), shot, { recipe });
  assert.equal(payload.inputs.guides[0].media_id, 'i');
  assert.equal(payload.inputs.guides[0].asset_id, undefined);
  store.updateEntity(image, { data: { fileId: 'cloud_asset_new', cloudAssetId: 'new' } });
  assert.notEqual(await digest(shotSnapshot(store.exportProject(), shot)), before);
});
test('late old-version result creates an unselected candidate and preserves current chosen asset', () => {
  const { store, shot } = fixture(),
    p = store.exportProject(),
    s = p.entities.find((e) => e.id === shot);
  s.data.selectedAssetId = 'keep';
  s.version = 3;
  const job = {
    id: 'j',
    status: 'succeeded',
    simulation: true,
    client_ref: { project_id: p.id, shot_id: shot, shot_version: 2, source_hash: 'old' },
    artifacts: [
      {
        id: 'a',
        kind: 'video',
        mime: 'video/mp4',
        size_bytes: 20,
        content_url: '/v1/artifacts/a/content',
      },
    ],
  };
  const result = mergeJobCandidate(p, job, { snapshotHash: 'new' });
  assert.equal(result.oldVersion, true);
  assert.equal(s.data.selectedAssetId, 'keep');
  assert.deepEqual(s.data.candidateIds, ['result-a']);
  assert.equal(p.entities.find((e) => e.id === 'result-a').data.simulation, true);
  assert.equal(p.entities.find((e) => e.id === 'result-a').data.bytes, 20);
  assert.equal(mergeJobCandidate(p, job).changed, false);
});
test('single shot cloud flow saves version before preflight and only submits frozen plan', async () => {
  const h = await harness().start();
  await h.controller.prepare(h.shot);
  assert.deepEqual(
    h.calls.map((c) => c[0]),
    ['save', 'plan'],
  );
  const payload = h.calls[1][1];
  assert.equal(payload.client_ref.project_id, h.store.getState().project.id);
  assert.match(payload.client_ref.source_hash, /^[a-f0-9]{64}$/);
  await h.controller.submit(h.shot);
  await h.controller.submit(h.shot);
  assert.equal(h.jobs.length, 1);
  assert.equal(
    h.calls.filter((c) => c[0] === 'submit')[0][2],
    h.calls.filter((c) => c[0] === 'submit')[1][2],
  );
  assert.equal(h.store.exportProject().jobs[0].status, 'queued');
  assert.equal(validateProject(h.store.exportProject()).ok, true);
  h.controller.destroy();
});
test('guided selected profile controls, REF anchors and availability keep their exact profile through preflight and submit', async () => {
  const fullSchema = {
      ...schema,
      steps: { type: 'integer', minimum: 1, maximum: 100, default: 20 },
      export_crf: { type: 'integer', minimum: 0, maximum: 51, default: 18 },
    },
    native = {
      ...capabilities,
      deployment_profiles: [
        {
          id: 'full-native',
          model_id: 'MiniMax-H3-Pruned-Rank8-INT8',
          generation_support: {
            ref: {
              enabled: true,
              configured: true,
              controls: fullSchema,
              input_support: {
                reference_images: true,
                reference_video: true,
                reference_audio: true,
                first_frame: true,
                last_frame: true,
              },
              limits: {
                input_count_semantics: 'native_roles_v2',
                max_images: 9,
                max_total_files: 12,
              },
            },
          },
        },
      ],
    },
    availability = {
      version: 1,
      observed_at: Date.now() / 1000,
      expires_at: Date.now() / 1000 + 10,
      advisory_only: true,
      profiles: [
        {
          deployment_profile_id: 'full-native',
          modes: { ref: { recipe_id: 'h3-ref', state: 'ready', available: true } },
        },
      ],
    };
  const h = await harness({
    capabilities: async () => native,
    generationAvailability: async () => availability,
  }).start();
  try {
    h.store.updateEntity(h.shot, {
      data: {
        h3: {
          recipeId: 'h3-ref',
          inputMode: 'ref',
          deploymentProfileId: 'full-native',
          controls: { steps: 100, export_crf: 0 },
        },
      },
    });
    const first = h.store.addEntity('image', null, {
      data: { fileId: 'cloud_asset_first', cloudAssetId: 'first' },
    });
    h.store.addLink(first, h.shot, 'firstFrame');
    const projected = selectedProfileRecipe(
      native,
      h.store.exportProject().entities.find((entity) => entity.id === h.shot).data.h3,
    );
    assert.equal(projected.controls.steps.maximum, 100);
    assert.equal(projected.input_support.first_frame, true);
    await h.controller.prepare(h.shot);
    const payload = h.calls.find((call) => call[0] === 'plan')[1];
    assert.equal(payload.deployment_profile_id, 'full-native');
    assert.equal(payload.controls.steps, 100);
    assert.equal(payload.controls.export_crf, 0);
    assert.equal(payload.inputs.first_frame.asset_id, 'first');
    assert.deepEqual(h.controller.getState().plans[h.shot].availabilitySettings, {
      deployment_profile_id: 'full-native',
      recipe_id: 'h3-ref',
    });
    await h.controller.submit(h.shot);
    assert.equal(h.calls.filter((call) => call[0] === 'submit').length, 1);
    assert.equal(
      h.store.exportProject().entities.find((entity) => entity.id === h.shot).data.h3
        .deploymentProfileId,
      'full-native',
    );
  } finally {
    h.controller.destroy();
  }
});
test('editing after preflight rejects submit and upstream scene changes also invalidate it', async () => {
  const h = await harness().start();
  await h.controller.prepare(h.shot);
  h.store.updateEntity(h.scene, { data: { script: '新台词' } });
  await assert.rejects(h.controller.submit(h.shot), /重新预检/);
  assert.equal(
    h.calls.some((c) => c[0] === 'submit'),
    false,
  );
  h.controller.destroy();
});
test('disabled execution still returns visible blocked preflight and performs no submit', async () => {
  const h = await harness({
    capabilities: async () => ({ ...capabilities, execution_enabled: false }),
    plan: async () => ({ plan_id: 'blocked', status: 'blocked', blockers: ['没有执行容量'] }),
  }).start();
  await h.controller.prepare(h.shot);
  assert.equal(h.controller.getState().plans[h.shot].blockers[0], '没有执行容量');
  await assert.rejects(h.controller.submit(h.shot));
  assert.equal(
    h.calls.some((c) => c[0] === 'submit'),
    false,
  );
  h.controller.destroy();
});
test('409 never overwrites either remote version or unsaved local draft', async () => {
  const h = await harness().start(),
    id = h.store.getState().project.id;
  h.remote.get(id).version = 2;
  h.store.updateProject({ title: '我仍保留的草稿' });
  await assert.rejects(h.controller.save(), /未覆盖远端/);
  assert.equal(h.remote.get(id).project.title, '云副本');
  assert.equal(h.store.exportProject().title, '我仍保留的草稿');
  assert.equal(h.controller.getState().conflict.id, id);
  assert.equal(h.storage.getItem('yingxu-studio-v4').includes('原本机作品'), true);
  h.controller.destroy();
});
test('lost submission response persists intent, blocks new plans and retries same key', async () => {
  const h = await harness().start();
  await h.controller.prepare(h.shot);
  let attempts = 0;
  const keys = [];
  h.api.submit = async (id, key) => {
    attempts++;
    keys.push(key);
    if (attempts === 1) throw new CloudError('连接断开');
    return {
      id: 'recovered',
      plan_id: id,
      status: 'queued',
      client_ref: h.calls.find((c) => c[0] === 'plan')[1].client_ref,
      artifacts: [],
    };
  };
  await assert.rejects(h.controller.submit(h.shot), /未确认/);
  await assert.rejects(h.controller.prepare(h.shot), /未确认/);
  await h.controller.submit(h.shot);
  assert.equal(keys[0], keys[1]);
  assert.equal(Object.keys(h.controller.getState().unknown).length, 0);
  h.controller.destroy();
});
test('confirmed capacity drain rejects once, preserves work and requires explicit new preflight', async () => {
  const h = await harness().start();
  await h.controller.prepare(h.shot);
  const project = h.store.exportProject();
  let submits = 0;
  const client = createCloudClient({
    fetcher: async (path, options) => {
      submits++;
      assert.equal(path, '/v1/jobs');
      assert.equal(options.method, 'POST');
      return new Response(JSON.stringify({ detail: 'capacity_drain_repreflight' }), {
        status: 409,
        headers: { 'Content-Type': 'application/json' },
      });
    },
  });
  h.api.submit = client.submit;
  await assert.rejects(
    h.controller.submit(h.shot),
    (error) =>
      error.status === 409 &&
      /GPU is sleeping or switching/.test(error.message) &&
      /Nothing was submitted/.test(error.message),
  );
  assert.equal(submits, 1);
  assert.deepEqual(h.controller.getState().unknown, {});
  assert.equal(h.controller.getState().plans[h.shot], undefined);
  assert.match(h.controller.getState().preflightErrors[h.shot], /Check again before generating/);
  assert.deepEqual(h.store.exportProject(), project);
  assert.equal(h.jobs.length, 0);
  await assert.rejects(h.controller.submit(h.shot), /先完成.*预检/);
  assert.equal(submits, 1);
  await h.controller.prepare(h.shot);
  assert.equal(h.controller.getState().preflightErrors[h.shot], null);
  assert.equal(submits, 1);
  h.controller.destroy();
});
test('other 409 errors do not discard cached generation plan as a capacity drain', async () => {
  const h = await harness().start();
  await h.controller.prepare(h.shot);
  h.api.submit = async () => {
    throw new CloudError('another_conflict', {
      status: 409,
      detail: { detail: 'another_conflict' },
    });
  };
  await assert.rejects(h.controller.submit(h.shot), /another_conflict/);
  assert.equal(h.controller.getState().plans[h.shot].plan_id, 'plan1');
  h.controller.destroy();
});
test('local reference upload preserves selected range binding and is done once', async () => {
  const h = harness();
  const asset = h.store.addEntity('video', null, {
    data: { fileId: 'local1', fileName: 'reference.mp4', mime: 'video/mp4' },
  });
  h.store.addLink(asset, h.shot, 'motion');
  const link = h.store.getState().project.links[0];
  h.store.updateEntity(h.shot, {
    data: {
      h3: { recipeId: 'h3-ref' },
      referenceRanges: { [link.id]: { start: 2, end: 5, duration: 20, fileId: 'local1' } },
    },
  });
  await h.start();
  await h.controller.prepare(h.shot);
  const current = h.store.exportProject(),
    s = current.entities.find((e) => e.id === h.shot);
  assert.equal(s.data.referenceRanges[link.id].fileId, 'cloud_asset_asset1');
  assert.equal(h.calls.filter((c) => c[0] === 'upload').length, 1);
  assert.equal(h.calls.find((c) => c[0] === 'plan')[1].inputs.videos[0].asset_id, 'clip1');
  h.controller.destroy();
});
test('logout restores original local work and clears cloud upload handler', async () => {
  const h = harness(),
    before = h.store.exportProject();
  await h.start();
  assert.equal(typeof h.getHandlers().upload, 'function');
  await h.controller.logout();
  assert.deepEqual(h.store.exportProject(), before);
  assert.equal(h.getHandlers().upload, null);
  assert.equal(h.controller.getState().account, null);
  h.controller.destroy();
});
test('unknown batch admission survives errors and cannot be bypassed by single-shot submit', async () => {
  const h = await harness().start();
  await h.controller.prepare(h.shot);
  const attempts = [];
  h.api.batch = async (project, ids, key) => {
    attempts.push({ project, ids, key });
    if (attempts.length === 1) throw new CloudError('network');
    return { id: 'batch1', status: 'active', items: [] };
  };
  await assert.rejects(h.controller.submitBatch([h.shot]));
  assert.equal(h.controller.getState().batchPending.shotIds[0], h.shot);
  await assert.rejects(h.controller.submit(h.shot), /待核对批次/);
  await assert.rejects(h.controller.prepare(h.shot), /未确认/);
  await h.controller.recoverBatch();
  assert.deepEqual(attempts[1], attempts[0]);
  assert.equal(h.controller.getState().batchPending, null);
  assert.equal(h.controller.getState().batches[0].id, 'batch1');
  h.controller.destroy();
});
test('empty explicit prompt is not silently replaced and fee wording separates unknown from mock', () => {
  const { store, shot } = fixture();
  store.updateEntity(shot, { data: { prompt: '' } });
  assert.throws(() => buildPlanPayload(store.exportProject(), shot, { recipe }), /先写清/);
  assert.match(estimateLabel({ cost_microusd: null }), /尚未报价/);
  assert.match(estimateLabel({ source: 'mock', cost_microusd: 0 }), /真实推理价格尚未验证/);
  assert.match(estimateLabel({ cost_microusd: 50000, currency: 'USD' }), /\$0.05 USD/);
});
test('freestyle uses the existing project schema and creates no plan or job', async () => {
  const p = freestyleProject('独立单镜');
  assert.equal(validateProject(p).ok, true);
  assert.equal(p.entities.filter((e) => e.type === 'chapter').length, 1);
  assert.equal(p.entities.filter((e) => e.type === 'scene').length, 1);
  assert.equal(p.entities.filter((e) => e.type === 'shot').length, 1);
  assert.equal(p.jobs.length, 0);
  assert.equal(p.journey.workspace, 'freestyle');
  const h = harness(),
    original = h.storage.getItem('yingxu-studio-v4');
  await h.controller.login('supervan', 'test-only');
  await h.controller.createQuick('快捷测试');
  assert.equal(h.store.getState().project.title, '快捷测试');
  assert.equal(
    h.calls.some((c) => ['plan', 'submit'].includes(c[0])),
    false,
  );
  assert.equal(h.storage.getItem('yingxu-studio-v4'), original);
  h.controller.destroy();
});
test('removing a freestyle zone preserves other uses, other shots and the original asset', () => {
  const { store, shot, scene } = fixture(),
    image = store.addEntity('image', null, { data: { fileId: 'keep-file' } }),
    other = store.addEntity('shot', scene);
  store.addLink(image, shot, 'firstFrame');
  store.addLink(image, shot, 'lastFrame');
  store.addLink(image, other, 'firstFrame');
  const p = store.exportProject(),
    version = p.entities.find((e) => e.id === shot).version;
  assert.equal(removeFreestyleInput(p, shot, image, { roles: ['firstFrame'] }), true);
  assert.equal(
    p.links.some((l) => l.target === shot && l.role === 'firstFrame'),
    false,
  );
  assert.equal(
    p.links.some((l) => l.target === shot && l.role === 'lastFrame'),
    true,
  );
  assert.equal(
    p.links.some((l) => l.target === other),
    true,
  );
  assert.equal(p.entities.find((e) => e.id === image).data.fileId, 'keep-file');
  assert.equal(p.entities.find((e) => e.id === shot).version, version + 1);
  assert.equal(removeFreestyleInput(p, shot, image, { roles: ['firstFrame'] }), false);
});
test('guide clip request uses the selected range and rejects replaced source file', async () => {
  const h = harness(),
    asset = h.store.addEntity('audio', null, {
      data: { fileId: 'local-audio', fileName: 'voice.wav', mime: 'audio/wav' },
    });
  h.store.updateEntity(h.shot, {
    data: {
      h3: {
        guides: [
          {
            media_id: asset,
            time_seconds: 1,
            use_audio: true,
            source_range: { start: 0, end: 3, fileId: 'local-audio' },
          },
        ],
      },
    },
  });
  await h.start();
  await h.controller.prepare(h.shot);
  assert.equal(h.calls.find((c) => c[0] === 'plan')[1].inputs.guides[0].media_id, 'clip1');
  h.store.updateEntity(asset, { data: { fileId: 'changed', cloudAssetId: 'changed' } });
  await assert.rejects(h.controller.prepare(h.shot), /原文件已改变/);
  h.controller.destroy();
});
test('asset recovery resumes same receipt once and only adds ready asset to library', async () => {
  const h = await harness().start(),
    projectId = h.store.getState().project.id,
    asset = {
      id: 'recover1',
      asset_id: 'recover1',
      project_id: projectId,
      status: 'storage_unknown',
      kind: 'audio',
      file_name: 'reference.wav',
      mime: 'audio/wav',
      metadata: { duration: 4 },
      content_url: '/v1/assets/recover1/content',
    },
    attempts = [];
  h.api.assets = async () => ({ assets: [structuredClone(asset)] });
  h.api.storageUsage = async () => ({
    accounted_bytes: 1024,
    limit_bytes: 10240,
    active_uploads: 1,
    max_active: 2,
  });
  h.api.resumeAsset = async (id) => {
    attempts.push(id);
    asset.status = 'ready';
    return structuredClone(asset);
  };
  await h.controller.assetInventory();
  assert.equal(h.controller.getState().assets[0].status, 'storage_unknown');
  await h.controller.resumeAsset('recover1');
  assert.deepEqual(attempts, ['recover1']);
  assert.equal(
    h.calls.some((c) => c[0] === 'upload'),
    false,
  );
  const created = h.store
    .getState()
    .project.entities.filter((e) => e.data.cloudAssetId === 'recover1');
  assert.equal(created.length, 1);
  assert.equal(
    h.store.getState().project.links.some((l) => l.source === created[0].id),
    false,
  );
  assert.equal(h.controller.getState().usage.accounted_bytes, 1024);
  await h.controller.resumeAsset('recover1');
  assert.equal(
    h.store.getState().project.entities.filter((e) => e.data.cloudAssetId === 'recover1').length,
    1,
  );
  h.controller.leave();
  assert.equal(h.controller.getState().assets.length, 0);
  h.controller.destroy();
});
test('unready and wrong-project assets never become project inputs', async () => {
  const h = await harness().start(),
    count = h.store.getState().project.entities.length;
  h.api.asset = async () => ({ id: 'alien', status: 'ready', kind: 'image', project_id: 'other' });
  await assert.rejects(h.controller.attachAsset('alien'), /其他项目/);
  h.api.asset = async () => ({
    id: 'failed',
    status: 'failed',
    kind: 'image',
    project_id: h.store.getState().project.id,
  });
  await assert.rejects(h.controller.attachAsset('failed'), /尚未就绪/);
  assert.equal(h.store.getState().project.entities.length, count);
  h.controller.destroy();
});

test('budget reservation wording is never rendered as an actual charge', () => {
  const text = estimateLabel({
    kind: 'budget_reservation',
    cost_microusd: 250000,
    currency: 'USD',
  });
  assert.match(text, /预算预留/);
  assert.match(text, /不是最终账单/);
  assert.match(text, /0.25/);
});

async function renderHarness() {
  const h = await harness({
    capabilities: async () => ({
      ...capabilities,
      execution_enabled: false,
      chapter_render: { implemented: true, enabled: true },
    }),
  }).start();
  h.video = h.store.addEntity('video', null, {
    data: {
      fileId: 'cloud_artifact_selected',
      cloudArtifactId: 'selected',
      metadata: { duration: 5 },
      simulation: true,
    },
  });
  h.store.updateEntity(h.shot, { data: { selectedAssetId: h.video } });
  h.store.editProject((p) => {
    p.journey = { sound: { mode: 'silent' } };
  });
  h.api.renderPlan = async (payload) => {
    h.calls.push(['renderPlan', payload]);
    return {
      plan_id: 'render1',
      status: 'ready',
      client_ref: payload.client_ref,
      expires_at: Date.now() / 1000 + 900,
      output_spec: { width: 720, height: 1280, fps: 24, actual_duration: 5, frame_count: 120 },
      timeline: { shots: [], audio_tracks: [] },
    };
  };
  h.api.submit = async (planId, key) => {
    h.calls.push(['submitRender', planId, key]);
    const existing = h.jobs.find((j) => j.plan_id === planId);
    if (existing) return structuredClone(existing);
    const job = {
      id: 'cut1',
      plan_id: planId,
      recipe_id: 'chapter-roughcut-v1',
      client_ref: { project_id: h.store.getState().project.id, chapter_id: h.chapter },
      status: 'queued',
      artifacts: [],
    };
    h.jobs.push(job);
    return structuredClone(job);
  };
  return h;
}

test('chapter CPU plan saves before preflight, does not require GPU execution and never adopts output as shot candidate', async () => {
  const h = await renderHarness();
  await h.controller.prepareRender(h.chapter, { resolution: '720P', aspect: '9:16' });
  assert.deepEqual(
    h.calls.map((c) => c[0]),
    ['save', 'renderPlan'],
  );
  assert.equal(
    h.calls.some((c) => c[0] === 'upload'),
    false,
  );
  await h.controller.submitRender(h.chapter);
  h.jobs[0].status = 'succeeded';
  h.jobs[0].simulation = true;
  h.jobs[0].artifacts = [
    {
      id: 'render-output',
      kind: 'video',
      mime: 'video/mp4',
      content_url: '/v1/artifacts/render-output/content',
    },
  ];
  await h.controller.refresh();
  const p = h.store.exportProject();
  assert.equal(
    p.entities.some((e) => e.data.cloudArtifactId === 'render-output'),
    false,
  );
  assert.equal(p.entities.find((e) => e.id === h.shot).data.selectedAssetId, h.video);
  assert.equal(
    p.jobs.some((j) => j.id === 'cut1'),
    true,
  );
  h.controller.destroy();
});

test('chapter change blocks old preflight and blocked source cannot submit', async () => {
  const h = await renderHarness();
  await h.controller.prepareRender(h.chapter, { resolution: '480P', aspect: '9:16' });
  h.store.updateEntity(h.shot, { data: { seconds: 6 } });
  await assert.rejects(h.controller.submitRender(h.chapter), /重新预检/);
  h.api.renderPlan = async (payload) => ({
    plan_id: 'blocked-cut',
    status: 'blocked',
    client_ref: payload.client_ref,
    blockers: ['视频比目标短'],
  });
  await h.controller.prepareRender(h.chapter, { resolution: '720P', aspect: '9:16' });
  assert.deepEqual(h.controller.getState().renderPlans[h.chapter].blockers, ['视频比目标短']);
  await assert.rejects(h.controller.submitRender(h.chapter), /预检/);
  assert.equal(
    h.calls.some((c) => c[0] === 'submitRender'),
    false,
  );
  h.controller.destroy();
});

test('chapter lost response retains original intent across local edits and reopening; recovery is owner scoped', async () => {
  const h = await renderHarness();
  await h.controller.prepareRender(h.chapter, { resolution: '720P', aspect: '9:16' });
  const original = h.api.submit,
    keys = [];
  h.api.submit = async (id, key) => {
    keys.push(key);
    if (keys.length === 1) throw new CloudError('disconnect');
    return original(id, key);
  };
  await assert.rejects(h.controller.submitRender(h.chapter), /未确认/);
  await assert.rejects(
    h.controller.prepareRender(h.chapter, { resolution: '480P', aspect: '16:9' }),
    /未确认/,
  );
  const id = h.store.getState().project.id;
  h.controller.leave();
  assert.equal(Object.keys(h.controller.getState().renderPending).length, 0);
  await h.controller.open(id);
  assert.equal(h.controller.getState().renderPending[h.chapter].planId, 'render1');
  h.store.updateEntity(h.shot, { data: { seconds: 3 } });
  await h.controller.submitRender(h.chapter);
  assert.equal(keys[0], keys[1]);
  assert.equal(h.jobs.length, 1);
  assert.equal(Object.keys(h.controller.getState().renderPending).length, 0);
  await h.controller.logout();
  await h.controller.login('supervan', 'test-only');
  assert.equal(Object.keys(h.controller.getState().renderPlans).length, 0);
  assert.equal(Object.keys(h.controller.getState().renderPending).length, 0);
  h.controller.destroy();
});

test('rough-cut plan with another project identity is rejected before submission', async () => {
  const h = await renderHarness();
  h.api.renderPlan = async () => ({
    plan_id: 'alien',
    status: 'ready',
    client_ref: { project_id: 'other', chapter_id: h.chapter },
  });
  await assert.rejects(
    h.controller.prepareRender(h.chapter, { resolution: '720P', aspect: '9:16' }),
    /响应无效/,
  );
  assert.equal(Object.keys(h.controller.getState().renderPlans).length, 0);
  h.controller.destroy();
});

test('batch preflight preserves per-shot failures, checks independent shots, never submits and clears repaired errors', async () => {
  const h = harness(),
    second = h.store.addEntity('shot', h.scene, { description: '有效镜头' });
  h.store.updateEntity(h.shot, { data: { prompt: '' } });
  await h.start();
  const failures = await h.controller.prepareBatch([h.shot, second]);
  assert.equal(failures.length, 1);
  assert.equal(failures[0].id, h.shot);
  assert.ok(h.controller.getState().preflightErrors[h.shot]);
  assert.equal(h.controller.getState().plans[second].status, 'ready');
  assert.equal(
    h.calls.some((c) => c[0] === 'submit'),
    false,
  );
  h.store.updateEntity(h.shot, { data: { prompt: '修复后描述' } });
  await h.controller.prepare(h.shot);
  assert.equal(h.controller.getState().preflightErrors[h.shot], null);
  h.controller.leave();
  assert.deepEqual(h.controller.getState().preflightErrors, {});
  h.controller.destroy();
});

test('history paging deduplicates shifting offsets, refresh retains older jobs and follows known active ones', async () => {
  const h = await harness().start(),
    projectId = h.store.getState().project.id,
    make = (id, status = 'failed') => ({
      id,
      status,
      client_ref: { project_id: projectId, shot_id: h.shot },
      artifacts: [],
    });
  let page = 'initial';
  const offsets = [],
    reads = [];
  h.api.activitySummary = async () => ({ total: 102, counts: { failed: 101, running: 1 } });
  h.api.jobs = async (id, { offset = 0 } = {}) => {
    offsets.push(offset);
    return {
      jobs: offset
        ? [make('j99'), make('old', 'running')]
        : Array.from({ length: 100 }, (_, i) => make('j' + (page === 'initial' ? i : i + 1))),
    };
  };
  h.api.job = async (id) => {
    reads.push(id);
    return make(id, 'cancelled');
  };
  await h.controller.refresh();
  assert.equal(h.controller.getState().jobs.length, 100);
  assert.equal(h.controller.getState().moreJobs, true);
  await h.controller.loadMoreJobs();
  assert.equal(h.controller.getState().jobs.length, 101);
  assert.equal(h.controller.getState().nextJobOffset, 102);
  page = 'shifted';
  await h.controller.refresh();
  assert.equal(h.controller.getState().jobs.length, 102);
  assert.deepEqual(reads, ['old']);
  assert.equal(h.controller.getState().jobs.find((j) => j.id === 'old').status, 'cancelled');
  assert.equal(h.controller.getState().moreJobs, false);
  assert.ok(offsets.includes(100));
  h.controller.leave();
  assert.equal(h.controller.getState().activity, null);
  assert.equal(h.controller.getState().nextJobOffset, 100);
  h.controller.destroy();
});

test('recovery-held generation preserves editing and other shots but rejects preflight and all resubmission paths', async () => {
  const h = await harness().start();
  await h.controller.prepare(h.shot);
  await h.controller.submit(h.shot);
  h.jobs[0].status = 'recovery_hold';
  await h.controller.refresh();
  const before = h.calls.filter((c) => c[0] === 'submit').length;
  h.store.updateEntity(h.shot, { description: '恢复期间继续编辑' });
  await assert.rejects(h.controller.prepare(h.shot), /恢复后待核对/);
  await assert.rejects(h.controller.submit(h.shot), /恢复后待核对/);
  await assert.rejects(h.controller.submitBatch([h.shot]), /恢复后待核对/);
  assert.equal(h.calls.filter((c) => c[0] === 'submit').length, before);
  const another = h.store.addEntity('shot', h.scene, { description: '另一镜仍可准备' });
  await h.controller.prepare(another);
  assert.equal(h.controller.getState().plans[another].status, 'ready');
  h.controller.destroy();
});

test('recovery-held rough cut cannot start or resubmit and does not remove the chapter draft', async () => {
  const h = await renderHarness();
  await h.controller.prepareRender(h.chapter, { resolution: '720P', aspect: '9:16' });
  await h.controller.submitRender(h.chapter);
  h.jobs[0].status = 'recovery_hold';
  await h.controller.refresh();
  h.store.updateEntity(h.shot, { data: { seconds: 3 } });
  await assert.rejects(
    h.controller.prepareRender(h.chapter, { resolution: '720P', aspect: '9:16' }),
    /恢复后待核对/,
  );
  await assert.rejects(h.controller.submitRender(h.chapter), /恢复后待核对/);
  assert.equal(h.store.getState().project.entities.find((e) => e.id === h.shot).data.seconds, 3);
  assert.equal(h.jobs.length, 1);
  h.controller.destroy();
});

test('project list loads bounded pages, retains history on refresh and never automatically loads all', async () => {
  const h = harness(),
    rows = Array.from({ length: 150 }, (_, i) => ({ id: 'p' + i, title: '项目' + i, version: 1 })),
    calls = [];
  h.api.projects = async ({ limit, offset }) => {
    calls.push({ limit, offset });
    return { projects: rows.slice(offset, offset + limit) };
  };
  await h.controller.login('superdan', 'test-only');
  assert.equal(calls.length, 1);
  assert.equal(h.controller.getState().projects.length, 100);
  assert.equal(h.controller.getState().moreProjects, true);
  rows.unshift({ id: 'new', title: '新项目', version: 1 });
  await h.controller.loadMoreProjects();
  assert.equal(h.controller.getState().projects.length, 150);
  assert.equal(h.controller.getState().moreProjects, false);
  assert.equal(calls[1].offset, 100);
  await h.controller.list();
  assert.equal(h.controller.getState().projects.length, 151);
  assert.equal(h.controller.getState().projects[0].id, 'new');
  assert.equal(h.controller.getState().projects.at(-1).id, 'p149');
  assert.equal(calls.length, 3);
  await h.controller.logout();
  assert.equal(h.controller.getState().projectsLoaded, false);
  assert.equal(h.controller.getState().projects.length, 0);
  h.controller.destroy();
});

test('batch history is explicitly paged by ten, deduplicated, retained on refresh and cleared on account departure', async () => {
  const h = await harness().start(),
    rows = Array.from({ length: 15 }, (_, i) => ({
      id: 'batch-' + i,
      status: 'completed',
      items: [],
      summary: true,
    })),
    calls = [];
  h.api.batches = async (id, { limit, offset }) => {
    calls.push({ limit, offset });
    return {
      batches: rows.slice(offset, offset + limit),
      has_more: offset + limit < rows.length,
      next_offset: offset + Math.min(limit, rows.length - offset),
    };
  };
  await h.controller.refresh();
  assert.equal(calls.length, 1);
  assert.equal(h.controller.getState().batches.length, 10);
  assert.equal(h.controller.getState().moreBatches, true);
  rows.unshift({ id: 'new-batch', status: 'active', items: [], summary: true });
  await h.controller.loadMoreBatches();
  assert.equal(h.controller.getState().batches.length, 15);
  assert.equal(h.controller.getState().moreBatches, false);
  assert.deepEqual(calls[1], { limit: 10, offset: 10 });
  await h.controller.refresh();
  assert.equal(h.controller.getState().batches.length, 16);
  assert.equal(h.controller.getState().batches[0].id, 'new-batch');
  assert.equal(h.controller.getState().moreBatches, false);
  h.controller.leave();
  assert.equal(h.controller.getState().batches.length, 0);
  assert.equal(h.controller.getState().batchesLoaded, false);
  assert.equal(h.controller.getState().nextBatchOffset, 10);
  h.controller.destroy();
});
test('account change clears prior private summaries even when new list retrieval fails', async () => {
  const h = harness();
  h.api.projects = async () => ({
    projects: [{ id: 'private', title: '仅旧账户可见', version: 1 }],
  });
  await h.controller.login('superdan', 'test-only');
  h.api.projects = async () => {
    throw Error('列表暂不可用');
  };
  await assert.rejects(h.controller.login('supervan', 'test-only'), /列表暂不可用/);
  assert.equal(h.controller.getState().account, 'supervan');
  assert.deepEqual(h.controller.getState().projects, []);
  assert.equal(h.controller.getState().projectsLoaded, false);
  assert.equal(h.controller.getState().capabilities, null);
  h.controller.destroy();
});
test('cloud operations queue quietly; only blocking ones mark the page busy and submits never queue', async () => {
  const h = harness();
  await h.controller.login('superdan', 'test-only');
  let resolve;
  h.api.projects = () => new Promise((r) => (resolve = r));
  const listing = h.controller.list();
  // A quiet read does not grey the page out.
  assert.equal(h.controller.getState().busy, false);
  // Another quiet operation waits its turn instead of failing.
  const second = h.controller.loadMoreProjects();
  // A submit while anything runs is refused, never queued into a second job.
  await assert.rejects(h.controller.submit('shot'), /上一项云操作还在处理中/);
  h.api.projects = async () => ({ projects: [] });
  resolve({ projects: [] });
  await listing;
  await second.catch(() => {});
  assert.equal(h.controller.getState().busy, false);
  h.controller.destroy();
});
test('late project page cannot reappear after context invalidation', async () => {
  const h = harness();
  await h.controller.login('superdan', 'test-only');
  let resolve;
  h.api.projects = () => new Promise((r) => (resolve = r));
  const pending = h.controller.list();
  h.controller.leave();
  resolve({ projects: [{ id: 'late', title: '迟到页面' }] });
  await assert.rejects(pending, /账户已经改变/);
  assert.equal(
    h.controller.getState().projects.some((p) => p.id === 'late'),
    false,
  );
  // Dropping a stale read is expected, not an error the person must dismiss.
  assert.equal(h.controller.getState().error, '');
  h.controller.destroy();
});
test('request timeout aborts once without retrying and project pagination uses explicit bounds', async () => {
  let attempts = 0;
  const client = createCloudClient({
    timeoutMs: 5,
    fetcher: async (path, { signal }) => {
      attempts++;
      return new Promise((_, reject) =>
        signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), {
          once: true,
        }),
      );
    },
  });
  await assert.rejects(
    client.submit('original-plan', 'original-key'),
    /Nothing will be automatically resubmitted/,
  );
  assert.equal(attempts, 1);
  let called;
  const good = createCloudClient({
    fetcher: async (path) => {
      called = path;
      return new Response('{"projects":[]}');
    },
  });
  await good.projects({ limit: 100, offset: 200 });
  assert.equal(called, '/v1/projects?limit=100&offset=200');
});

test('waiting capacity retains the original job and accepts cancellation without new model admission', async () => {
  const h = await harness().start();
  await h.controller.prepare(h.shot);
  await h.controller.submit(h.shot);
  h.jobs[0].status = 'waiting_capacity';
  await h.controller.refresh();
  await assert.rejects(h.controller.prepare(h.shot), /相同版本已在队列/);
  const count = h.calls.filter((c) => c[0] === 'submit').length;
  await h.controller.cancel('job1');
  assert.equal(h.jobs[0].status, 'cancelled');
  assert.equal(h.calls.filter((c) => c[0] === 'submit').length, count);
  h.controller.destroy();
});

test('session recheck changes owner without carrying project bodies and keeps separate same-ID drafts', async () => {
  const h = await harness().start(),
    id = h.store.getState().project.id;
  h.store.updateEntity(h.shot, { description: 'superdan 私有未同步正文' });
  h.api.me = async () => ({ username: 'supervan' });
  await h.controller.initialize();
  assert.equal(h.controller.getState().account, 'supervan');
  assert.equal(h.store.getState().workspace.mode, 'local');
  assert.equal(h.store.exportProject().title, '原本机作品');
  assert.equal(
    h.store.getCloudDraft('superdan', id).project.entities.find((e) => e.id === h.shot).description,
    'superdan 私有未同步正文',
  );
  assert.equal(h.store.getCloudDraft('supervan', id), null);
  h.remote.get(id).project.entities.find((e) => e.id === h.shot).description = 'supervan 独立正文';
  await h.controller.open(id);
  assert.equal(h.store.getState().workspace.account, 'supervan');
  assert.equal(
    h.store.getState().project.entities.find((e) => e.id === h.shot).description,
    'supervan 独立正文',
  );
  h.api.me = async () => {
    throw new CloudError('expired', { status: 401 });
  };
  await h.controller.recheckSession();
  assert.equal(h.controller.getState().account, null);
  assert.equal(h.store.getState().workspace.mode, 'local');
  assert.equal(
    h.store.getCloudDraft('supervan', id).project.entities.find((e) => e.id === h.shot).description,
    'supervan 独立正文',
  );
  h.controller.destroy();
});
test('late project, save and media responses cannot reinsert old-owner content or conflict in a new session', async () => {
  const h = await harness().start(),
    id = h.store.getState().project.id,
    record = structuredClone(h.remote.get(id));
  let resolveProject;
  h.api.project = () => new Promise((r) => (resolveProject = r));
  const opening = h.controller.open(id);
  h.api.me = async () => ({ username: 'supervan' });
  await h.controller.recheckSession();
  resolveProject(record);
  await assert.rejects(opening, /账户已经改变/);
  assert.equal(h.store.getState().workspace.mode, 'local');
  assert.equal(h.controller.getState().draftOffer, null);
  h.controller.destroy();
  const saved = await harness().start();
  let rejectSave;
  saved.api.saveProject = () => new Promise((_, reject) => (rejectSave = reject));
  const saving = saved.controller.save();
  saved.api.me = async () => ({ username: 'supervan' });
  await saved.controller.recheckSession();
  rejectSave(new CloudError('private conflict', { status: 409 }));
  await assert.rejects(saving, /账户或项目已经切换/);
  assert.equal(saved.controller.getState().conflict, null);
  saved.controller.destroy();
  const media = await harness().start();
  let resolveBlob;
  media.api.download = () => new Promise((resolve) => (resolveBlob = resolve));
  const pending = media.getHandlers().read('cloud_asset_old');
  media.api.me = async () => ({ username: 'supervan' });
  await media.controller.recheckSession();
  resolveBlob(new Blob(['old private media']));
  await assert.rejects(pending, /账户或项目已经切换/);
  media.controller.destroy();
});
test('all authenticated API operations carry expected owner and reject foreign response before exposing it', async () => {
  const calls = [],
    client = createCloudClient({
      fetcher: async (path, options) => {
        calls.push({ path, options });
        return new Response('{}', { headers: { 'X-Authenticated-Account': 'superdan' } });
      },
    });
  client.setAccount('superdan');
  await client.projects();
  await client.saveProject('same-id', {}, 1);
  await client.upload(
    new File(['x'], 'image.png', { type: 'image/png' }),
    'same-id',
    'asset',
    'upload-key',
  );
  await client.cancel('j');
  await client.batch('same-id', ['p'], 'batch-key');
  for (const call of calls) assert.equal(call.options.headers['X-Expected-Account'], 'superdan');
  let expired = 0;
  const foreign = createCloudClient({
    onUnauthorized: () => expired++,
    fetcher: async () =>
      new Response('{"private":"other owner"}', {
        headers: { 'X-Authenticated-Account': 'supervan' },
      }),
  });
  foreign.setAccount('superdan');
  await assert.rejects(foreign.projects(), /old response was not shown/);
  assert.equal(expired, 1);
  const mismatch = createCloudClient({
    onUnauthorized: () => expired++,
    fetcher: async () =>
      new Response('{"code":"account_context_changed","detail":"private title"}', { status: 409 }),
  });
  mismatch.setAccount('superdan');
  await assert.rejects(
    mismatch.projects(),
    (error) => error.status === 401 && !error.message.includes('private title'),
  );
  assert.equal(expired, 2);
});

test('late creation cannot open a same-ID project after account changes', async () => {
  const h = await harness().start();
  let resolve, payload;
  h.api.createProject = (project) => {
    payload = project;
    return new Promise((r) => (resolve = r));
  };
  const creating = h.controller.create('只属于原账户的新作');
  h.api.me = async () => ({ username: 'supervan' });
  await h.controller.recheckSession();
  resolve({ id: payload.id, project: payload, version: 1 });
  await assert.rejects(creating, /账户已经改变/);
  assert.equal(h.controller.getState().account, 'supervan');
  assert.equal(h.store.getState().workspace.mode, 'local');
  assert.equal(h.store.getState().project.title, '原本机作品');
  h.controller.destroy();
});

test('selected video trim requires server v2 capability and editing only its range invalidates an existing cut plan', async () => {
  const h = await renderHarness(),
    asset = h.store.getState().project.entities.find((e) => e.id === h.video),
    range = { ...videoBinding(asset), start: 1, end: 5 };
  h.store.updateEntity(h.shot, { data: { seconds: 3, selectedVideoRange: range } });
  await assert.rejects(
    h.controller.prepareRender(h.chapter, { resolution: '720P', aspect: '9:16' }),
    /尚不支持.*剪辑选段/,
  );
  assert.equal(h.calls.length, 0);
  h.api.capabilities = async () => ({
    ...capabilities,
    chapter_render: {
      implemented: true,
      enabled: true,
      source_trim: true,
      render_contract_version: 2,
    },
  });
  await h.controller.reloadCapabilities();
  await h.controller.prepareRender(h.chapter, { resolution: '720P', aspect: '9:16' });
  h.store.editProject((p) => {
    p.entities.find((e) => e.id === h.shot).data.selectedVideoRange.start = 2;
  });
  await assert.rejects(h.controller.submitRender(h.chapter), /重新预检/);
  assert.equal(
    h.calls.some((c) => c[0] === 'submitRender'),
    false,
  );
  h.controller.destroy();
});

test('syncing selected local media preserves nonzero trim while rebinding exactly the uploaded file', async () => {
  const h = harness(),
    id = h.store.addEntity('video', null, {
      data: {
        fileId: 'local-cut-video',
        fileName: 'cut.mp4',
        mime: 'video/mp4',
        metadata: { duration: 10 },
      },
    }),
    asset = h.store.getState().project.entities.find((e) => e.id === id),
    range = { ...videoBinding(asset), start: 2, end: 8 };
  h.store.updateEntity(h.shot, { data: { selectedAssetId: id, selectedVideoRange: range } });
  await h.start();
  await h.controller.syncAll();
  const p = h.store.exportProject(),
    shot = p.entities.find((e) => e.id === h.shot),
    uploaded = p.entities.find((e) => e.id === id);
  assert.equal(boundVideoRange(shot.data.selectedVideoRange, uploaded), true);
  assert.equal(shot.data.selectedVideoRange.cloudAssetId, 'asset1');
  assert.equal(shot.data.selectedVideoRange.start, 2);
  assert.equal(shot.data.selectedVideoRange.end, 8);
  assert.equal(h.calls.filter((c) => c[0] === 'upload').length, 1);
  h.controller.destroy();
});

test('linked generated audio requires server capability and blocks changed-source tracks before save or submit', async () => {
  const h = await renderHarness();
  h.store.updateEntity(h.video, { data: { sourceJobId: 'generated-job' } });
  h.store.addEntity('audio', null, {
    data: {
      fileId: 'cloud_artifact_generated-audio',
      cloudArtifactId: 'generated-audio',
      sourceJobId: 'generated-job',
      mime: 'audio/flac',
      metadata: { duration: 5 },
    },
  });
  h.store.editProject((p) => {
    upsertGeneratedSound(p, h.shot);
  });
  await assert.rejects(
    h.controller.prepareRender(h.chapter, { resolution: '720P', aspect: '9:16' }),
    /尚不支持同次生成声音/,
  );
  assert.equal(h.calls.length, 0);
  h.api.capabilities = async () => ({
    ...capabilities,
    chapter_render: {
      implemented: true,
      enabled: true,
      generated_audio: true,
      render_contract_version: 2,
    },
  });
  await h.controller.reloadCapabilities();
  await h.controller.prepareRender(h.chapter, { resolution: '720P', aspect: '9:16' });
  assert.equal(h.calls.at(-1)[0], 'renderPlan');
  h.store.updateEntity(h.shot, { data: { selectedAssetId: '' } });
  await assert.rejects(h.controller.submitRender(h.chapter), /重新预检/);
  h.calls.length = 0;
  await assert.rejects(
    h.controller.prepareRender(h.chapter, { resolution: '720P', aspect: '9:16' }),
    /更换或被删除/,
  );
  assert.equal(h.calls.length, 0);
  h.store.editProject((p) => {
    p.journey.soundTracks[h.chapter][0].muted = true;
  });
  await h.controller.prepareRender(h.chapter, { resolution: '720P', aspect: '9:16' });
  assert.equal(h.calls.at(-1)[0], 'renderPlan');
  h.controller.destroy();
});

test('subtitle burn-in fails closed before side effects, sends only a boolean and rechecks changed text before submission', async () => {
  const h = await renderHarness();
  h.store.editProject((p) => {
    p.journey.captionTracks = {
      [h.chapter]: { cues: [{ id: 'cue', start: 0, end: 2, text: '雨夜，明天见。' }] },
    };
    p.journey.captionTracks[h.chapter].confirmedSnapshot = captionSignature(p, h.chapter);
  });
  await assert.rejects(
    h.controller.prepareRender(h.chapter, {
      resolution: '720P',
      aspect: '9:16',
      burn_subtitles: true,
    }),
    /不支持.*字幕烧录/,
  );
  assert.equal(h.calls.length, 0);
  h.api.capabilities = async () => ({
    ...capabilities,
    chapter_render: {
      implemented: true,
      enabled: true,
      subtitles: true,
      render_contract_version: 3,
    },
  });
  await h.controller.reloadCapabilities();
  const original = h.api.renderPlan;
  h.api.renderPlan = async (payload) => ({
    ...(await original(payload)),
    timeline: {
      shots: [],
      audio_tracks: [],
      subtitles: { burned: true, preset: 'shortdrama-zh-v1', cues: [] },
    },
  });
  await h.controller.prepareRender(h.chapter, {
    resolution: '720P',
    aspect: '9:16',
    burn_subtitles: true,
  });
  const payload = h.calls.find((c) => c[0] === 'renderPlan')[1];
  assert.equal(payload.burn_subtitles, true);
  assert.equal(payload.cues, undefined);
  assert.equal(payload.font_path, undefined);
  h.store.editProject((p) => {
    p.journey.captionTracks[h.chapter].cues[0].text = '字幕已改';
  });
  await assert.rejects(h.controller.submitRender(h.chapter), /重新预检/);
  const calls = h.calls.length;
  await assert.rejects(
    h.controller.prepareRender(h.chapter, {
      resolution: '720P',
      aspect: '9:16',
      burn_subtitles: true,
    }),
    /确认/,
  );
  assert.equal(h.calls.length, calls);
  h.store.editProject((p) => {
    p.journey.captionTracks[h.chapter].confirmedSnapshot = captionSignature(p, h.chapter);
  });
  h.api.renderPlan = original;
  await assert.rejects(
    h.controller.prepareRender(h.chapter, {
      resolution: '720P',
      aspect: '9:16',
      burn_subtitles: true,
    }),
    /未确认字幕/,
  );
  assert.equal(
    h.calls.some((c) => c[0] === 'submitRender'),
    false,
  );
  h.controller.destroy();
});

test('subtitle edits do not invalidate explicitly subtitle-off plans, and original submit recovery remains frozen', async () => {
  const h = await renderHarness();
  await h.controller.prepareRender(h.chapter, {
    resolution: '720P',
    aspect: '9:16',
    burn_subtitles: false,
  });
  h.store.editProject((p) => {
    p.journey.captionTracks = {
      [h.chapter]: { cues: [{ id: 'cue', start: 0, end: 1, text: '仍是草稿' }] },
    };
  });
  await h.controller.submitRender(h.chapter);
  assert.equal(h.calls.find((c) => c[0] === 'renderPlan')[1].burn_subtitles, undefined);
  assert.equal(h.jobs.length, 1);
  h.controller.destroy();
});

test('draft roughcut choices survive repair navigation without saving or submitting and clear when leaving the project', async () => {
  const h = await renderHarness(),
    options = { resolution: '480P', aspect: '9:16', burn_subtitles: true };
  h.controller.setRenderOptions(h.chapter, options);
  assert.deepEqual(h.controller.getState().renderOptions[h.chapter], options);
  assert.equal(h.calls.length, 0);
  h.store.setSection('journey');
  h.store.updateEntity(h.shot, { description: '回到声音步骤修正字幕' });
  assert.equal(h.controller.getState().renderOptions[h.chapter].burn_subtitles, true);
  assert.equal(h.calls.length, 0);
  h.controller.leave();
  assert.deepEqual(h.controller.getState().renderOptions, {});
  h.controller.destroy();
});

test('project activity read rejects late same-project workspace and account responses without editing drafts', async () => {
  const h = await harness().start();
  let finish;
  h.api.projectActivity = () => new Promise((resolve) => (finish = resolve));
  const before = h.store.exportProject(),
    pending = h.controller.readProjectActivity(),
    rejected = assert.rejects(pending, /账户或项目已经切换/);
  h.store.enterCloudProject(before, { account: 'superdan', version: 1 });
  finish({ items: [{ id: 'private' }] });
  await rejected;
  assert.deepEqual(h.store.exportProject(), before);
  const pending2 = h.controller.readProjectActivity(),
    rejected2 = assert.rejects(pending2, /账户或项目已经切换/);
  await h.controller.logout();
  finish({ items: [{ id: 'private' }] });
  await rejected2;
  h.controller.destroy();
});

const automaticRecipe = {
  ...recipe,
  controls: {
    ...schema,
    encoder_device: { type: 'string', enum: ['default', 'cpu'], default: 'default' },
    video_decode: { type: 'string', enum: ['normal', 'tiled'], default: 'normal' },
  },
  deployment_preset: {
    applies_to: 'unset_controls_only',
    controls: { encoder_device: 'cpu', video_decode: 'tiled' },
  },
  execution_support: {
    constraints: { controls: { encoder_device: ['cpu'], video_decode: ['tiled'] } },
  },
};
test('preflight refreshes disabled cached service and persists pool settings without changing creative inputs', async () => {
  const h = await harness({
    capabilities: async () => ({
      ...capabilities,
      execution_enabled: false,
      recipes: [automaticRecipe],
    }),
  }).start();
  h.store.updateEntity(h.shot, {
    data: {
      h3: {
        controls: {
          encoder_device: 'default',
          video_decode: 'normal',
          duration: 4,
          resolution: '512P',
          seed: '42',
        },
      },
    },
  });
  h.api.capabilities = async () => ({ ...capabilities, recipes: [automaticRecipe] });
  await h.controller.prepare(h.shot);
  assert.equal(h.controller.getState().capabilities.execution_enabled, true);
  const saved = h.remote
    .get(h.store.getState().project.id)
    .project.entities.find((e) => e.id === h.shot).data.h3.controls;
  assert.deepEqual(saved, {
    encoder_device: 'cpu',
    video_decode: 'tiled',
    duration: 4,
    resolution: '512P',
    seed: '42',
  });
  assert.equal(h.calls.find((c) => c[0] === 'plan')[1].controls.video_decode, 'tiled');
  assert.equal(
    h.calls.some((c) => c[0] === 'submit'),
    false,
  );
  await h.controller.submit(h.shot);
  assert.equal(h.jobs.length, 1);
  h.controller.destroy();
});
test('failed capability refresh preserves draft and makes no plan or paid submission', async () => {
  const h = await harness().start(),
    before = h.store.exportProject();
  h.api.capabilities = async () => {
    throw Error('offline');
  };
  await assert.rejects(h.controller.prepare(h.shot), /offline/);
  assert.equal(h.controller.getState().capabilities, null);
  assert.match(h.controller.getState().capabilityError, /暂未核对/);
  assert.deepEqual(h.store.exportProject(), before);
  assert.equal(h.calls.length, 0);
  h.controller.destroy();
});
test('submit rechecks service disabled after successful preflight without making a job', async () => {
  const h = await harness().start();
  await h.controller.prepare(h.shot);
  h.api.capabilities = async () => ({ ...capabilities, execution_enabled: false });
  await assert.rejects(h.controller.submit(h.shot), /未启用/);
  assert.equal(h.jobs.length, 0);
  h.controller.destroy();
});
test('automatic operational settings require matching single-choice execution evidence', () => {
  const saved = { encoder_device: 'default', video_decode: 'normal', duration: 9 };
  const multiple = {
    ...automaticRecipe,
    execution_support: {
      constraints: {
        controls: { encoder_device: ['cpu', 'default'], video_decode: ['normal', 'tiled'] },
      },
    },
  };
  assert.equal(controlsForRecipe(multiple, saved).video_decode, 'normal');
  assert.equal(controlsForRecipe(automaticRecipe, saved).duration, 9);
  assert.equal(
    controlsForRecipe(
      {
        ...automaticRecipe,
        deployment_preset: {
          ...automaticRecipe.deployment_preset,
          controls: { encoder_device: 'other', video_decode: 'other' },
        },
      },
      saved,
    ).video_decode,
    'normal',
  );
});

test('accepted project trash refresh explicitly replaces visible page while normal refresh keeps loaded history', async () => {
  const h = harness();
  await h.controller.login('superdan', 'test-only');
  h.api.projects = async () => ({
    projects: [
      { id: 'kept', title: 'Kept', version: 1 },
      { id: 'gone', title: 'Gone', version: 1 },
    ],
  });
  await h.controller.list({ replace: true });
  h.api.projects = async () => ({ projects: [{ id: 'kept', title: 'Kept', version: 1 }] });
  await h.controller.list();
  assert.equal(
    h.controller.getState().projects.some((p) => p.id === 'gone'),
    true,
  );
  await h.controller.list({ replace: true });
  assert.deepEqual(
    h.controller.getState().projects.map((p) => p.id),
    ['kept'],
  );
  assert.equal(h.controller.getState().nextProjectOffset, 1);
  assert.equal(h.controller.getState().moreProjects, false);
  h.controller.destroy();
});

test('sub-cent display preserves less-than ordering and one currency sign in both locales', () => {
  for (const locale of ['en', 'zh'])
    for (const kind of ['budget_reservation', 'estimate']) {
      const value = estimateLabel({ cost_microusd: 1, currency: 'USD', kind }, locale);
      assert.match(value, /<\$0\.01 USD/);
      assert.doesNotMatch(value, /\$</);
    }
});

test('cloud system notices follow locale without translating user text or diagnostics', () => {
  assert.equal(
    cloudSystemMessage('云项目已打开；原本机作品保留。', 'en'),
    'Story opened. Your local work is kept.',
  );
  assert.equal(cloudSystemMessage('已恢复没同步的草稿。', 'zh'), '已恢复没同步的草稿。');
  assert.equal(cloudSystemMessage('我的故事：夜雨', 'en'), '我的故事：夜雨');
  assert.equal(
    cloudSystemMessage('HTTP 429 · provider detail', 'en'),
    'HTTP 429 · provider detail',
  );
});

test('history receipt notices use the active language for pending blocked and every completed action', () => {
  const expected = {
    trash: 'Moved to trash.',
    restore: 'Creation restored.',
    purge: 'Permanently deleted.',
    empty_trash: 'Trash emptied.',
  };
  for (const [action, english] of Object.entries(expected)) {
    const notice = HISTORY_ACTIONS[action] + '已完成，任务和计费账本保留。';
    assert.equal(
      cloudSystemMessage(notice, 'en'),
      english + ' Task and spending records are retained.',
    );
    assert.match(cloudSystemMessage(notice, 'zh'), /任务和计费账本保留/);
    const authored = 'My prompt quotes: ' + notice;
    assert.equal(cloudSystemMessage(authored, 'en'), authored);
  }
  assert.equal(
    cloudSystemMessage('永久删除已受理，仍在处理中；请核对原清理状态。', 'en'),
    'Permanent deletion accepted; still processing. Check the original cleanup status.',
  );
  assert.equal(
    cloudSystemMessage('原清理操作已核对，受阻记录仍保留。', 'en'),
    'Original cleanup checked; blocked items are retained.',
  );
});

test('a server-side result binding rebases the tab: clean adopts, separate edits merge, overlap conflicts', () => {
  const setup = () => {
    const { store, shot } = fixture(),
      cloud = copyForCloud(store.exportProject());
    assert.equal(store.enterCloudProject(cloud, { account: 'superdan', version: 1 }), true);
    const remote = structuredClone(store.exportProject());
    // The server binds a result: one new entity plus a selection on the shot.
    remote.entities.find((e) => e.id === shot).data.selectedAssetId = 'result-x';
    remote.entities.push({
      ...structuredClone(remote.entities.find((e) => e.id === shot)),
      id: 'result-x',
      type: 'video',
      parentId: null,
      title: 'v1',
      data: { cloudArtifactId: 'x', fileId: 'cloud_artifact_x' },
    });
    return { store, shot, remote };
  };
  // Clean tab: adopt the server version as saved.
  let { store, shot, remote } = setup();
  remote.entities = remote.entities.filter((e) => e.type !== 'video' || e.id !== 'result-x');
  delete remote.entities.find((e) => e.id === shot).data.selectedAssetId;
  remote.title = 'Renamed on the server';
  assert.equal(store.rebaseCloud(remote, { account: 'superdan', version: 2 }), true);
  assert.equal(store.getState().workspace.serverVersion, 2);
  assert.equal(store.getState().workspace.dirty, false);
  assert.equal(store.exportProject().title, 'Renamed on the server');
  // Dirty tab, different field: both survive and the tab stays dirty against v2.
  ({ store, shot, remote } = setup());
  remote.title = 'Renamed on the server';
  store.updateEntity(shot, { description: 'Local edit' });
  assert.equal(store.rebaseCloud(remote, { account: 'superdan', version: 2 }), true);
  const merged = store.exportProject();
  assert.equal(merged.title, 'Renamed on the server');
  assert.equal(merged.entities.find((e) => e.id === shot).description, 'Local edit');
  assert.equal(store.getState().workspace.serverVersion, 2);
  assert.equal(store.getState().workspace.dirty, true);
  // Both sides changed the same shot differently: refuse, keep the draft untouched.
  ({ store, shot, remote } = setup());
  remote.entities.find((e) => e.id === shot).description = 'Server edit';
  store.updateEntity(shot, { description: 'Local edit' });
  const before = store.exportProject();
  assert.equal(store.rebaseCloud(remote, { account: 'superdan', version: 2 }), false);
  assert.deepEqual(store.exportProject(), before);
  assert.equal(store.getState().workspace.serverVersion, 1);
  // Never step backwards or accept another account.
  assert.equal(store.rebaseCloud(remote, { account: 'superdan', version: 1 }), false);
  assert.equal(store.rebaseCloud(remote, { account: 'supervan', version: 3 }), false);
});
