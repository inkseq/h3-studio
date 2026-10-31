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
