import test from 'node:test';
import assert from 'node:assert/strict';
import { storyboardPrompt } from './shot-prompt.js';
const project = {
  entities: [
    {
      id: 'scene',
      type: 'scene',
      data: {
        script: '她打开信。下一镜切到列车。',
        cast: [{ characterId: 'person', lookId: 'coat' }],
      },
    },
    {
      id: 'shot',
      type: 'shot',
      parentId: 'scene',
      description: '林夏抬头看向广播。',
      data: { prompt: '保留原提示词', shotSize: '中景', cameraMove: '缓慢推进' },
    },
    {
      id: 'person',
      type: 'character',
      title: '林夏',
      description: '年轻快递员',
      data: { looks: [{ id: 'coat', name: '雨衣', description: '黄色雨衣' }] },
    },
    { id: 'place', type: 'location', title: '车站', description: '空荡的候车室', data: {} },
  ],
  links: [{ source: 'place', target: 'shot', role: 'location' }],
};
test('draft uses effective cast, explicit shot and linked locations, not whole scene by default', () => {
  const before = structuredClone(project),
    draft = storyboardPrompt(project, 'shot');
  assert.match(draft, /林夏抬头/);
  assert.match(draft, /中景；缓慢推进/);
  assert.match(draft, /黄色雨衣/);
  assert.match(draft, /空荡的候车室/);
  assert.doesNotMatch(draft, /下一镜|保留原提示词/);
  assert.deepEqual(project, before);
  assert.match(storyboardPrompt(project, 'shot', { includeScene: true }), /下一镜切到列车/);
  assert.doesNotMatch(
    storyboardPrompt(project, 'shot', { includeCharacters: false, includeLocations: false }),
    /黄色雨衣|空荡的候车室/,
  );
});
