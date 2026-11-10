// Which takes can be enhanced to 1080P/2K (h3-studio#795).
export const ENHANCE_RECIPE = 'enhance-v1';

// Enhance is switched on for this service, and the take is finished, has its own library
// entity and is not itself an enhanced copy.
export function enhanceable(take, capabilities) {
  return !!(
    capabilities?.enhance?.enabled === true &&
    take?.status === 'done' &&
    take.asset?.entity?.id &&
    !take.enhanced
  );
}

// The take a batch would enhance for each selected shot: the one the shot uses now.
export function batchEnhanceTakes(shots, ids, capabilities, chosen) {
  const wanted = new Set(ids);
  return shots
    .filter((shot) => wanted.has(shot.id))
    .map((shot) => chosen(shot))
    .filter((take) => enhanceable(take, capabilities));
}

// The service names every collected take "Generated result"; that is no name. The size
// shows on the thumbnail (takeQuality), not in the name.
const PLACEHOLDER_TITLE = 'Generated result';
const TIER_LABELS = { '1080p': '1080P', '2k': '2K' };

export function takeName(title, fallback) {
  return title && title !== PLACEHOLDER_TITLE ? title : fallback;
}

// The size a take plays at: an enhanced copy by its tier, others by their short side
// (480P/768P/1080P; 1440 and up is 2K). Unknown sizes show nothing.
export function takeQuality(version) {
  const tier = version?.enhanced && TIER_LABELS[version.effectiveRequest?.tier];
  if (tier) return tier;
  const side = Math.min(Number(version?.width), Number(version?.height));
  if (!(side > 0)) return '';
  return side >= 1440 ? '2K' : side + 'P';
}

// "Use the sharper copy when it is done" (h3-studio#893): remembered in this browser per shot
// and source take, until the copy is chosen or the shot moves to another take.
const USE_KEY = 'inkseq.enhanceUseCopy';
const readUse = (storage) => {
  try {
    const value = JSON.parse(storage?.getItem(USE_KEY) || '[]');
    return Array.isArray(value) ? value.filter((row) => row && typeof row.e === 'string') : [];
  } catch {
    return [];
  }
};
const writeUse = (storage, rows) => {
  try {
    storage?.setItem(USE_KEY, JSON.stringify(rows.slice(-200)));
  } catch {
    /* A browser without storage just keeps the original chosen. */
  }
};
const store = () => (typeof localStorage === 'undefined' ? null : localStorage);

export function rememberUseCopy(project, shot, entity, storage = store()) {
  const rows = readUse(storage).filter((row) => !(row.p === project && row.s === shot));
  writeUse(storage, [...rows, { p: project, s: shot, e: entity }]);
}

/**
 * Finished copies to choose now: [{ shot, version }]. A shot whose chosen take is no longer
 * the source is forgotten (the person chose something else), and so is one whose copy is
 * chosen. `chosen(shot)` is the shot's live chosen take; `live(take)` says a take is finished.
 */
export function copiesToUse(project, shots, chosen, live, storage = store()) {
  const rows = readUse(storage),
    keep = [],
    out = [];
  for (const row of rows) {
    if (row.p !== project) {
      keep.push(row);
      continue;
    }
    const shot = shots.find((item) => item.id === row.s);
    if (!shot) continue;
    const current = chosen(shot);
    if (current?.asset?.entity?.id !== row.e) continue;
    const copy = shot.versions.find((v) => v.enhanced && v.sourceEntityId === row.e && live(v));
    if (copy) out.push({ shot: shot.id, version: copy.id });
    keep.push(row);
  }
  if (keep.length !== rows.length) writeUse(storage, keep);
  return out;
}
