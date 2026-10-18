import { generatedTrackState } from './generated-sound-model.js';
import { videoCut } from './video-cut-model.js';
import { chapterShots, entityPath } from './delivery-model.js';
import { chapterTiming, trackPlacement } from './media-timing.js';
import { referenceSpecs } from './cloud-model.js';
import { shotReviewState, reviewLabels } from './production-model.js';
import { effectiveLook } from './character-model.js';
import { captionStatus } from './caption-model.js';

const baseName = (value) =>
  String(value || '')
    .split(/[\\/]/)
    .at(-1)
    .split(/[?#]/)[0]
    .replace(/[\u0000-\u001f]/g, '');
const mediaRecord = (asset) =>
  asset
    ? {
        id: asset.id,
        title: asset.title,
        kind: asset.type,
        file_name: baseName(asset.data.fileName),
        mime: asset.data.mime || null,
        bytes: asset.data.bytes ?? null,
        cloud_asset_id: asset.data.cloudAssetId || null,
        cloud_artifact_id: asset.data.cloudArtifactId || null,
        source_job_id: asset.data.sourceJobId || null,
        source_shot_version: asset.data.sourceShotVersion || null,
        simulation: !!asset.data.simulation,
        missing_file: !!asset.data.missingFile || !asset.data.fileId,
        metadata: Object.fromEntries(
          [
            'duration',
            'duration_s',
            'width',
            'height',
            'fps',
            'frame_count',
            'sample_rate',
            'channels',
          ]
            .filter((k) => Number.isFinite(asset.data.metadata?.[k]))
            .map((k) => [k, asset.data.metadata[k]]),
        ),
      }
    : null;
export function chapterHandover(project, chapterId, { cloud = true } = {}) {
  const chapter = project.entities.find((e) => e.id === chapterId && e.type === 'chapter');
  if (!chapter) throw Error('先选择要交接的章节。');
  const timeline = chapterTiming(chapterShots(project, chapterId)),
    assets = new Map(),
    find = (id) => project.entities.find((e) => e.id === id),
    collect = (id) => {
      const asset = find(id);
      if (!asset || !['image', 'video', 'audio'].includes(asset.type)) return null;
      assets.set(id, mediaRecord(asset));
      return id;
    };
  const shots = timeline.map((item, index) => {
    const shot = item.shot,
      cut = videoCut(shot, find(shot.data.selectedAssetId)),
      scene = find(shot.parentId),
      refs = referenceSpecs(project, shot.id),
      selected = collect(shot.data.selectedAssetId),
      direct = project.links.filter(
        (l) => l.target === shot.id && ['image', 'video', 'audio'].includes(find(l.source)?.type),
      );
    const references = direct.map((link) => {
      const range = shot.data.referenceRanges?.[link.id];
      return {
        asset_id: collect(link.source),
        purpose: link.role,
        range: range ? { start: range.start, end: range.end } : null,
      };
    });
    for (const ref of refs.references)
      if (!references.some((r) => r.asset_id === ref.entity.id && r.purpose === ref.purpose))
        references.push({
          asset_id: collect(ref.entity.id),
          purpose: ref.purpose,
          range: ref.range ? { start: ref.range.start, end: ref.range.end } : null,
        });
    const guides = (shot.data.h3?.guides || []).map((g) => ({
      asset_id: collect(g.media_id),
      missing_asset_id: find(g.media_id) ? null : g.media_id,
      time_seconds: g.time_seconds,
      use_audio: !!g.use_audio,
      source_range: g.source_range
        ? { start: g.source_range.start, end: g.source_range.end }
        : null,
    }));
    const characterIds = new Set(
      [...(scene?.data.cast || []), ...(shot.data.cast || [])].map((c) =>
        typeof c === 'string' ? c : c.characterId,
      ),
    );
    const cast = [...characterIds].map((id) => {
      const character = find(id),
        binding = effectiveLook(project, shot, id),
        look = character?.data.looks?.find((l) => l.id === binding?.lookId);
      return {
        character_id: id,
        name: character?.title || null,
        look_id: look?.id || null,
        look_name: look?.name || null,
      };
    });
    return {
      number: index + 1,
      shot_id: shot.id,
      shot_version: shot.version,
      title: shot.title,
      path: entityPath(project, shot.id),
      scene_id: shot.parentId,
      scene_title: scene?.title || '',
      planned_start: item.start,
      planned_end: item.end,
      planned_duration: item.end - item.start,
      source_in: cut.issue ? null : cut.sourceStart,
      source_out: cut.issue ? null : cut.sourceEnd,
      selected_range: shot.data.selectedVideoRange
        ? {
            asset_id: shot.data.selectedVideoRange.assetId,
            cloud_asset_id: shot.data.selectedVideoRange.cloudAssetId,
            cloud_artifact_id: shot.data.selectedVideoRange.cloudArtifactId,
            start: shot.data.selectedVideoRange.start,
            end: shot.data.selectedVideoRange.end,
          }
        : null,
      source_range_issue: find(selected)?.type === 'video' ? cut.issue : null,
      description: shot.description,
      prompt: shot.data.prompt || '',
      selection_type: selected ? 'media' : shot.data.uxReview?.chosen ? 'demo' : 'none',
      selected_asset_id: selected,
      selected_asset_title:
        find(selected)?.title ||
        (shot.data.uxReview?.chosen
          ? '交互示例 ' + shot.data.uxReview.chosen + '（非实际素材）'
          : ''),
      selected_file_name: baseName(find(selected)?.data.fileName),
      selected_simulation:
        !!find(selected)?.data.simulation || (!selected && !!shot.data.uxReview?.chosen),
      review_state: reviewLabels[shotReviewState(project, shot, { cloud })],
      review_issues: shot.data.uxReview?.issues || [],
      review_note: shot.data.uxReview?.note || '',
      cast,
      references,
      guides,
      reference_issues: refs.issues,
    };
  });
  const audioTracks = (project.journey?.soundTracks?.[chapterId] || []).map((original) => {
    const result = generatedTrackState(project, original),
      t = result.track;
    return {
      id: t.id,
      asset_id: collect(t.assetId || t.audioId),
      missing_asset_id: find(t.assetId || t.audioId) ? null : t.assetId || t.audioId,
      role: t.role,
      shot_id: t.shotId || null,
      offset: t.offset,
      source_start: t.start,
      source_end: t.end,
      gain: t.gain,
      muted: !!t.muted,
      placement: trackPlacement(t, timeline),
      ...(t.generatedFrom
        ? {
            generated_from: { ...t.generatedFrom },
            needs_review: !!t.needsReview,
            binding_issue: result.issue,
          }
        : {}),
    };
  });
  const captions = captionStatus(project, chapterId);
  return {
    format: 'yingxu-chapter-handover',
    version: 2,
    project_id: project.id,
    project_title: project.title,
    chapter_id: chapterId,
    chapter_title: chapter.title,
    project_updated_at: project.updatedAt || null,
    delivery_version: project.journey?.delivery?.version || '草案 v1',
    timing_basis: '逐镜按24fps取最近帧，再累计整帧；非ASR、EDL或已渲染成片时码。',
    duration: timeline.at(-1)?.end || 0,
    sound_mode: project.journey?.sound?.mode || '未决定',
    captions: {
      count: captions.track.cues?.length || 0,
      manually_confirmed: captions.confirmed,
      asr: false,
      burned_in: false,
    },
    shots,
    audio_tracks: audioTracks,
    assets: [...assets.values()],
    note: '仅稳定素材标识与元信息，不含文件本体、下载链接或访问授权。另交素材文件；未选择/缺失/模拟来源与返修状态均保留。',
  };
}
export function csvCell(value) {
  let text = String(value ?? '').replace(/\r\n?|\n/g, '\r\n');
  if (/^[\s]*[=+\-@]/.test(text) || /^[\t\r\n]/.test(text)) text = "'" + text;
  return '"' + text.replace(/"/g, '""') + '"';
}
export function shotListCsv(handover) {
  const columns = [
    ['章', 'chapter_title'],
    ['顺序', 'number'],
    ['场戏', 'scene_title'],
    ['镜头', 'title'],
    ['镜头ID', 'shot_id'],
    ['版本', 'shot_version'],
    ['计划入点秒', 'planned_start'],
    ['计划出点秒', 'planned_end'],
    ['时长秒', 'planned_duration'],
    ['实际源入点秒', 'source_in'],
    ['实际源出点秒', 'source_out'],
    ['源选段待核对', 'source_range_issue'],
    ['画面与动作', 'description'],
    ['生成提示词', 'prompt'],
    ['选择类型', 'selection_type'],
    ['采用素材ID', 'selected_asset_id'],
    ['采用素材', 'selected_asset_title'],
    ['文件名', 'selected_file_name'],
    ['模拟来源', 'selected_simulation'],
    ['审片状态', 'review_state'],
    ['返修分类', 'review_issues'],
    ['审片与返修备注', 'review_note'],
  ];
  return (
    '\ufeff' +
    [
      columns.map(([label]) => csvCell(label)).join(','),
      ...handover.shots.map((shot) =>
        columns
          .map(([, key]) =>
            csvCell(
              key === 'chapter_title'
                ? handover.chapter_title
                : Array.isArray(shot[key])
                  ? shot[key].join('；')
                  : shot[key],
            ),
          )
          .join(','),
      ),
    ].join('\r\n') +
    '\r\n'
  );
}
