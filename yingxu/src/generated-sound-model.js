import { videoCut } from './video-cut-model.js';

const find = (project, id) => project.entities.find((e) => e.id === id);
const flac = (asset) =>
  asset?.type === 'audio' &&
  ['audio/flac', 'audio/x-flac'].includes(asset.data.mime || asset.data.metadata?.mime);
export function chapterForShot(project, shotId) {
  const shot = find(project, shotId),
    scene = find(project, shot?.parentId),
    chapter = find(project, scene?.parentId);
  return chapter?.type === 'chapter' ? chapter.id : null;
}
export function generatedAudioOption(project, shotId) {
  const shot = find(project, shotId),
    video = find(project, shot?.data.selectedAssetId);
  if (video?.type !== 'video') return { issue: '先采用一个视频候选，再选择这次生成的声音。' };
  if (!video.data.sourceJobId || !video.data.cloudArtifactId)
    return { issue: '此视频没有可核对的生成任务来源。不会从MP4自动抽音。' };
  const audios = project.entities.filter(
    (e) => flac(e) && e.data.sourceJobId === video.data.sourceJobId && e.data.cloudArtifactId,
  );
  if (audios.length !== 1) {
    const removed = project.jobs?.find((job) => job.id === video.data.sourceJobId)
      ?.clientImportedAudioIds?.length;
    return {
      issue: audios.length
        ? '同一次任务有多份独立声音，尚不能自动确定对应文件。'
        : removed
          ? '这次生成声音已从本项目移除。可撤销删除，或从原任务下载后作为手动音轨导入；刷新不会恢复已明确移除的素材。'
          : '这次任务尚未提供独立FLAC声音；可刷新任务核对，不会从MP4自动抽音。',
      video,
    };
  }
  const audio = audios[0],
    cut = videoCut(shot, video);
  if (cut.issue) return { issue: cut.issue, video, audio };
  if (!audio.data.fileId || audio.data.missingFile)
    return { issue: '同次生成的独立声音文件缺失，请先恢复素材。', video, audio };
  const duration = audio.data.metadata?.duration ?? audio.data.metadata?.duration_s;
  if (!Number.isFinite(duration) || duration + 1e-3 < cut.sourceEnd)
    return { issue: '独立声音时长未知或不足以覆盖采用视频的选段，请核对原任务。', video, audio };
  const chapterId = chapterForShot(project, shotId),
    tracks = project.journey?.soundTracks?.[chapterId] || [];
  if (
    tracks.some(
      (t) =>
        !t.generatedFrom && !t.muted && (t.assetId === audio.id || t.fileId === audio.data.fileId),
    )
  )
    return {
      issue:
        '本章已有这份声音的手动音轨。请先将该手动音轨静音或移除，再加入跟随视频的生成音轨，避免重复叠音。',
      video,
      audio,
    };
  return {
    issue: '',
    shot,
    video,
    audio,
    cut,
    duration,
    generatedFrom: {
      jobId: video.data.sourceJobId,
      videoEntityId: video.id,
      videoArtifactId: video.data.cloudArtifactId,
      audioArtifactId: audio.data.cloudArtifactId,
    },
  };
}
export function generatedTrackState(project, track, { ignoreReview = false } = {}) {
  if (!track.generatedFrom) return { track, issue: '' };
  const fail = (issue, markReview = true) => ({
      track: markReview ? { ...track, needsReview: true } : track,
      issue,
    }),
    binding = track.generatedFrom;
  const shot = find(project, track.shotId),
    video = find(project, binding.videoEntityId),
    audio = find(project, track.assetId || track.audioId);
  if (
    !shot ||
    shot.data.selectedAssetId !== binding.videoEntityId ||
    video?.type !== 'video' ||
    video.data.cloudArtifactId !== binding.videoArtifactId ||
    video.data.sourceJobId !== binding.jobId
  )
    return fail(
      '采用视频已更换或被删除，原生成音轨保留待核对。请选择新候选的声音、解除关联或静音。',
    );
  if (
    !flac(audio) ||
    audio.data.cloudArtifactId !== binding.audioArtifactId ||
    audio.data.sourceJobId !== binding.jobId ||
    audio.data.fileId !== track.fileId ||
    audio.data.missingFile
  )
    return fail('生成声音的原文件或同任务绑定已改变，需重新核对、解除关联或静音。');
  if (track.needsReview && !ignoreReview)
    return fail('这条生成音轨尚未重新确认。请明确使用当前候选的声音，或解除关联／静音。');
  const cut = videoCut(shot, video),
    duration = audio.data.metadata?.duration ?? audio.data.metadata?.duration_s;
  if (cut.issue) return fail(cut.issue, false);
  if (!Number.isFinite(duration) || duration + 1e-3 < cut.sourceEnd)
    return fail('独立生成声音不足以覆盖当前视频选段，请缩短镜头或核对原任务。', false);
  return {
    issue: '',
    track: {
      ...track,
      start: cut.sourceStart,
      end: cut.sourceEnd,
      duration,
      offset: 0,
      needsReview: false,
    },
  };
}
export function reconcileGeneratedTracks(project) {
  for (const tracks of Object.values(project.journey?.soundTracks || {}))
    if (Array.isArray(tracks))
      for (let i = 0; i < tracks.length; i++)
        if (tracks[i].generatedFrom) tracks[i] = generatedTrackState(project, tracks[i]).track;
}
export function upsertGeneratedSound(project, shotId) {
  const option = generatedAudioOption(project, shotId),
    chapterId = chapterForShot(project, shotId);
  if (option.issue) throw Error(option.issue);
  if (!chapterId) throw Error('镜头所属章节不存在。');
  project.journey ||= {};
  project.journey.soundTracks ||= {};
  const tracks = project.journey.soundTracks[chapterId] || [];
  const existing = tracks.find((t) => t.shotId === shotId && t.generatedFrom);
  if (!existing && tracks.length >= 32)
    throw Error('本章已达32条音轨上限，请先整理或移除不需要的音轨。');
  const track = {
    id: existing?.id || 'sound-' + crypto.randomUUID(),
    assetId: option.audio.id,
    fileId: option.audio.data.fileId,
    role: 'generated',
    shotId,
    offset: 0,
    start: option.cut.sourceStart,
    end: option.cut.sourceEnd,
    duration: option.duration,
    gain: 0.7,
    muted: false,
    needsReview: false,
    generatedFrom: option.generatedFrom,
  };
  project.journey.soundTracks[chapterId] = existing
    ? tracks
        .map((t) => (t.id === existing.id ? track : t))
        .filter((t) => !t.generatedFrom || t.shotId !== shotId || t.id === existing.id)
    : [...tracks, track];
  project.journey.sound = { ...(project.journey.sound || {}), mode: 'mixed' };
  return track;
}
export function unlinkGeneratedSound(project, chapterId, trackId) {
  const track = project.journey?.soundTracks?.[chapterId]?.find((t) => t.id === trackId);
  if (!track?.generatedFrom) return false;
  const current = generatedTrackState(project, track, { ignoreReview: true });
  if (!current.issue) Object.assign(track, current.track);
  delete track.generatedFrom;
  delete track.needsReview;
  track.role = 'dialogue';
  return true;
}
