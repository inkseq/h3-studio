import test from 'node:test';
import assert from 'node:assert/strict';
import JSZip from 'jszip';
import { exportBundle, readBundle, bundleLimits, findMissingFileIds } from './media.js';

function project(assets = []) {
  return {
    schemaVersion: 4,
    id: 'project-test',
    title: '备份测试',
    logline: '',
    entities: assets.map((a, i) => ({
      id: 'asset-' + i,
      type: 'image',
      title: '素材 ' + i,
      description: '',
      parentId: null,
      order: i,
      version: 1,
      status: 'draft',
      data: { fileId: a, fileName: a + '.png', mime: 'image/png', bytes: 5 },
    })),
    links: [],
    layout: { positions: {}, viewport: { x: 0, y: 0, zoom: 1 } },
    jobs: [],
  };
}
function file(blob, name = 'project.yingxu.zip') {
  return new File([blob], name);
}
test('v6 backup restores look bindings, selected candidates, trimmed references and sound file identities together', async () => {
  const p = project(['image-file', 'audio-file', 'video-file']);
  p.entities[1].type = 'audio';
  p.entities[2].type = 'video';
  const entity = (id, type, parentId, data = {}) => ({
    id,
    type,
    parentId,
    data,
    title: id,
    description: '',
    order: 0,
    version: 1,
    status: 'draft',
  });
  p.entities.push(
    entity('c', 'chapter', null),
    entity('s', 'scene', 'c', { cast: [{ characterId: 'actor', lookId: 'rain' }] }),
    entity('q', 'shot', 's', {
      seconds: 5,
      selectedAssetId: 'asset-0',
      candidateIds: ['asset-0'],
      referenceRanges: { motion: { start: 1, end: 3, duration: 8, fileId: 'video-file' } },
    }),
    entity('actor', 'character', null, {
      looks: [
        {
          id: 'rain',
          name: '雨衣',
          description: '绿色',
          version: 1,
          gallery: { front: 'asset-0' },
        },
      ],
    }),
  );
  p.links = [{ id: 'motion', source: 'asset-2', target: 'q', role: 'motion' }];
  p.journey = {
    stage: 6,
    sound: { mode: 'mixed' },
    soundTracks: {
      c: [
        {
          id: 'track',
          assetId: 'asset-1',
          fileId: 'audio-file',
          start: 1,
          end: 4,
          duration: 8,
          shotId: 'q',
          offset: 0,
          gain: 0.4,
          muted: false,
        },
      ],
    },
  };
  const { blob } = await exportBundle(p, { readFile: () => new Blob(['fixture']) }),
    restored = await readBundle(file(blob)),
    q = restored.project.entities.find((e) => e.id === 'q');
  assert.deepEqual(
    restored.project.entities.find((e) => e.id === 's').data.cast,
    p.entities.find((e) => e.id === 's').data.cast,
  );
  assert.equal(
    restored.project.entities.find((e) => e.id === 'actor').data.looks[0].gallery.front,
    'asset-0',
  );
  assert.equal(q.data.selectedAssetId, 'asset-0');
  assert.deepEqual(q.data.candidateIds, ['asset-0']);
  assert.equal(q.data.referenceRanges.motion.start, 1);
  assert.equal(q.data.referenceRanges.motion.fileId, restored.project.entities[2].data.fileId);
  assert.equal(
    restored.project.journey.soundTracks.c[0].fileId,
    restored.project.entities[1].data.fileId,
  );
  assert.notEqual(restored.project.entities[1].data.fileId, 'audio-file');
  assert.equal(restored.files.length, 3);
});
async function zipWith(project, entries = {}) {
  const zip = new JSZip();
  zip.file('project.json', JSON.stringify(project));
  for (const [name, data] of Object.entries(entries)) zip.file(name, data);
  return file(await zip.generateAsync({ type: 'uint8array' }));
}
function alterCentralSize(input, name, size) {
  const bytes = new Uint8Array(input),
    view = new DataView(bytes.buffer);
  for (let i = 0; i < bytes.length - 46; i++) {
    if (view.getUint32(i, true) !== 0x02014b50) continue;
    const n = view.getUint16(i + 28, true);
    if (new TextDecoder().decode(bytes.subarray(i + 46, i + 46 + n)) === name) {
      view.setUint32(i + 24, size, true);
      return bytes;
    }
  }
  throw Error('entry absent');
}

