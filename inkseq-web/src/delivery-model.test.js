import test from 'node:test';
import assert from 'node:assert/strict';
import { deliveryIssues, shotsAspect } from './delivery-model.js';
import { createStore } from './store.js';
test('delivery reports exact missing shot, accepted placeholder and upstream review separately', () => {
  const s = createStore({
    storage: {
      getItem() {
        return null;
      },
      setItem() {},
    },
  });
  s.newProject('验收');
  s.updateProject({ logline: '送信人寻找姐姐', journey: { sound: { mode: 'silent' } } });
  const c = s.addEntity('chapter'),
    scene = s.addEntity('scene', c, { data: { script: '开门' } }),
    shot = s.addEntity('shot', scene, { description: '开门走进雨里' });
  let issues = deliveryIssues(s.exportProject(), c);
  assert.equal(issues.length, 1);
  assert.equal(issues[0].entityId, shot);
  assert.match(issues[0].path, /新章节/);
  s.updateEntity(shot, { status: 'review', data: { acceptPlaceholder: true } });
  issues = deliveryIssues(s.exportProject(), c);
  assert.equal(issues.find((i) => i.placeholderAllowed).accepted, true);
  assert.ok(issues.find((i) => i.id === 'review-' + shot));
  s.updateEntity(shot, {
    status: 'draft',
    data: { uxReview: { chosen: 'A', status: 'selected' } },
  });
  assert.equal(deliveryIssues(s.exportProject(), c).length, 0);
});

test('the film frame follows the shots, not the story brief (#1114)', () => {
  const shot = (id, aspect, chosen = true) => ({
    id,
    data: {
      ...(chosen ? { selectedAssetId: 'a-' + id } : {}),
      ...(aspect ? { h3: { controls: { aspect_ratio: aspect } } } : {}),
    },
  });
  const p = { journey: { brief: { aspect: '9:16' } } };
  // Shots without a set shape generate 16:9, as the server does.
  assert.equal(shotsAspect(p, [shot('a'), shot('b')]), '16:9');
  assert.equal(shotsAspect(p, [shot('a', '9:16'), shot('b', '9:16'), shot('c')]), '9:16');
  // Shots with a chosen take decide; unfinished shots do not.
  assert.equal(shotsAspect(p, [shot('a', '1:1'), shot('b', '9:16', false)]), '1:1');
  // No shots: the brief, else 16:9.
  assert.equal(shotsAspect(p, []), '9:16');
  assert.equal(shotsAspect({}, []), '16:9');
});
