// Drop deleted ids from a list field; returns how many were removed.
function keepLive(holder, field, ids, idOf = (value) => value) {
  if (!Array.isArray(holder?.[field])) return 0;
  const kept = holder[field].filter((value) => !ids.has(idOf(value)));
  const removed = holder[field].length - kept.length;
  holder[field] = kept;
  return removed;
}

// Clear deleted look images; a changed look gets a new version.
function detachGallery(looks, ids, counts) {
  let edited = false;
  for (const look of looks || []) {
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
  return edited;
}

// Delete keys of a map that point at removed things.
function dropKeys(map, removed) {
  let edited = false;
  for (const id of Object.keys(map || {}))
    if (removed.has(id)) {
      delete map[id];
      edited = true;
    }
  return edited;
}

// One entity's live references to deleted ids; returns whether anything changed.
function detachFromData(data, ids, removedLinks, counts) {
  const cast = keepLive(data, 'cast', ids, (c) => (typeof c === 'string' ? c : c.characterId)),
    refs = keepLive(data, 'referenceAssetIds', ids),
    guides = keepLive(data.h3, 'guides', ids, (g) => g.media_id);
  counts.cast += cast;
  counts.locations += refs;
  counts.guides += guides;
  let edited = cast + refs + guides > 0;
  edited = detachGallery(data.looks, ids, counts) || edited;
  for (const field of ['locationId', 'selectedAssetId'])
    if (ids.has(data[field])) {
      data[field] = '';
      counts[field === 'locationId' ? 'locations' : 'selected']++;
      edited = true;
    }
  edited = dropKeys(data.referenceRanges, removedLinks) || edited;
  edited = dropKeys(data.h3?.video_audio, ids) || edited;
  return edited;
}

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
    const edited = detachFromData(data, ids, removedLinks, counts);
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