test('file audit reports unique missing IDs without reading or mutating media entities', async () => {
  const p = project(['exists', 'gone', 'gone']),
    before = structuredClone(p);
  let reads = 0;
  const missing = await findMissingFileIds(p, {
    readKeys: async () => {
      reads++;
      return ['exists', 'another-project-file'];
    },
  });
  assert.deepEqual(missing, ['gone']);
  assert.equal(reads, 1);
  assert.deepEqual(p, before);
});

test('file audit does not confuse storage access errors with missing files', async () => {
  const failure = Error('IndexedDB denied'),
    p = project(['exists']);
  await assert.rejects(
    findMissingFileIds(p, {
      readKeys: async () => {
        throw failure;
      },
    }),
    (error) => error === failure,
  );
  assert.equal(p.entities[0].data.missingFile, undefined);
});

test('file audit skips empty projects and recognizes files restored under their existing IDs', async () => {
  const p = project(['restored']);
  p.entities[0].data.missingFile = true;
  assert.deepEqual(await findMissingFileIds(p, { readKeys: async () => ['restored'] }), []);
  assert.deepEqual(
    await findMissingFileIds(project(), {
      readKeys: async () => {
        throw Error('must not access storage');
      },
    }),
    [],
  );
});

test('full bundle roundtrip preserves structure and unique file content', async () => {
  const p = project(['file-a', 'file-b']),
    before = structuredClone(p),
    blobs = new Map([
      ['file-a', new Blob(['image-one'], { type: 'image/png' })],
      ['file-b', new Blob(['image-two'], { type: 'image/png' })],
    ]);
  const exported = await exportBundle(p, { readFile: (id) => blobs.get(id) }),
    restored = await readBundle(file(exported.blob));
  assert.deepEqual(p, before);
  assert.deepEqual(exported.missing, []);
  assert.equal(restored.project.id, p.id);
  assert.deepEqual(
    restored.project.entities.map((e) => e.id),
    p.entities.map((e) => e.id),
  );
  assert.equal(restored.files.length, 2);
  assert.deepEqual(await Promise.all(restored.files.map(([, blob]) => blob.text())), [
    'image-one',
    'image-two',
  ]);
  assert.ok(restored.project.entities.every((e) => e.data.missingFile === false));
});

test('shared source file is read once, exported once and restored to one fresh ID', async () => {
  const p = project(Array(5).fill('shared-file'));
  let reads = 0;
  const { blob } = await exportBundle(p, {
      readFile: () => {
        reads++;
        return new Blob(['same-file']);
      },
    }),
    restored = await readBundle(file(blob));
  assert.equal(reads, 1);
  assert.equal(restored.files.length, 1);
  assert.equal(new Set(restored.project.entities.map((e) => e.data.fileId)).size, 1);
  assert.notEqual(restored.project.entities[0].data.fileId, 'shared-file');
  const zip = await JSZip.loadAsync(await blob.arrayBuffer());
  assert.equal(zip.file(/^media\/.+/).length, 1);
});

