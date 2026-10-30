import { validateProject, store as defaultStore } from './store.js';
import { captureWorkspace, assertWorkspace } from './workspace-context.js';

const MB = 1024 * 1024;
export const bundleLimits = Object.freeze({
  file: 30 * MB,
  media: 120 * MB,
  archive: 150 * MB,
  manifest: 8 * MB,
  entries: 10002,
});
const validFileId = (id) => typeof id === 'string' && /^[\w-]{1,100}$/.test(id);
const utf8 = new TextEncoder();
let dbPromise;
let cloudMediaReader = null,
  cloudMediaUploader = null,
  cloudMediaNative = null,
  cloudMaxBytes = null;
export function setCloudMediaHandlers({
  read = null,
  upload = null,
  native = null,
  maxBytes = null,
} = {}) {
  cloudMediaReader = read;
  cloudMediaUploader = upload;
  cloudMediaNative = native;
  cloudMaxBytes = Number.isSafeInteger(maxBytes) && maxBytes > 0 ? maxBytes : null;
}
export const isCloudFileId = (id) =>
  typeof id === 'string' && /^cloud_(?:asset|artifact)_[A-Za-z0-9_-]+$/.test(id);
// View-only access. Byte processing/export continues to use getFile below.
// Capture the originating resolver and its context once; never refresh an old
// link against a later account's global handler when a user clicks it.
export function nativeMediaSource(id, { download = false, rendition = null } = {}) {
  if (!isCloudFileId(id)) {
    if (typeof id === 'string' && id.startsWith('cloud_')) throw Error('client.mediaPath');
    return null;
  }
  if (!cloudMediaNative) throw Error('client.mediaAccount');
  const source = cloudMediaNative(id, { download, rendition });
  source.assertCurrent();
  return source;
}
function database() {
  return (dbPromise ??= new Promise((resolve, reject) => {
    const req = indexedDB.open('yingxu-media-v1', 1);
    req.onupgradeneeded = () => req.result.createObjectStore('files');
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => {
      dbPromise = null;
      reject(req.error);
    };
  }));
}
export async function putFile(id, file) {
  const db = await database();
  return new Promise((resolve, reject) => {
    const tx = db.transaction('files', 'readwrite');
    tx.objectStore('files').put(file, id);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error);
  });
}
export async function getFile(id) {
  if (isCloudFileId(id)) {
    if (!cloudMediaReader) throw Error('请登录云工作室后读取此素材。');
    return cloudMediaReader(id);
  }
  const db = await database();
  return new Promise((resolve, reject) => {
    const req = db.transaction('files').objectStore('files').get(id);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}
export async function getStoredFileIds() {
  const db = await database();
  return new Promise((resolve, reject) => {
    const tx = db.transaction('files'),
      req = tx.objectStore('files').getAllKeys();
    let ids;
    req.onsuccess = () => {
      ids = req.result;
    };
    tx.oncomplete = () => resolve(ids);
    req.onerror = () => reject(req.error);
    tx.onabort = () => reject(tx.error || Error('素材存储读取已中止。'));
  });
}
// Audit keys only: returning to a project must not decode or copy every large Blob.
// A storage error is distinct from a missing file and must reach the caller.
export async function findMissingFileIds(project, { readKeys = getStoredFileIds } = {}) {
  const ids = [
    ...new Set(project.entities.map((e) => e.data.fileId).filter((id) => id && !isCloudFileId(id))),
  ];
  if (!ids.length) return [];
  const stored = new Set(await readKeys());
  return ids.filter((id) => !stored.has(id));
}
const mimeKinds = {
  'image/png': 'image',
  'image/jpeg': 'image',
  'image/webp': 'image',
  'image/gif': 'image',
  'audio/mpeg': 'audio',
  'audio/wav': 'audio',
  'audio/x-wav': 'audio',
  'audio/mp4': 'audio',
  'audio/ogg': 'audio',
  'audio/webm': 'audio',
  'audio/flac': 'audio',
  'audio/x-flac': 'audio',
  'video/mp4': 'video',
  'video/webm': 'video',
  'video/quicktime': 'video',
};
export async function uploadFile(
  file,
  expected,
  { store = defaultStore, writeFile = putFile } = {},
) {
  const context = captureWorkspace(store),
    uploader = cloudMediaUploader,
    type = mimeKinds[file.type];
  if (!type)
    throw Error(
      '请选择 PNG / JPG / WebP / GIF 图片、MP4 / WebM / MOV 视频或 MP3 / WAV / M4A / OGG 音频。',
    );
  if (expected && type !== expected)
    throw Error(
      '这里需要' + { image: '图片', audio: '音频', video: '视频' }[expected] + '，请重新选择。',
    );
  const maxBytes = uploader && cloudMaxBytes ? cloudMaxBytes : bundleLimits.file;
  if (file.size > maxBytes)
    throw Error('每个素材最多 ' + Math.floor(maxBytes / MB) + ' MB，请先压缩或截取需要的片段。');
  if (!file.size) throw Error('文件为空，请换一个文件。');
  if (context.mode === 'cloud' && !uploader)
    throw Error('云上传尚未就绪，请重新核对登录后重试；未把文件改存到本机。');
  let result;
  if (uploader) result = await uploader(file, type);
  else {
    const fileId = crypto.randomUUID();
    await writeFile(fileId, file);
    result = {
      type,
      data: {
        fileId,
        fileName: file.name,
        mime: file.type,
        bytes: file.size,
        source: 'upload',
        missingFile: false,
      },
    };
  }
  // Keep the completed Blob/receipt; rejecting attachment never deletes media.
  assertWorkspace(store, context);
  return result;
}
export function download(blob, name) {
  const url = URL.createObjectURL(blob),
    a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}

function checkedProject(input) {
  const checked = validateProject(input);
  if (!checked.ok) throw Error(checked.error || '项目格式不正确。');
  return checked.project;
}
function markMissing(entity) {
  delete entity.data.fileId;
  entity.data.missingFile = true;
}
function manifestText(project) {
  const text = JSON.stringify(project, null, 2);
  if (utf8.encode(text).length > bundleLimits.manifest)
    throw Error('项目结构超过 8 MB，请拆分项目后再备份；当前项目未改变。');
  return text;
}

export async function exportBundle(input, { readFile = getFile } = {}) {
  const project = checkedProject(input),
    media = new Map(),
    missing = [];
  let total = 0;
  for (const e of project.entities) {
    if (!e.data.fileId) {
      if (e.data.missingFile) missing.push(e.title);
      continue;
    }
    const id = e.data.fileId;
    if (!validFileId(id)) throw Error('素材标识不合法，请重新上传这份素材。');
    if (!media.has(id)) {
      const blob = await readFile(id);
      media.set(id, blob || null);
      if (blob) {
        if (blob.size > bundleLimits.file)
          throw Error('备份包含超过 30 MB 的素材，请先压缩或替换这份素材。');
        total += blob.size;
        if (total > bundleLimits.media)
          throw Error(
            '完整备份的不同素材合计超过 120 MB，无法生成可恢复的备份。请将项目拆分，或压缩并替换较大素材后重试；没有素材被删除。',
          );
      }
    }
    if (!media.get(id)) {
      missing.push(e.title);
      markMissing(e);
    } else e.data.missingFile = false;
  }
  const { default: JSZip } = await import('jszip');
  const zip = new JSZip();
  zip.file('project.json', manifestText(project));
  for (const [id, blob] of media) if (blob) zip.file('media/' + id, await blob.arrayBuffer());
  zip.file(
    '说明.txt',
    'Inkseq 项目备份：project.json 保存项目结构，media 保存本次可读取的素材。\n云任务、源素材与候选归属信息保留在项目中；这不是自动合成的最终成片。\n限制：单份素材 30 MB，不同素材合计 120 MB，项目结构 8 MB。\n' +
      (missing.length ? '这是不完整备份，缺失素材：' + missing.join('、') : '素材完整。'),
  );
  const bytes = await zip.generateAsync({ type: 'uint8array', compression: 'STORE' });
  if (bytes.byteLength > bundleLimits.archive)
    throw Error('备份超过 150 MB，无法在本原型恢复。请拆分项目后重试；当前项目未改变。');
  return { blob: new Blob([bytes], { type: 'application/zip' }), missing };
}

function entryPreflight(entry, limit, label) {
  // JSZip 3.x exposes these sizes internally. They are early hints, not a trust boundary;
  // boundedEntry also limits actual bytes while streaming decompression.
  const meta = entry?._data,
    size = meta?.uncompressedSize,
    compressed = meta?.compressedSize;
  if (Number.isFinite(size) && size > limit)
    throw Error(label + '超过允许大小，请拆分或压缩素材后重新导出。');
  if (
    Number.isFinite(size) &&
    Number.isFinite(compressed) &&
    size > MB &&
    (compressed === 0 || size / Math.max(1, compressed) > 1000)
  )
    throw Error(label + '的压缩比异常，请使用本工作室导出的备份。');
}
function boundedEntry(entry, limit, label) {
  entryPreflight(entry, limit, label);
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0,
      done = false;
    const stream = entry.internalStream('uint8array');
    stream.on('data', (chunk) => {
      if (done) return;
      size += chunk.byteLength;
      if (size > limit) {
        done = true;
        stream.pause();
        chunks.length = 0;
        reject(Error(label + '解压后超过允许大小，导入已停止。'));
        return;
      }
      chunks.push(chunk);
    });
    stream.on('error', (error) => {
      if (!done) {
        done = true;
        reject(error);
      }
    });
    stream.on('end', () => {
      if (done) return;
      done = true;
      const bytes = new Uint8Array(size);
      let offset = 0;
      for (const chunk of chunks) {
        bytes.set(chunk, offset);
        offset += chunk.byteLength;
      }
      resolve(bytes);
    });
    stream.resume();
  });
}

