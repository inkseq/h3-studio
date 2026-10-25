import test from 'node:test';
import assert from 'node:assert/strict';
import seed from './legacy-seed.json' with { type: 'json' };
import {
  createStore,
  migrateLegacy,
  validateProject,
  validateLink,
  readiness,
  children,
  ancestors,
} from './store.js';
const KEY = 'yingxu-studio-v4';
test('editing an already selected shot requires review while renames and review notes preserve its decision', () => {
  const { s } = fresh(),
    { shot } = story(s);
  s.updateEntity(shot, {
    status: 'draft',
    data: { uxReview: { chosen: 'A', status: 'selected', note: '保留' } },
  });
  s.updateEntity(shot, { title: '改名' });
  assert.equal(s.getState().project.entities.find((e) => e.id === shot).status, 'draft');
  s.updateEntity(shot, { description: '改成背面进入' });
  assert.equal(s.getState().project.entities.find((e) => e.id === shot).status, 'review');
  assert.equal(s.getState().project.entities.find((e) => e.id === shot).data.uxReview.chosen, 'A');
  s.updateEntity(shot, { status: 'draft' });
  s.updateEntity(shot, { data: { seconds: 12 } });
  assert.equal(s.getState().project.entities.find((e) => e.id === shot).status, 'review');
  s.updateEntity(shot, {
    status: 'draft',
    data: { uxReview: { chosen: 'B', status: 'selected' } },
  });
  assert.equal(s.getState().project.entities.find((e) => e.id === shot).status, 'draft');
});
test('multi-entity edits are atomic, validated, undoable and exception safe', () => {
  const { s } = fresh();
  const { scene, shot } = story(s);
  const before = s.exportProject();
  assert.equal(
    s.editProject((p) => {
      p.entities.find((e) => e.id === scene).title = '雨夜';
      p.entities.find((e) => e.id === shot).status = 'review';
    }),
    true,
  );
  assert.equal(s.getState().project.entities.find((e) => e.id === scene).title, '雨夜');
  s.undo();
  assert.deepEqual(s.exportProject(), before);
  assert.equal(
    s.editProject((p) => {
      p.entities[0].parentId = 'missing';
    }),
    false,
  );
  assert.deepEqual(s.exportProject(), before);
  assert.equal(
    s.editProject((p) => {
      p.title = 'never';
      throw Error('保留原文');
    }),
    false,
  );
  assert.match(s.getState().notice, /保留原文/);
  assert.deepEqual(s.exportProject(), before);
  assert.equal(
    s.editProject((p) => {
      p.title = 'discard';
      return false;
    }),
    false,
  );
  assert.deepEqual(s.exportProject(), before);
});
test('creation journey persists with shared entity IDs, survives export and keeps selection', () => {
  const { s, storage } = fresh();
  const { shot } = story(s);
  s.select(shot);
  s.setSection('journey');
  s.updateProject({
    journey: {
      stage: 5,
      brief: { idea: '一个雨夜的故事', script: '第一场\n她走进房间。' },
      sound: { mode: 'silent' },
    },
  });
  s.updateEntity(shot, {
    data: { uxReview: { selected: 'A', note: '保留人物，改慢动作', status: 'failed' } },
  });
  const reloaded = createStore({ storage }),
    p = reloaded.exportProject();
  assert.equal(reloaded.getState().section, 'journey');
  assert.equal(reloaded.getState().selectedId, shot);
  assert.equal(p.journey.stage, 5);
  assert.equal(p.journey.brief.script, '第一场\n她走进房间。');
  assert.equal(p.journey.sound.mode, 'silent');
  assert.equal(p.entities.find((e) => e.id === shot).data.uxReview.selected, 'A');
  assert.equal(p.entities.length, 3);
  assert.equal(p.jobs.length, 0);
  assert.equal(validateProject(p).ok, true);
  const before = s.exportProject();
  assert.equal(s.updateProject({ journey: { stage: 8 } }), false);
  assert.deepEqual(s.exportProject(), before);
});
function memory(initial = {}) {
  const map = new Map(Object.entries(initial));
  return {
    map,
    getItem: (key) => map.get(key) ?? null,
    setItem: (key, value) => map.set(key, value),
  };
}
function fresh(options = {}) {
  const storage = options.storage || memory(),
    s = createStore({ ...options, storage });
  assert.equal(s.newProject('空白项目'), true);
  return { s, storage };
}
function story(s) {
  const chapter = s.addEntity('chapter', null, { title: '第一章' }),
    scene = s.addEntity('scene', chapter, { title: '第一次见面' }),
    shot = s.addEntity('shot', scene, { title: '进入门厅', description: '林岚推门进入车站。' });
  return { chapter, scene, shot };
}
test('legacy migration preserves script, stable ids, hierarchy and references', () => {
  const p = migrateLegacy(seed);
  assert.equal(validateProject(p).ok, true);
  const chapter = p.entities.find((e) => e.id === 'ep-01');
  assert.equal(chapter.type, 'chapter');
  const scene = p.entities.find((e) => e.id === 'sc-01');
  assert.equal(scene.data.script, seed.episodes[0].scenes[0].script);
  const shots = children(p, scene.id);
  assert.equal(shots.length, 3);
  assert.deepEqual(
    ancestors(p, shots[0].id).map((e) => e.id),
    ['ep-01', 'sc-01'],
  );
  assert.ok(p.links.some((l) => l.source === 'char-lin' && l.target === shots[0].id));
});
test('startup migrates legacy local data, keeps original, reload preserves UI selection', () => {
  const raw = JSON.stringify({ ...seed, title: '我写的短剧' }),
    storage = memory({ 'yingxu-series-project-v1': raw }),
    s = createStore({ storage });
  assert.equal(s.getState().project.title, '我写的短剧');
  assert.equal(storage.getItem('yingxu-series-project-v1'), raw);
  s.setMode('canvas');
  s.select('sc-01');
  s.setScope('ep-01');
  const again = createStore({ storage });
  assert.equal(again.getState().mode, 'canvas');
  assert.equal(again.getState().selectedId, 'sc-01');
  assert.equal(again.getState().scopeId, 'ep-01');
});
test('blank project has no demo entities and archives previous project before switching', () => {
  const storage = memory(),
    s = createStore({ storage }),
    old = s.exportProject();
  assert.ok(old.entities.length);
  s.newProject('我的作品');
  assert.deepEqual(s.getState().project.entities, []);
  assert.ok(
    [...storage.map.entries()].some(
      ([k, v]) => k.includes('-backup-') && JSON.parse(v).id === old.id,
    ),
  );
});
test('snapshots deeply immutable, exported data is separate, subscribers unsubscribe', () => {
  const { s } = fresh();
  const id = s.addEntity('note');
  assert.throws(() => {
    s.getState().project.entities[0].title = '篡改';
  }, TypeError);
  const exported = s.exportProject();
  exported.entities[0].title = '外部';
  assert.notEqual(s.getState().project.entities[0].title, '外部');
  let n = 0;
  const off = s.subscribe(() => n++);
  s.select(id);
  assert.equal(n, 1);
  off();
  s.select(null);
  assert.equal(n, 1);
});
test('typing coalesces undo and edits retain last confirmed version', () => {
  let clock = 100;
  const { s } = fresh({ now: () => clock }),
    id = s.addEntity('note', null, { title: '初始' }),
    base = s.getState().undoCount;
  s.updateEntity(id, { title: '第一次' }, { coalesceKey: id + ':title' });
  clock += 200;
  s.updateEntity(id, { title: '第二次' }, { coalesceKey: id + ':title' });
  assert.equal(s.getState().undoCount, base + 1);
  assert.equal(s.getState().project.entities[0].version, 1);
  s.undo();
  assert.equal(s.getState().project.entities[0].title, '初始');
  s.redo();
  assert.equal(s.getState().project.entities[0].title, '第二次');
  s.updateEntity(id, { status: 'ready', version: 2 });
  s.updateEntity(id, { title: '第三次' });
  assert.equal(s.getState().project.entities[0].version, 2);
  assert.equal(s.getState().project.entities[0].status, 'draft');
});
test('deep duplicate creates fresh IDs and remaps internal edges, retains incoming shared references', () => {
  const { s } = fresh(),
    { chapter, scene, shot } = story(s),
    character = s.addEntity('character', null, { title: '林岚' }),
    gen = s.addEntity('generation', scene);
  s.addLink(character, shot, 'identity');
  s.addLink(shot, gen, 'dependency');
  const before = s.exportProject(),
    copy = s.duplicateEntity(chapter),
    after = s.exportProject(),
    copyScene = children(after, copy)[0],
    copyShot = children(after, copyScene.id).find((e) => e.type === 'shot'),
    copyGen = children(after, copyScene.id).find((e) => e.type === 'generation');
  assert.notEqual(copyScene.id, scene);
  assert.notEqual(copyShot.id, shot);
  assert.ok(after.links.some((l) => l.source === character && l.target === copyShot.id));
  assert.ok(after.links.some((l) => l.source === copyShot.id && l.target === copyGen.id));
  assert.equal(after.entities.filter((e) => e.type === 'character').length, 1);
  assert.ok(before.entities.every((e) => after.entities.some((x) => x.id === e.id)));
});
test('delete cascades children and links, preserves shared assets; undo restores all', () => {
  const { s } = fresh(),
    { chapter, scene, shot } = story(s),
    character = s.addEntity('character');
  s.addLink(character, shot, 'identity');
  const before = s.exportProject();
  s.deleteEntity(chapter);
  assert.deepEqual(
    s.getState().project.entities.map((e) => e.id),
    [character],
  );
  assert.equal(s.getState().project.links.length, 0);
  assert.equal(s.getState().project.layout.positions[scene], undefined);
  s.undo();
  assert.deepEqual(s.exportProject(), before);
});
test('reorder changes only sibling order, not canvas position or ids', () => {
  const { s } = fresh(),
    a = s.addEntity('chapter', null, { title: 'A' }),
    b = s.addEntity('chapter', null, { title: 'B' });
  const xy = s.getState().project.layout.positions[b];
  s.reorderEntity(b, -1);
  assert.deepEqual(
    children(s.getState().project, null).map((e) => e.id),
    [b, a],
  );
  assert.deepEqual(s.getState().project.layout.positions[b], xy);
  s.setPositions({ [a]: { x: 999, y: 444 } });
  assert.deepEqual(
    children(s.getState().project, null).map((e) => e.id),
    [b, a],
  );
});
test('reference types and video-specific roles reject wrong models and endpoints', () => {
  const { s } = fresh(),
    { shot } = story(s),
    img = s.addEntity('image'),
    audio = s.addEntity('audio'),
    motion = s.addEntity('video'),
    loc = s.addEntity('location'),
    music = s.addEntity('generation', null, { data: { recipe: 'music' } });
  assert.equal(s.addLink(img, shot, 'identity').ok, true);
  assert.equal(s.addLink(img, shot, 'firstFrame').ok, true);
  assert.equal(s.addLink(audio, shot, 'motion').ok, false);
  assert.equal(s.addLink(motion, music, 'motion').ok, false);
  assert.equal(s.addLink(loc, shot, 'location').ok, true);
  assert.equal(s.addLink(img, loc, 'reference').ok, false);
  assert.equal(s.addLink(img, shot, 'firstFrame').ok, false);
});
test('execution cycles rejected and import detects cycles regardless of order', () => {
  const { s } = fresh(),
    a = s.addEntity('generation'),
    b = s.addEntity('generation'),
    c = s.addEntity('generation');
  assert.equal(s.addLink(a, b, 'dependency').ok, true);
  assert.equal(s.addLink(b, c, 'dependency').ok, true);
  assert.equal(s.addLink(c, a, 'dependency').ok, false);
  const p = s.exportProject();
  p.links.push({ id: 'cycle', source: c, target: a, role: 'dependency' });
  assert.match(validateProject(p).error, /循环/);
});
test('updating or removing reference marks only downstream work for review', () => {
  const { s } = fresh(),
    { shot } = story(s),
    character = s.addEntity('character'),
    unrelated = s.addEntity('generation');
  s.addLink(character, shot, 'identity');
  s.updateEntity(shot, { status: 'ready' });
  s.updateEntity(character, { description: '新雨衣' });
  assert.equal(s.getState().project.entities.find((e) => e.id === shot).status, 'review');
  assert.equal(s.getState().project.entities.find((e) => e.id === unrelated).status, 'draft');
  assert.equal(s.getState().project.jobs.length, 0);
});
test('readiness never claims backend availability or paid task execution', () => {
  const { s } = fresh(),
    { shot } = story(s),
    gen = s.addEntity('generation', null, {
      description: '完整描述',
      data: { recipe: 'video', seconds: 6 },
    });
  assert.ok(readiness(s.getState().project, shot).some((x) => x.includes('尚未接入')));
  assert.ok(readiness(s.getState().project, gen).some((x) => x.includes('尚未接入')));
  assert.deepEqual(s.getState().project.jobs, []);
});
test('missing media flags block linked references and selected candidates despite a retained file ID', () => {
  const { s } = fresh(),
    { shot } = story(s),
    asset = s.addEntity('image', null, {
      title: '被清理的图片',
      data: { fileId: 'lost-file', missingFile: true },
    });
  s.addLink(asset, shot, 'firstFrame');
  s.updateEntity(shot, { data: { selectedAssetId: asset } });
  const problems = readiness(s.getState().project, shot);
  assert.ok(problems.some((x) => x.includes('参考素材「被清理的图片」的文件找不到了')));
  assert.ok(problems.some((x) => x.includes('选的「被清理的图片」文件找不到了')));
  assert.ok(readiness(s.getState().project, asset).some((x) => x.includes('素材文件找不到了')));
});
test('selected candidate without a reference edge is checked and becomes available after reupload', () => {
  const { s } = fresh(),
    { shot } = story(s),
    asset = s.addEntity('video', null, { title: '缺失候选', data: { missingFile: true } });
  s.updateEntity(shot, { data: { selectedAssetId: asset } });
  assert.ok(
    readiness(s.getState().project, shot).some((x) => x.includes('选的「缺失候选」文件找不到了')),
  );
  s.updateEntity(asset, { data: { fileId: 'replacement-file', missingFile: false } });
  assert.equal(
    readiness(s.getState().project, shot).some((x) => x.includes('文件找不到了')),
    false,
  );
  assert.equal(
    s.getState().project.entities.find((e) => e.id === shot).data.selectedAssetId,
    asset,
  );
});
test('malformed import refuses structural corruption atomically', () => {
  const { s } = fresh();
  story(s);
  const good = s.exportProject(),
    mutations = [
      (p) => (p.entities[0].type = 'evil'),
      (p) => (p.entities[1].parentId = 'missing'),
      (p) => (p.entities[2].parentId = p.entities[0].id),
      (p) => (p.entities[2].data.seconds = NaN),
      (p) => (p.entities[0].order = Infinity),
      (p) => (p.entities[0].title = 'x'.repeat(161)),
      (p) => p.entities.push({ ...p.entities[0] }),
      (p) => (p.layout.positions.unknown = { x: 0, y: 0 }),
      (p) => (p.layout.viewport.zoom = 0),
      (p) =>
        p.links.push({ id: 'bad', source: 'unknown', target: p.entities[2].id, role: 'reference' }),
      (p) => (p.entities[0].parentId = p.entities[2].id),
    ];
  for (const mutate of mutations) {
    const bad = structuredClone(good);
    mutate(bad);
    assert.equal(validateProject(bad).ok, false);
    assert.equal(s.replaceProject(bad), false);
    assert.deepEqual(s.exportProject(), good);
  }
});
test('prototype pollution keys and unbounded nesting rejected', () => {
  const { s } = fresh(),
    p = s.exportProject();
  p.extra = JSON.parse('{"__proto__":{"polluted":true}}');
  assert.equal(validateProject(p).ok, false);
  assert.equal({}.polluted, undefined);
});
test('save failures are visible, remain in memory, and block destructive replace', () => {
  const storage = memory(),
    s = createStore({ storage }),
    before = s.exportProject();
  storage.setItem = () => {
    throw Error('quota');
  };
  const id = s.addEntity('note', null, { title: '尚未保存' });
  assert.ok(id);
  assert.equal(s.getState().saveState, '保存失败，请导出备份');
  const current = s.exportProject();
  assert.equal(s.newProject('不应覆盖'), false);
  assert.deepEqual(s.exportProject(), current);
  assert.ok(s.getState().notice.includes('备份'));
  assert.notDeepEqual(current, before);
});
test('corrupt saved raw opens an empty recovery workspace and retains original bytes when backup quota is full', () => {
  const raw = '{broken',
    storage = memory({ [KEY]: raw });
  storage.setItem = () => {
    throw Error('quota');
  };
  const s = createStore({ storage });
  assert.equal(s.getState().project.title, '待恢复的项目');
  assert.deepEqual(s.getState().project.entities, []);
  assert.equal(s.getRecoveryRaw(), raw);
  assert.equal(storage.getItem(KEY), raw);
  assert.equal(s.getState().saveState, '保存失败，请导出备份');
});
test('invalid new hierarchy never mutates project, image and music recipes allowed', () => {
  const { s } = fresh();
  assert.equal(s.addEntity('shot'), false);
  assert.equal(s.addEntity('scene'), false);
  assert.equal(s.getState().project.entities.length, 0);
  for (const recipe of ['video', 'image', 'music']) {
    const id = s.addEntity('generation', null, { data: { recipe } });
    assert.ok(id);
  }
  assert.equal(s.addEntity('generation', null, { data: { recipe: 'unknown' } }), false);
});
test('undo/redo branch clears redo and IDs are never reused', () => {
  const { s } = fresh(),
    a = s.addEntity('note');
  s.undo();
  assert.equal(s.getState().redoCount, 1);
  const b = s.addEntity('note');
  assert.notEqual(a, b);
  assert.equal(s.getState().redoCount, 0);
  assert.equal(s.redo(), false);
});
test('controlled input can clear a draft title, persist it and import its exported draft', () => {
  const { s, storage } = fresh(),
    id = s.addEntity('note', null, { title: '可以清空' });
  assert.equal(s.updateEntity(id, { title: '' }, { coalesceKey: id + ':title' }), true);
  assert.equal(s.getState().project.entities[0].title, '');
  assert.equal(validateProject(s.exportProject()).ok, true);
  assert.equal(createStore({ storage }).getState().project.entities[0].title, '');
  s.updateEntity(id, { title: '新标题' }, { coalesceKey: id + ':title' });
  assert.equal(s.getState().project.entities[0].title, '新标题');
});
test('deleting selected candidate clears only affected shots, review and undo restore', () => {
  const { s } = fresh(),
    { shot } = story(s),
    asset = s.addEntity('video'),
    other = s.addEntity('image');
  s.updateEntity(shot, { data: { selectedAssetId: asset }, status: 'ready' });
  const before = s.exportProject();
  assert.equal(s.deleteEntity(asset), true);
  const edited = s.getState().project.entities.find((e) => e.id === shot);
  assert.equal(edited.data.selectedAssetId, '');
  assert.equal(edited.status, 'review');
  assert.ok(s.getState().project.entities.some((e) => e.id === other));
  assert.equal(validateProject(s.exportProject()).ok, true);
  s.undo();
  assert.deepEqual(s.exportProject(), before);
});
test('duplicating subtree remaps internal selected candidate but retains shared selection', () => {
  const { s } = fresh(),
    { chapter, scene, shot } = story(s),
    internal = s.addEntity('image', scene),
    shared = s.addEntity('video');
  s.updateEntity(shot, { data: { selectedAssetId: internal } });
  const copy = s.duplicateEntity(chapter),
    p = s.exportProject(),
    copyScene = children(p, copy)[0],
    copyShot = children(p, copyScene.id).find((e) => e.type === 'shot'),
    copyImage = children(p, copyScene.id).find((e) => e.type === 'image');
  assert.equal(copyShot.data.selectedAssetId, copyImage.id);
  s.updateEntity(shot, { data: { selectedAssetId: shared } });
  const copy2 = s.duplicateEntity(chapter),
    p2 = s.exportProject(),
    scene2 = children(p2, copy2)[0];
  assert.equal(children(p2, scene2.id).find((e) => e.type === 'shot').data.selectedAssetId, shared);
});
test('missing selected candidate import rejected with guidance', () => {
  const { s } = fresh(),
    { shot } = story(s),
    p = s.exportProject();
  p.entities.find((e) => e.id === shot).data.selectedAssetId = 'missing';
  assert.match(validateProject(p).error, /候选素材不存在/);
});
test('changing recipe with incompatible input fails atomically and explains recovery', () => {
  const { s } = fresh(),
    image = s.addEntity('image'),
    gen = s.addEntity('generation');
  s.addLink(image, gen, 'firstFrame');
  const before = s.exportProject();
  assert.equal(s.updateEntity(gen, { data: { recipe: 'music' } }), false);
  assert.deepEqual(s.exportProject(), before);
  assert.match(s.getState().notice, /先解除不兼容/);
  s.removeLink(s.getState().project.links[0].id);
  assert.equal(s.updateEntity(gen, { data: { recipe: 'music' } }), true);
});
test('corrupt raw stays untouched even when recovery backup succeeds until explicit replacement', () => {
  const raw = '{broken',
    storage = memory({ [KEY]: raw }),
    s = createStore({ storage });
  assert.equal(storage.getItem(KEY), raw);
  s.addEntity('note');
  assert.equal(storage.getItem(KEY), raw);
  assert.ok(
    [...storage.map].some(([key, value]) => key.includes('-backup-recovery-') && value === raw),
  );
  assert.equal(s.newProject('明确新建'), true);
  assert.equal(JSON.parse(storage.getItem(KEY)).project.title, '明确新建');
});
test('scene script and selected candidate revisions propagate review to child shots and dependent steps', () => {
  const { s } = fresh(),
    { scene, shot } = story(s),
    candidate = s.addEntity('video'),
    gen = s.addEntity('generation');
  s.addLink(shot, gen, 'dependency');
  s.updateEntity(shot, { data: { selectedAssetId: candidate }, status: 'ready' });
  s.updateEntity(gen, { status: 'ready' });
  s.updateEntity(scene, { data: { script: '换成夜晚' } });
  assert.equal(s.getState().project.entities.find((e) => e.id === shot).status, 'review');
  assert.equal(s.getState().project.entities.find((e) => e.id === gen).status, 'review');
  s.updateEntity(shot, { status: 'ready' });
  s.updateEntity(gen, { status: 'ready' });
  s.updateEntity(candidate, { description: '替换的素材' });
  assert.equal(s.getState().project.entities.find((e) => e.id === shot).status, 'review');
  assert.equal(s.getState().project.entities.find((e) => e.id === gen).status, 'review');
});
test('copies start as unconfirmed drafts rather than inheriting stale confirmed version metadata', () => {
  const { s } = fresh(),
    id = s.addEntity('note', null, { title: '原版' });
  s.updateEntity(id, {
    status: 'ready',
    version: 2,
    data: { confirmed: { title: '原版', description: '', script: '' } },
  });
  const copied = s.duplicateEntity(id),
    copy = s.getState().project.entities.find((e) => e.id === copied);
  assert.equal(copy.version, 1);
  assert.equal(copy.status, 'draft');
  assert.equal(copy.data.confirmed, undefined);
  assert.ok(s.getState().project.entities.find((e) => e.id === id).data.confirmed);
});

test('a story without updatedAt stays editable after a file-presence observation (#408)', () => {
  const { s } = fresh();
  const noteId = s.addEntity('note', null, { title: 'observed' });
  const p = s.exportProject();
  delete p.updatedAt; // stories created through the API carry no updatedAt
  assert.equal(s.replaceProject(p), true);
  s.updateEntity(noteId, { data: { missingFile: true } }, { history: false, observation: true });
  assert.equal(
    'updatedAt' in s.getState().project && s.getState().project.updatedAt === undefined,
    false,
  );
  assert.ok(s.addEntity('note', null, { title: 'after observation' }));
});
