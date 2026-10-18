import test from 'node:test';
import assert from 'node:assert/strict';
import {
  applyBreakdown,
  createBreakdown,
  importUndoIssues,
  mergeBreakdownSegment,
  replaceBreakdown,
  sourceLocation,
  splitBreakdownSegment,
  undoBreakdownImport,
} from './script-breakdown-model.js';
import { createStore, validateProject } from './store.js';

function setup(source = '车站，夜。\n小雨等车。\n\n旧屋，晨。\n姐姐打开门。') {
  const project = {
    schemaVersion: 4,
    id: 'test-project',
    title: '测试短剧',
    logline: '',
    entities: [],
    links: [],
    jobs: [],
    layout: { positions: {}, viewport: { x: 0, y: 0, zoom: 1 } },
    journey: { brief: { sourceScript: source } },
  };
  replaceBreakdown(project, createBreakdown(source));
  return project;
}

test('preview preserves exact source and ranges; source locations follow CRLF and leading whitespace', () => {
  const source = '  车站\r\n雨夜  \r\n\r\n  老屋\r\n清晨  ',
    draft = createBreakdown(source);
  assert.equal(draft.source, source);
  assert.equal(draft.segments.length, 2);
  assert.equal(draft.segments[0].script, '车站\r\n雨夜');
  for (const s of draft.segments)
    assert.equal(source.slice(s.ranges[0].start, s.ranges[0].end), s.script);
  assert.match(sourceLocation(source, draft.segments[1].ranges[0]), /第 4–5 行/);
  assert.equal(createBreakdown('没有空行的一整段剧本。').segments.length, 1);
});

test('split retains precise unedited ranges; manually rewritten split retains shared original evidence', () => {
  const draft = createBreakdown('车站。\n女孩到达。'),
    id = draft.segments[0].id;
  splitBreakdownSegment(draft, id, 4);
  assert.equal(draft.segments.length, 2);
  assert.equal(draft.segments.map((s) => s.script).join(''), draft.source);
  for (const s of draft.segments)
    assert.equal(draft.source.slice(s.ranges[0].start, s.ranges[0].end), s.script);
  draft.segments[0].script = '先经过街道。然后抵达车站。';
  splitBreakdownSegment(draft, id, 6);
  assert.equal(draft.segments[0].mapping, 'shared');
  assert.deepEqual(draft.segments[0].ranges, draft.segments[1].ranges);
  assert.throws(() => splitBreakdownSegment(draft, id, 0), /光标/);
});

test('merge skips omitted scene without importing its source; omission can be restored', () => {
  const draft = createBreakdown('第一段\n\n误识别段\n\n第三段');
  draft.segments[1].omitted = true;
  mergeBreakdownSegment(draft, draft.segments[0].id);
  assert.equal(draft.segments.length, 2);
  assert.equal(draft.segments[0].script, '第一段\n\n第三段');
  assert.equal(draft.segments[0].ranges.length, 2);
  assert.equal(draft.segments[1].script, '误识别段');
  draft.segments[1].omitted = false;
  assert.equal(draft.segments.filter((s) => !s.omitted).length, 2);
});

test('application is one batch, validates schema, preserves source, and cannot be applied twice', () => {
  const p = setup(),
    originalSource = p.journey.brief.sourceScript,
    batch = p.journey.breakdown.batchId;
  const receipt = applyBreakdown(p, batch);
  assert.equal(p.entities.filter((e) => e.type === 'chapter').length, 1);
  assert.equal(p.entities.filter((e) => e.type === 'scene').length, 2);
  assert.equal(receipt.entities.length, 3);
  assert.equal(p.journey.brief.sourceScript, originalSource);
  assert.deepEqual(validateProject(p).ok, true);
  assert.throws(() => applyBreakdown(p, batch), /已经导入/);
  assert.equal(p.entities.length, 3);
});

test('append and targeted undo preserve old scenes and unrelated edits after import', () => {
  const p = setup();
  p.entities.push(
    {
      id: 'old-chapter',
      type: 'chapter',
      title: '原有章',
      description: '',
      parentId: null,
      order: 2,
      version: 1,
      status: 'draft',
      data: {},
    },
    {
      id: 'old-scene',
      type: 'scene',
      title: '原有场',
      description: '',
      parentId: 'old-chapter',
      order: 3,
      version: 1,
      status: 'draft',
      data: { script: '原剧本' },
    },
  );
  p.journey.breakdown.targetChapterId = 'old-chapter';
  const batch = p.journey.breakdown.batchId;
  applyBreakdown(p, batch);
  assert.deepEqual(
    p.entities.slice(2).map((e) => e.order),
    [4, 5],
  );
  p.entities[1].data.script = '导入后编辑原有场';
  p.title = '另一个不相关编辑';
  p.journey.sound = { mode: 'silent' };
  assert.equal(importUndoIssues(p, p.journey.breakdown.receipt).length, 0);
  undoBreakdownImport(p, batch);
  assert.equal(p.entities.length, 2);
  assert.equal(p.entities[1].data.script, '导入后编辑原有场');
  assert.equal(p.title, '另一个不相关编辑');
  assert.equal(p.journey.sound.mode, 'silent');
  assert.equal(validateProject(p).ok, true);
});

