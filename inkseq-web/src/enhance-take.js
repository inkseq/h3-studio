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

// The service names every collected take "Generated result"; that is no name. An
// enhanced copy says which size it is, so it is never mistaken for its original.
const PLACEHOLDER_TITLE = 'Generated result';
const TIER_LABELS = { '1080p': '1080P', '2k': '2K' };

export function takeName(title, version, fallback) {
  const name = title && title !== PLACEHOLDER_TITLE ? title : fallback;
  const tier = version?.enhanced && TIER_LABELS[version.effectiveRequest?.tier];
  return tier ? name + ' · ' + tier : name;
}
