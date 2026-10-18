import { translate } from './i18n/locale.js';
import { effectiveLook } from './character-model.js';
import { chapterTiming, trackPlacement, validateRange } from './media-timing.js';
import { children, ancestors } from './store.js';
export const chapterShots = (p, id) =>
  children(p, id)
    .filter((e) => e.type === 'scene')
    .flatMap((e) => children(p, e.id).filter((s) => s.type === 'shot'));
export const entityPath = (p, id) =>
  [...ancestors(p, id), p.entities.find((e) => e.id === id)]
    .filter(Boolean)
    .map((e) => e.title)
    .join(' / ');
export function deliveryIssues(p, chapterId, locale = 'zh') {
  const t = (key, values) => translate(locale, key, values);
  const list = [],
    add = (id, stage, entityId, title, detail, field = '') =>
      list.push({
        id,
        stage,
        entityId,
        title,
        detail,
        field,
        path: entityId ? entityPath(p, entityId) : t('legacy.delivery1'),
      });
  if (!p.logline.trim())
    add('story', 1, null, t('legacy.delivery2'), t('legacy.delivery3'), 'logline');
  const chapter = p.entities.find((e) => e.id === chapterId),
    scenes = chapter ? children(p, chapterId).filter((e) => e.type === 'scene') : [],
    shots = chapterShots(p, chapterId);
  if (!chapter) add('chapter', 2, null, t('legacy.delivery4'), t('legacy.delivery5'));
  else if (!scenes.length)
    add('scene-' + chapter.id, 2, chapter.id, t('legacy.delivery6'), t('legacy.delivery7'));
  for (const scene of scenes) {
    if (!scene.data.script?.trim())
      add(
        'script-' + scene.id,
        2,
        scene.id,
        t('legacy.delivery8'),
        t('legacy.delivery9'),
        'script',
      );
    if (!children(p, scene.id).some((e) => e.type === 'shot'))
      add('shots-' + scene.id, 2, scene.id, t('legacy.delivery10'), t('legacy.delivery11'));
  }
  for (const shot of shots) {
    if (!shot.description.trim())
      add(
        'description-' + shot.id,
        4,
        shot.id,
        t('legacy.delivery12'),
        t('legacy.delivery13'),
        'description',
      );
    const asset = p.entities.find((e) => e.id === shot.data.selectedAssetId),
      review = shot.data.uxReview || {};
    const usable = shot.data.selectedAssetId
      ? asset?.data.fileId && !asset.data.missingFile
      : !!review.chosen;
    if (!usable) {
      add(
        'candidate-' + shot.id,
        5,
        shot.id,
        t('legacy.delivery14'),
        asset ? t('legacy.delivery15') : t('legacy.delivery16'),
      );
      list.at(-1).placeholderAllowed = true;
      list.at(-1).accepted = shot.data.acceptPlaceholder === true;
    }
    if (shot.status === 'review')
      add('review-' + shot.id, 5, shot.id, t('legacy.delivery17'), t('legacy.delivery18'));
    if (['revision', 'failed', 'retry', 'processing', 'cancelled'].includes(review.status))
      add('revision-' + shot.id, 5, shot.id, t('legacy.delivery19'), t('legacy.delivery20'));
    for (const link of p.links.filter((l) => l.target === shot.id)) {
      const ref = p.entities.find((e) => e.id === link.source);
      if (
        ref &&
        ['image', 'video', 'audio'].includes(ref.type) &&
        (!ref.data.fileId || ref.data.missingFile)
      )
        add(
          'missing-' + link.id,
          4,
          shot.id,
          t('legacy.delivery21'),
          t('legacy.referenceMissing', { title: ref.title }),
          'referenceRanges',
        );
      const range = shot.data.referenceRanges?.[link.id];
      if (
        ref?.data.fileId &&
        range &&
        (range.fileId !== ref.data.fileId || validateRange(range.start, range.end, range.duration))
      )
        add(
          'range-' + link.id,
          4,
          shot.id,
          t('legacy.delivery22'),
          t('legacy.referenceChanged', { title: ref.title }),
          'referenceRanges',
        );
    }
    const characters = [
        ...new Set(
          [
            ...(p.entities.find((e) => e.id === shot.parentId)?.data.cast || []),
            ...(shot.data.cast || []),
          ].map((c) => c.characterId),
        ),
      ],
      cast = characters.map((id) => effectiveLook(p, shot, id)).filter(Boolean);
    for (const binding of cast || []) {
      const char = p.entities.find((e) => e.id === binding.characterId),
        look = char?.data.looks?.find((l) => l.id === binding.lookId);
      if (!char || !look)
        add(
          'cast-' + shot.id + '-' + binding.characterId,
          3,
          shot.id,
          t('legacy.delivery23'),
          t('legacy.delivery24'),
        );
      else
        for (const [slot, assetId] of Object.entries(look.gallery || {})) {
          if (!assetId) continue;
          const image = p.entities.find((e) => e.id === assetId),
            key = 'gallery-' + char.id + '-' + look.id + '-' + slot;
          if ((!image?.data.fileId || image.data.missingFile) && !list.some((i) => i.id === key)) {
            add(
              key,
              3,
              char.id,
              t('legacy.delivery25'),
              t('legacy.lookImageMissing', {
                character: char.title,
                look: look.name,
                slot:
                  {
                    front: t('legacy.delivery26'),
                    side: t('legacy.delivery27'),
                    full: t('legacy.delivery28'),
                  }[slot] || slot,
              }),
            );
            list.at(-1).lookId = look.id;
          }
        }
    }
  }
  const mode = p.journey?.sound?.mode,
    tracks = p.journey?.soundTracks?.[chapterId] || [];
  if (!mode) add('sound', 6, null, t('legacy.delivery29'), t('legacy.delivery30'));
  else if (mode !== 'silent' && !tracks.length)
    add('sound-media', 6, null, t('legacy.delivery31'), t('legacy.delivery32'));
  const timeline = chapterTiming(shots),
    total = timeline.at(-1)?.end || 0;
  for (const track of tracks) {
    if (mode === 'silent' || track.muted) continue;
    const a = p.entities.find((e) => e.id === (track.assetId || track.audioId)),
      placement = trackPlacement(track, timeline),
      origin = timeline.find((s) => s.id === track.shotId);
    if (!a?.data.fileId || a.data.missingFile)
      add('sound-missing-' + track.id, 6, null, t('legacy.delivery33'), t('legacy.delivery34'));
    else if (
      track.fileId !== a.data.fileId ||
      validateRange(track.start, track.end, track.duration)
    )
      add('sound-range-' + track.id, 6, null, t('legacy.delivery35'), t('legacy.delivery36'));
    if (
      !placement ||
      placement.start >= total ||
      (origin && track.offset >= origin.end - origin.start)
    )
      add('sound-origin-' + track.id, 6, null, t('legacy.delivery37'), t('legacy.delivery38'));
  }
  return list;
}