export async function readBundle(file) {
  if (file.size > bundleLimits.archive)
    throw Error('备份超过 150 MB，请在原项目中拆分或压缩素材后重新备份。');
  let project, zip;
  if (file.name.toLowerCase().endsWith('.json')) {
    if (file.size > bundleLimits.manifest) throw Error('项目结构超过 8 MB，无法导入，请拆分项目。');
    project = JSON.parse(await file.text());
  } else {
    const { default: JSZip } = await import('jszip');
    zip = await JSZip.loadAsync(await file.arrayBuffer());
    if (Object.keys(zip.files).length > bundleLimits.entries)
      throw Error('备份包含过多文件，请使用本工作室导出的备份。');
    const manifest = zip.file('project.json');
    if (!manifest) throw Error('备份中没有 project.json。');
    const bytes = await boundedEntry(manifest, bundleLimits.manifest, '项目结构');
    project = JSON.parse(new TextDecoder().decode(bytes));
  }
  project = checkedProject(project);
  const files = [],
    missing = [],
    restored = new Map();
  let total = 0;
  for (const e of project.entities) {
    if (!e.data.fileId) {
      if (e.data.missingFile) missing.push(e.title);
      continue;
    }
    const sourceId = e.data.fileId;
    if (!validFileId(sourceId)) throw Error('素材标识不合法。');
    if (!restored.has(sourceId)) {
      const entry = zip?.file('media/' + sourceId);
      if (!entry) restored.set(sourceId, null);
      else {
        const remaining = bundleLimits.media - total;
        if (remaining <= 0) throw Error('不同素材合计超过 120 MB，请拆分项目后重新备份。');
        const bytes = await boundedEntry(
          entry,
          Math.min(bundleLimits.file, remaining),
          '素材「' + e.title + '」',
        );
        total += bytes.byteLength;
        const id = crypto.randomUUID(),
          blob = new Blob([bytes], { type: e.data.mime || 'application/octet-stream' });
        restored.set(sourceId, { id, size: blob.size });
        files.push([id, blob]);
      }
    }
    const result = restored.get(sourceId);
    if (result) {
      e.data.fileId = result.id;
      e.data.bytes = result.size;
      e.data.missingFile = false;
    } else {
      markMissing(e);
      missing.push(e.title);
    }
  }
  for (const e of project.entities) {
    const range = e.data.selectedVideoRange,
      next = range && restored.get(range.fileId);
    if (next) range.fileId = next.id;
  }
  for (const e of project.entities)
    for (const range of Object.values(e.data.referenceRanges || {})) {
      const next = restored.get(range.fileId);
      if (next) range.fileId = next.id;
    }
  for (const tracks of Object.values(project.journey?.soundTracks || {}))
    for (const track of tracks) {
      const next = restored.get(track.fileId);
      if (next) track.fileId = next.id;
    }
  return { project, files, missing };
}
