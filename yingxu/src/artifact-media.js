import { safeCloudPath } from './cloud-client.js';

// Capture the source context once with the URL; an old link cannot adopt a new login.
export function nativeArtifactAttachment(client, artifact, { account, sessionId } = {}) {
  if (!account || !sessionId) throw Error('请先核对登录账户和当前创作，再下载原文件。');
  if (!artifact || typeof artifact.id !== 'string' || !/^[A-Za-z0-9_-]+$/.test(artifact.id))
    throw Error('产物标识无效，请刷新原任务后下载。');
  const expected = `/v1/artifacts/${artifact.id}/content`,
    path = artifact.download_url || artifact.content_url || expected;
  if (![expected, expected + '?download=1'].includes(safeCloudPath(path)))
    throw Error('产物下载地址与原任务不一致，请刷新原记录。');
  const href = client.nativeContentURL(path, { download: true });
  if (new URL(href, 'https://studio.invalid').searchParams.get('expected_account') !== account)
    throw Error('账户已经改变，请从当前创作重新下载原文件。');
  return Object.freeze({ href, account, sessionId });
}
export function assertArtifactAttachmentTarget(source, live) {
  if (!source || live.account !== source.account || live.session?.id !== source.sessionId)
    throw Error('账户或创作已经改变，请从当前创作重新下载原文件。');
  if (live.busy) throw Error('当前操作正在核对，请稍后下载原文件。');
}

// Keep only authenticated application URLs. Never persist a redirected storage URL.
export function artifactMedia(artifact, job, { title = '章节' } = {}) {
  if (!artifact || typeof artifact.id !== 'string' || !/^[A-Za-z0-9_-]+$/.test(artifact.id))
    return null;
  const mime = artifact.mime || artifact.metadata?.mime || artifact.metadata?.content_type;
  const type =
    artifact.kind === 'video' && mime === 'video/mp4'
      ? 'video'
      : artifact.kind === 'audio' && ['audio/flac', 'audio/x-flac'].includes(mime)
        ? 'audio'
        : null;
  if (!type) return null;
  const expected = `/v1/artifacts/${artifact.id}/content`;
  try {
    if (safeCloudPath(artifact.content_url) !== expected) return null;
  } catch {
    return null;
  }
  const extension = type === 'audio' ? 'flac' : 'mp4',
    label = type === 'audio' ? '独立混音 FLAC' : '成片 MP4';
  const filename =
    `${title}-${job?.simulation ? '模拟来源-' : ''}粗剪-${String(job?.id || '结果').slice(0, 8)}.${extension}`.replace(
      /[\\/:*?"<>|\x00-\x1f]/g,
      '_',
    );
  return {
    id: artifact.id,
    type,
    label,
    mime,
    filename,
    previewURL: expected,
    downloadURL: expected + '?download=1',
    duration: artifact.metadata?.duration,
  };
}
