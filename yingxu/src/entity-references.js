// Remove only live editing references. Jobs and their historical input snapshots stay intact.
// The caller commits one project snapshot, so one Undo restores every affected binding.
export function detachDeletedReferences(project, ids) {
  const changed = new Set();
  const removedLinks = new Set(
    project.links.filter((l) => ids.has(l.source) || ids.has(l.target)).map((l) => l.id),
  );
  const counts = { cast: 0, gallery: 0, selected: 0, guides: 0, tracks: 0, cover: 0, locations: 0 };
  for (const entity of project.entities) {
    if (ids.has(entity.id)) continue;
    const data = entity.data;
    let edited = false;
    if (Array.isArray(data.cast)) {
      const kept = data.cast.filter((c) => !ids.has(typeof c === 'string' ? c : c.characterId));
      counts.cast += data.cast.length - kept.length;
      edited ||= kept.length !== data.cast.length;
      data.cast = kept;
    }
    for (const look of data.looks || []) {
      let lookChanged = false;
      for (const [slot, assetId] of Object.entries(look.gallery || {}))
        if (ids.has(assetId)) {
          look.gallery[slot] = '';
          counts.gallery++;
          lookChanged = true;
        }
      if (lookChanged) {
        look.version = (look.version || 1) + 1;
        edited = true;
      }
    }
    if (ids.has(data.locationId)) {
      data.locationId = '';
      counts.locations++;
      edited = true;
    }
    if (Array.isArray(data.referenceAssetIds)) {
      const kept = data.referenceAssetIds.filter((id) => !ids.has(id));
      counts.locations += data.referenceAssetIds.length - kept.length;
      edited ||= kept.length !== data.referenceAssetIds.length;
      data.referenceAssetIds = kept;
    }
    if (ids.has(data.selectedAssetId)) {
      data.selectedAssetId = '';
      counts.selected++;
      edited = true;
    }
    if (Array.isArray(data.h3?.guides)) {
      const kept = data.h3.guides.filter((g) => !ids.has(g.media_id));
      counts.guides += data.h3.guides.length - kept.length;
      edited ||= kept.length !== data.h3.guides.length;
      data.h3.guides = kept;
    }
    for (const field of ['referenceRanges'])
      if (data[field])
        for (const id of Object.keys(data[field]))
          if (removedLinks.has(id)) {
            delete data[field][id];
            edited = true;
          }
    if (data.h3?.video_audio)
      for (const id of Object.keys(data.h3.video_audio))
        if (ids.has(id)) {
          delete data.h3.video_audio[id];
          edited = true;
        }
    if (ids.has(data.characterReview?.characterId)) delete data.characterReview;
    if (edited) {
      entity.version++;
      if (['scene', 'shot', 'generation'].includes(entity.type)) entity.status = 'review';
      changed.add(entity.id);
    }
  }
  const journey = project.journey;
  if (journey?.soundTracks)
    for (const [chapterId, tracks] of Object.entries(journey.soundTracks)) {
      if (!Array.isArray(tracks)) continue;
      // Keep source/shot IDs on surviving chapters: the sound editor flags the
      // missing binding and allows explicit repair. Never silently retime a track.
      counts.tracks += tracks.filter(
        (t) => ids.has(chapterId) || ids.has(t.assetId || t.audioId) || ids.has(t.shotId),
      ).length;
      if (ids.has(chapterId)) delete journey.soundTracks[chapterId];
    }
  if (journey?.captionTracks)
    for (const chapterId of Object.keys(journey.captionTracks))
      if (ids.has(chapterId)) delete journey.captionTracks[chapterId];
  if (ids.has(journey?.delivery?.coverAssetId)) {
    journey.delivery.coverAssetId = '';
    counts.cover++;
  }
  if (journey?.characterChange) {
    if (ids.has(journey.characterChange.characterId)) delete journey.characterChange;
    else if (Array.isArray(journey.characterChange.shotIds))
      journey.characterChange.shotIds = journey.characterChange.shotIds.filter(
        (id) => !ids.has(id),
      );
  }
  return { counts, changed: [...changed] };
}

export function deletionImpact(project, id) {
  const ids = new Set([id]);
  let added = true;
  while (added) {
    added = false;
    for (const e of project.entities)
      if (ids.has(e.parentId) && !ids.has(e.id)) {
        ids.add(e.id);
        added = true;
      }
  }
  return {
    ...detachDeletedReferences(structuredClone(project), ids).counts,
    descendants: ids.size - 1,
  };
}
