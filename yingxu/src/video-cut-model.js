export const cutFps = 24;
const epsilon = 1e-7;
export const videoBinding = (asset) => ({
  assetId: asset?.id || null,
  fileId: asset?.data.fileId || null,
  cloudAssetId: asset?.data.cloudAssetId || null,
  cloudArtifactId: asset?.data.cloudArtifactId || null,
});
export const boundVideoRange = (range, asset) =>
  !!range &&
  !!asset &&
  Object.entries(videoBinding(asset)).every(([key, value]) => range[key] === value);
export function rangeShapeIssue(range) {
  if (!range || typeof range !== 'object' || Array.isArray(range)) return '剪辑选段格式不正确。';
  if (
    Object.keys(range).sort().join(',') !== 'assetId,cloudArtifactId,cloudAssetId,end,fileId,start'
  )
    return '剪辑选段须保留完整的来源绑定。';
  if (typeof range.assetId !== 'string' || !range.assetId || range.assetId.length > 160)
    return '剪辑选段的素材标识无效。';
  for (const key of ['fileId', 'cloudAssetId', 'cloudArtifactId'])
    if (
      range[key] !== null &&
      (typeof range[key] !== 'string' || !range[key] || range[key].length > 200)
    )
      return '剪辑选段的文件绑定无效。';
  if (
    !Number.isFinite(range.start) ||
    !Number.isFinite(range.end) ||
    range.start < 0 ||
    range.end > 360000 ||
    range.end <= range.start
  )
    return '剪辑选段起止须为0–360000秒，出点晚于入点。';
  return '';
}
export function alignVideoRange(start, end) {
  if (
    typeof start !== 'number' ||
    typeof end !== 'number' ||
    !Number.isFinite(start) ||
    !Number.isFinite(end) ||
    start < 0 ||
    end <= start ||
    end > 360000
  )
    return null;
  const startFrame = Math.ceil(start * cutFps - epsilon),
    endFrame = Math.floor(end * cutFps + epsilon),
    frames = endFrame - startFrame;
  return {
    startFrame,
    endFrame,
    frames,
    start: startFrame / cutFps,
    end: endFrame / cutFps,
    duration: frames / cutFps,
  };
}
export function videoCut(
  shot,
  asset,
  duration = asset?.data.metadata?.duration ?? asset?.data.metadata?.duration_s,
) {
  const range = shot?.data.selectedVideoRange,
    requestedFrames = Math.floor(Number(shot?.data.seconds) * cutFps + 0.5);
  const fail = (issue) => ({ issue, range, requestedFrames });
  if (!asset || asset.type !== 'video' || asset.id !== shot?.data.selectedAssetId)
    return fail('先采用一段视频，再设置剪辑选段。');
  if (!asset.data.fileId || asset.data.missingFile)
    return fail('采用的视频文件缺失，请先恢复素材。');
  if (range) {
    const shape = rangeShapeIssue(range);
    if (shape) return fail(shape);
    if (!boundVideoRange(range, asset))
      return fail('采用视频或源文件已改变。旧选段保留，请重新选段或恢复从开头取片。');
  }
  if (!Number.isInteger(requestedFrames) || requestedFrames < 1)
    return fail('镜头时长不足一帧，请先调整时长。');
  const aligned = range
    ? alignVideoRange(range.start, range.end)
    : {
        startFrame: 0,
        endFrame: requestedFrames,
        frames: requestedFrames,
        start: 0,
        end: requestedFrames / cutFps,
        duration: requestedFrames / cutFps,
      };
  if (aligned.frames < 1) return fail('入点向上、出点向下对齐24fps后不足一帧，请扩大选段。');
  if (!Number.isFinite(duration) || duration <= 0)
    return { ...fail('视频时长尚未读取，请先打开源片预览。'), aligned };
  if (range && range.end > duration + epsilon)
    return { ...fail('选段出点超过当前源文件时长，请缩短选段。'), aligned };
  if (aligned.startFrame + requestedFrames > Math.floor(duration * cutFps + epsilon))
    return { ...fail('从该入点起视频不够长；请缩短镜头或选择更早入点。'), aligned };
  if (requestedFrames > aligned.frames)
    return { ...fail('选段短于镜头计划；扩大选段，或明确使用选段时长。'), aligned };
  return {
    issue: '',
    range,
    aligned,
    requestedFrames,
    sourceStart: aligned.start,
    sourceEnd: (aligned.startFrame + requestedFrames) / cutFps,
    unusedTailFrames: aligned.frames - requestedFrames,
  };
}
export function applyVideoRange(
  project,
  shotId,
  range,
  { duration, useRangeDuration = false } = {},
) {
  const shot = project.entities.find((e) => e.id === shotId && e.type === 'shot'),
    asset = project.entities.find((e) => e.id === shot?.data.selectedAssetId);
  const shape = rangeShapeIssue(range);
  if (shape) throw Error(shape);
  if (!boundVideoRange(range, asset)) throw Error('采用的视频已经改变，未保存旧来源选段。');
  const aligned = alignVideoRange(range.start, range.end);
  if (!aligned || aligned.frames < 1) throw Error('对齐24fps后选段不足一帧。');
  if (useRangeDuration && aligned.duration > 3600) throw Error('一个镜头最长3600秒，请拆分镜头。');
  const seconds = useRangeDuration ? aligned.duration : shot.data.seconds;
  const result = videoCut(
    { ...shot, data: { ...shot.data, seconds, selectedVideoRange: range } },
    asset,
    duration,
  );
  if (result.issue) throw Error(result.issue);
  if (
    !Number.isFinite(asset.data.metadata?.duration) &&
    !Number.isFinite(asset.data.metadata?.duration_s)
  )
    asset.data.metadata = { ...(asset.data.metadata || {}), duration };
  shot.data.selectedVideoRange = structuredClone(range);
  if (useRangeDuration) {
    // Before changing the edit duration, preserve the generator's inherited
    // value. A short cut must not silently turn a valid 5s request into <4s.
    if (shot.data.h3?.controls?.duration == null || shot.data.h3?.controls?.duration === '')
      shot.data.h3 = {
        ...(shot.data.h3 || {}),
        controls: { ...(shot.data.h3?.controls || {}), duration: shot.data.seconds },
      };
    shot.data.seconds = seconds;
  }
  return result;
}
