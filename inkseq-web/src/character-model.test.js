import test from 'node:test';
import assert from 'node:assert/strict';
import {
  addLook,
  editLook,
  bindLook,
  deleteLook,
  effectiveLook,
  lookReferences,
  characterPath,
} from './character-model.js';

function fixture() {
  const make = (id, type, parentId = null, data = {}) => ({
    id,
    type,
    parentId,
    title: id,
    description: '',
    version: 1,
    status: 'ready',
    data,
  });
  return {
    entities: [
      make('chapter', 'chapter'),
      make('rain', 'scene', 'chapter', { cast: [{ characterId: 'hero', lookId: 'coat' }] }),
      make('office', 'scene', 'chapter', { cast: [{ characterId: 'hero', lookId: 'suit' }] }),
      make('inherited', 'shot', 'rain', {
        selectedAssetId: 'img',
        uxReview: { chosen: 'A', note: '保留表情' },
      }),
      make('override', 'shot', 'rain', { cast: [{ characterId: 'hero', lookId: 'suit' }] }),
      make('other-scene', 'shot', 'office'),
      make('unbound', 'shot', 'office', { cast: [{ characterId: 'other', lookId: 'unrelated' }] }),
      make('hero', 'character', null, {
        looks: [
          {
            id: 'coat',
            name: '雨夜外套',
            description: '绿色',
            gallery: { front: 'img' },
            version: 1,
          },
          { id: 'suit', name: '工作制服', description: '灰色', gallery: {}, version: 1 },
        ],
      }),
      make('img', 'image', null, { fileId: 'file' }),
    ],
    journey: { stage: 3 },
  };
}

test('explicit shot look overrides scene look; paths retain chapter and scene', () => {
  const p = fixture();
  assert.deepEqual(
    effectiveLook(
      p,
      p.entities.find((e) => e.id === 'inherited'),
      'hero',
    ),
    { characterId: 'hero', lookId: 'coat', inherited: true, sourceId: 'rain' },
  );
  assert.equal(
    effectiveLook(
      p,
      p.entities.find((e) => e.id === 'override'),
      'hero',
    ).lookId,
    'suit',
  );
  assert.equal(characterPath(p, 'inherited'), 'chapter / rain / inherited');
});

test('editing one look marks only shots effectively using it and retains chosen assets/notes', () => {
  const p = fixture(),
    identity = structuredClone(p.entities.find((e) => e.id === 'hero'));
  const result = editLook(p, 'hero', 'coat', { description: '绿色，袖口湿透' });
  assert.deepEqual(result.affected, ['inherited']);
  assert.equal(p.entities.find((e) => e.id === 'inherited').status, 'review');
  assert.equal(p.entities.find((e) => e.id === 'override').status, 'ready');
  assert.equal(p.entities.find((e) => e.id === 'other-scene').status, 'ready');
  assert.equal(p.entities.find((e) => e.id === 'inherited').data.selectedAssetId, 'img');
  assert.deepEqual(p.entities.find((e) => e.id === 'inherited').data.uxReview, {
    chosen: 'A',
    note: '保留表情',
  });
  assert.equal(p.entities.find((e) => e.id === 'hero').description, identity.description);
  assert.equal(p.entities.find((e) => e.id === 'hero').data.looks[0].version, 2);
});

test('scene look changes leave explicit shot overrides untouched', () => {
  const p = fixture();
  const result = bindLook(p, 'rain', 'hero', 'suit');
  assert.deepEqual(result.affected, ['inherited']);
  assert.equal(p.entities.find((e) => e.id === 'override').status, 'ready');
});

test('removing shot override resumes scene inheritance without removing other characters', () => {
  const p = fixture(),
    shot = p.entities.find((e) => e.id === 'override');
  shot.data.cast.push({ characterId: 'other', lookId: 'unrelated' });
  assert.deepEqual(bindLook(p, 'override', 'hero', '').affected, ['override']);
  assert.equal(effectiveLook(p, shot, 'hero').lookId, 'coat');
  assert.equal(effectiveLook(p, shot, 'hero').inherited, true);
  assert.deepEqual(shot.data.cast, [{ characterId: 'other', lookId: 'unrelated' }]);
});

test('a redundant override does not falsely mark a shot for review', () => {
  const p = fixture();
  assert.deepEqual(bindLook(p, 'inherited', 'hero', 'coat').affected, []);
  assert.equal(p.entities.find((e) => e.id === 'inherited').status, 'ready');
});

test('deleting a used look rebinds all direct references and marks only changed effective shots', () => {
  const p = fixture();
  const result = deleteLook(p, 'hero', 'coat', 'suit');
  assert.deepEqual(result.affected, ['inherited']);
  assert.equal(lookReferences(p, 'hero', 'coat').length, 0);
  assert.equal(p.entities.find((e) => e.id === 'hero').data.looks.length, 1);
  assert.equal(
    effectiveLook(
      p,
      p.entities.find((e) => e.id === 'inherited'),
      'hero',
    ).lookId,
    'suit',
  );
  assert.equal(p.entities.find((e) => e.id === 'inherited').data.selectedAssetId, 'img');
  assert.equal(p.entities.find((e) => e.id === 'override').status, 'ready');
});

test('deleting without replacement clears bindings, retains other looks and image files', () => {
  const p = fixture();
  deleteLook(p, 'hero', 'suit');
  assert.equal(lookReferences(p, 'hero', 'suit').length, 0);
  assert.equal(
    effectiveLook(
      p,
      p.entities.find((e) => e.id === 'override'),
      'hero',
    ).lookId,
    'coat',
  );
  assert.equal(
    effectiveLook(
      p,
      p.entities.find((e) => e.id === 'other-scene'),
      'hero',
    ),
    null,
  );
  assert.ok(p.entities.find((e) => e.id === 'img'));
});

test('invalid assets, duplicate names and missing look replacements are rejected before mutation', () => {
  const p = fixture(),
    before = structuredClone(p);
  assert.equal(editLook(p, 'hero', 'coat', { gallery: { front: 'gone' } }).ok, false);
  assert.equal(editLook(p, 'hero', 'coat', { name: '工作制服' }).ok, false);
  assert.equal(deleteLook(p, 'hero', 'coat', 'unknown').ok, false);
  assert.equal(bindLook(p, 'rain', 'hero', 'unknown').ok, false);
  assert.equal(addLook(p, 'hero', { id: 'duplicate', name: '雨夜外套' }).ok, false);
  assert.deepEqual(p, before);
});

test('adding an independent look leaves identity and current shots untouched', () => {
  const p = fixture(),
    before = structuredClone(p.entities.find((e) => e.id === 'hero'));
  assert.equal(addLook(p, 'hero', { id: 'party', name: '庆功宴礼服' }).ok, true);
  assert.equal(p.entities.find((e) => e.id === 'hero').title, before.title);
  assert.equal(p.entities.find((e) => e.id === 'hero').description, before.description);
  assert.ok(p.entities.filter((e) => e.type === 'shot').every((e) => e.status === 'ready'));
});
