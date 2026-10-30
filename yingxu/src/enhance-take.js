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
