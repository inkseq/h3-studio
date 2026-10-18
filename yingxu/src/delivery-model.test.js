import test from 'node:test';
import assert from 'node:assert/strict';
import { deliveryIssues } from './delivery-model.js';
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