test('missing files are explicit in ZIP and JSON imports; entity metadata and candidate references survive', async () => {
  const p = project(['missing-file']);
  p.entities.push(
    {
      id: 'chapter',
      type: 'chapter',
      title: '章',
      description: '',
      parentId: null,
      order: 1,
      version: 1,
      status: 'draft',
      data: {},
    },
    {
      id: 'scene',
      type: 'scene',
      title: '场',
      description: '',
      parentId: 'chapter',
      order: 0,
      version: 1,
      status: 'draft',
      data: {},
    },
    {
      id: 'shot',
      type: 'shot',
      title: '镜',
      description: '',
      parentId: 'scene',
      order: 0,
      version: 1,
      status: 'draft',
      data: { seconds: 6, selectedAssetId: 'asset-0' },
    },
  );
  for (const input of [file(JSON.stringify(p), 'project.json'), await zipWith(p)]) {
    const pack = await readBundle(input);
    assert.deepEqual(pack.missing, ['素材 0']);
    assert.equal(pack.files.length, 0);
    assert.equal(pack.project.entities[0].data.fileId, undefined);
    assert.equal(pack.project.entities[0].data.missingFile, true);
    assert.equal(pack.project.entities[0].data.fileName, 'missing-file.png');
    assert.equal(pack.project.entities[3].data.selectedAssetId, 'asset-0');
  }
  assert.equal(p.entities[0].data.fileId, 'missing-file');
});

test('partial export reports missing media and its manifest cannot claim presence', async () => {
  const p = project(['gone']),
    result = await exportBundle(p, { readFile: () => undefined });
  assert.deepEqual(result.missing, ['素材 0']);
  const restored = await readBundle(file(result.blob));
  assert.deepEqual(restored.missing, ['素材 0']);
  assert.equal(restored.project.entities[0].data.fileId, undefined);
  assert.equal(restored.project.entities[0].data.missingFile, true);
  assert.equal(p.entities[0].data.fileId, 'gone');
});

test('export stops at aggregate limit before reading file bytes and never silently drops media', async () => {
  const p = project(['one', 'two', 'three', 'four', 'five']);
  let byteReads = 0;
  await assert.rejects(
    exportBundle(p, {
      readFile: () => ({
        size: 25 * 1024 * 1024,
        arrayBuffer() {
          byteReads++;
          throw Error('must not read');
        },
      }),
    }),
    /120 MB/,
  );
  assert.equal(byteReads, 0);
  assert.equal(p.entities.length, 5);
  assert.ok(p.entities.every((e) => e.data.fileId));
});

test('export rejects per-file overflow before ZIP construction', async () => {
  await assert.rejects(
    exportBundle(project(['large']), {
      readFile: () => ({
        size: bundleLimits.file + 1,
        arrayBuffer() {
          throw Error('must not read');
        },
      }),
    }),
    /30 MB/,
  );
});

test('JSON and ZIP archive input limits reject before file read', async () => {
  await assert.rejects(
    readBundle({
      name: 'project.json',
      size: bundleLimits.manifest + 1,
      text() {
        throw Error('must not read');
      },
    }),
    /8 MB/,
  );
  await assert.rejects(
    readBundle({
      name: 'project.zip',
      size: bundleLimits.archive + 1,
      arrayBuffer() {
        throw Error('must not read');
      },
    }),
    /150 MB/,
  );
});

test('ZIP manifest size is checked before inflation', async () => {
  const zip = new JSZip();
  zip.file('project.json', '{}');
  const bytes = await zip.generateAsync({ type: 'uint8array' }),
    bad = alterCentralSize(bytes, 'project.json', bundleLimits.manifest + 1);
  await assert.rejects(readBundle(file(bad)), /项目结构超过允许大小/);
});

test('ZIP streaming limit also catches understated manifest metadata', async () => {
  const zip = new JSZip();
  zip.file('project.json', 'a'.repeat(bundleLimits.manifest + 1000));
  const bytes = await zip.generateAsync({ type: 'uint8array', compression: 'DEFLATE' }),
    bad = alterCentralSize(bytes, 'project.json', 10);
  await assert.rejects(readBundle(file(bad)), /项目结构解压后超过允许大小/);
});

test('malformed structure, missing manifest and illegal media IDs are rejected', async () => {
  await assert.rejects(readBundle(file('{broken', 'project.json')), SyntaxError);
  const zip = new JSZip();
  zip.file('other.json', '{}');
  await assert.rejects(
    readBundle(file(await zip.generateAsync({ type: 'uint8array' }))),
    /没有 project.json/,
  );
  await assert.rejects(
    readBundle(file(JSON.stringify(project(['../outside'])), 'project.json')),
    /素材标识不合法/,
  );
});