test('targeted undo refuses changed imported content, new descendants, and new connections', () => {
  const edited = setup();
  let receipt = applyBreakdown(edited, edited.journey.breakdown.batchId),
    scene = edited.entities.find((e) => e.type === 'scene');
  scene.data.script = '后续手工编辑';
  assert.equal(importUndoIssues(edited, receipt)[0].id, scene.id);
  assert.throws(() => undoBreakdownImport(edited, edited.journey.breakdown.batchId), /保护/);
  assert.equal(scene.data.script, '后续手工编辑');
  const p = setup();
  receipt = applyBreakdown(p, p.journey.breakdown.batchId);
  scene = p.entities.find((e) => e.type === 'scene');
  p.entities.push({
    id: 'new-shot',
    type: 'shot',
    title: '后加镜头',
    description: '',
    parentId: scene.id,
    order: 0,
    version: 1,
    status: 'draft',
    data: { seconds: 6 },
  });
  assert.equal(importUndoIssues(p, receipt).at(-1).reason, '在导入后添加了内容');
  assert.throws(() => undoBreakdownImport(p, p.journey.breakdown.batchId), /保护/);
});

test('reanalysis preserves applied records; former import remains independently undoable', () => {
  const p = setup(),
    first = p.journey.breakdown.batchId;
  applyBreakdown(p, first);
  replaceBreakdown(p, createBreakdown('新的单场原文'));
  assert.equal(p.journey.breakdownImports[0].batchId, first);
  assert.equal(p.journey.breakdownImports[0].source, p.journey.brief.sourceScript);
  undoBreakdownImport(p, first);
  assert.equal(p.entities.length, 0);
  assert.equal(p.journey.breakdown.source, '新的单场原文');
  assert.equal(p.journey.breakdown.status, 'draft');
  assert.equal(validateProject(p).ok, true);
});

test('oversized/empty breakdown, missing target, and omitted-all drafts fail without partial application', () => {
  assert.throws(() => createBreakdown('  '), /粘贴剧本/);
  assert.throws(
    () => createBreakdown(Array.from({ length: 31 }, (_, i) => String(i)).join('\n\n')),
    /31 段/,
  );
  const p = setup();
  p.journey.breakdown.targetChapterId = 'gone';
  assert.throws(() => applyBreakdown(p, p.journey.breakdown.batchId), /已不存在/);
  assert.equal(p.entities.length, 0);
  p.journey.breakdown.targetChapterId = '';
  p.journey.breakdown.segments.forEach((s) => (s.omitted = true));
  assert.throws(() => applyBreakdown(p, p.journey.breakdown.batchId), /至少保留/);
  assert.equal(p.entities.length, 0);
});

test('store batch commits atomically, persists draft and source, and undo/redo does not duplicate entities', () => {
  const values = new Map(),
    storage = {
      getItem: (key) => values.get(key) || null,
      setItem: (key, value) => values.set(key, value),
    };
  const store = createStore({ storage }),
    p = setup();
  assert.equal(store.replaceProject(p), true);
  const batch = p.journey.breakdown.batchId;
  assert.equal(
    store.editProject((project) => {
      applyBreakdown(project, batch);
    }),
    true,
  );
  const ids = store.getState().project.entities.map((e) => e.id);
  assert.equal(ids.length, 3);
  store.undo();
  assert.equal(store.getState().project.entities.length, 0);
  assert.equal(store.getState().project.journey.breakdown.status, 'draft');
  store.redo();
  assert.deepEqual(
    store.getState().project.entities.map((e) => e.id),
    ids,
  );
  const reloaded = createStore({ storage });
  assert.deepEqual(
    reloaded.getState().project.journey.breakdown,
    store.getState().project.journey.breakdown,
  );
  const before = JSON.stringify(store.getState().project);
  assert.equal(
    store.editProject((project) => {
      applyBreakdown(project, batch);
    }),
    false,
  );
  assert.equal(JSON.stringify(store.getState().project), before);
});
