import { chapterShots } from './delivery-model.js';
import { chapterTiming } from './media-timing.js';
import { stableJSON } from './cloud-model.js';

export const captionTrack = (project, chapterId) =>
  project.journey?.captionTracks?.[chapterId] || { cues: [] };
export function captionBasis(project, chapterId) {
  return chapterTiming(chapterShots(project, chapterId)).map((item) => {
    const asset = project.entities.find((e) => e.id === item.shot.data.selectedAssetId);
    return {
      id: item.id,
      start: item.start,
      end: item.end,
      ...(item.shot.data.selectedVideoRange
        ? { selectedVideoRange: structuredClone(item.shot.data.selectedVideoRange) }
        : {}),
      asset: asset
        ? { id: asset.id, fileId: asset.data.fileId || null, missing: !!asset.data.missingFile }
        : null,
    };
  });
}
export const orderedCues = (cues) =>
  [...(Array.isArray(cues) ? cues : [])].sort(
    (a, b) => Number(a.start) - Number(b.start) || Number(a.end) - Number(b.end),
  );
export function captionIssues(cues, duration) {
  const issues = [];
  if (!Array.isArray(cues) || !cues.length) return ['先添加你已核对的对白或字幕文字。'];
  if (cues.length > 500) return ['本章字幕最多500条，请分章整理。'];
  if (cues.reduce((n, c) => n + (typeof c.text === 'string' ? c.text.length : 0), 0) > 100000)
    return ['本章字幕正文总量超过100000字，请分章整理。'];
  if (!Number.isFinite(duration) || duration <= 0) return ['先给本章镜头设置有效时长。'];
  if (duration >= 360000) return ['SRT章节时长须小于100小时。'];
  const ids = new Set();
  let previousEnd = -1;
  orderedCues(cues).forEach((cue, index) => {
    const prefix = `第${index + 1}条：`;
    if (typeof cue.id !== 'string' || !cue.id || ids.has(cue.id))
      issues.push(prefix + '字幕编号缺失或重复。');
    ids.add(cue.id);
    if (typeof cue.text !== 'string' || !cue.text.trim() || cue.text.length > 2000)
      issues.push(prefix + '请输入1–2000字的字幕。');
    else if (/[<>\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(cue.text))
      issues.push(prefix + '请使用纯文字，不要加入HTML标签或控制字符。');
    if (
      typeof cue.start !== 'number' ||
      typeof cue.end !== 'number' ||
      !Number.isFinite(cue.start) ||
      !Number.isFinite(cue.end) ||
      cue.start < 0 ||
      cue.end <= cue.start ||
      Math.round(cue.end * 1000) <= Math.round(cue.start * 1000)
    )
      issues.push(prefix + '起止时间须有效，结束至少比开始晚1毫秒。');
    else {
      if (cue.end > duration + 0.000001) issues.push(prefix + '结束超过章节时长。');
      if (Math.round(cue.start * 1000) < Math.round(previousEnd * 1000))
        issues.push(prefix + '与上一条重叠，请调整时间。');
      previousEnd = Math.max(previousEnd, cue.end);
    }
  });
  return issues;
}
export function captionSignature(project, chapterId) {
  return {
    basis: captionBasis(project, chapterId),
    soundMode: project.journey?.sound?.mode || null,
    audio: (project.journey?.soundTracks?.[chapterId] || []).map((track) => {
      const asset = project.entities.find((e) => e.id === (track.assetId || track.audioId));
      return {
        id: track.id,
        assetId: track.assetId || track.audioId || null,
        fileId: asset?.data.fileId || null,
        shotId: track.shotId || null,
        offset: track.offset ?? 0,
        start: track.start,
        end: track.end,
        muted: !!track.muted,
        ...(track.generatedFrom
          ? {
              generatedFrom: structuredClone(track.generatedFrom),
              needsReview: !!track.needsReview,
            }
          : {}),
      };
    }),
    cues: orderedCues(captionTrack(project, chapterId).cues).map(({ id, start, end, text }) => ({
      id,
      start,
      end,
      text,
    })),
  };
}
export function captionStatus(project, chapterId) {
  const track = captionTrack(project, chapterId),
    basis = captionBasis(project, chapterId),
    duration = basis.at(-1)?.end || 0,
    issues = captionIssues(track.cues, duration);
  return {
    track,
    duration,
    issues,
    confirmed:
      !issues.length &&
      stableJSON(track.confirmedSnapshot) === stableJSON(captionSignature(project, chapterId)),
    previouslyConfirmed: !!track.confirmedSnapshot,
  };
}
// SRT remains independent. These stricter checks apply only to the fixed burn-in preset.
export function burnCaptionStatus(project, chapterId) {
  const status = captionStatus(project, chapterId),
    issues = [...status.issues],
    cues = orderedCues(status.track.cues);
  if (!status.confirmed) issues.push('请先到节奏与声音，核对并确认当前这一版字幕文字和起止时间。');
  let previousEnd = -1;
  const aligned = cues.map((cue, index) => {
    const prefix = `第${index + 1}条：`,
      text = typeof cue.text === 'string' ? cue.text : '',
      lines = text.replace(/\r\n/g, '\n').split('\n');
    if (lines.length > 2 || lines.some((line) => !line.trim() || Array.from(line).length > 18))
      issues.push(prefix + '烧录版式最多2行，每行1–18个字符；请手动拆条或换行，正文不会自动改写。');
    if (
      /[{}\\<>\u2028\u2029]/.test(text) ||
      /\p{C}/u.test(text.replace(/\r\n/g, '\n').replace(/\n/g, ''))
    )
      issues.push(
        prefix +
          '烧录暂不支持ASCII花括号、反斜杠、HTML或控制字符。原稿和SRT仍保留，请明确修改后重新确认。',
      );
    const start_frame = Math.ceil(cue.start * 24 - 1e-7),
      end_frame = Math.floor(cue.end * 24 + 1e-7);
    if (!Number.isFinite(start_frame) || !Number.isFinite(end_frame) || end_frame <= start_frame)
      issues.push(prefix + '向内对齐24fps后不足1帧，请扩大显示时间。');
    else {
      if (start_frame < previousEnd) issues.push(prefix + '对齐后与上一条重叠。');
      if (start_frame < 0 || end_frame > Math.round(status.duration * 24))
        issues.push(prefix + '对齐后超出章节。');
      previousEnd = Math.max(previousEnd, end_frame);
    }
    return {
      id: cue.id,
      text: cue.text,
      start_frame,
      end_frame,
      start: start_frame / 24,
      end: end_frame / 24,
    };
  });
  return { ...status, issues: [...new Set(issues)], aligned };
}
export function srtTime(seconds) {
  const ms = Math.round(seconds * 1000);
  if (!Number.isSafeInteger(ms) || ms < 0 || ms >= 360000000) throw Error('SRT时间应小于100小时。');
  return `${String(Math.floor(ms / 3600000)).padStart(2, '0')}:${String(Math.floor(ms / 60000) % 60).padStart(2, '0')}:${String(Math.floor(ms / 1000) % 60).padStart(2, '0')},${String(ms % 1000).padStart(3, '0')}`;
}
export function exportSrt(project, chapterId) {
  const status = captionStatus(project, chapterId);
  if (status.issues.length) throw Error(status.issues.join(' '));
  if (!status.confirmed) throw Error('请先核对并确认这一版字幕文字和时间。');
  return orderedCues(status.track.cues)
    .map(
      (cue, index) =>
        `${index + 1}\r\n${srtTime(cue.start)} --> ${srtTime(cue.end)}\r\n${cue.text
          .trim()
          .replace(/\r\n?/g, '\n')
          .replace(/\n\s*\n+/g, '\n')
          .replace(/\n/g, '\r\n')}\r\n`,
    )
    .join('\r\n');
}
