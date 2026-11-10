// One status per shot (h3-studio#892), from the shot's takes as the storyboard reads them
// (storyboard-domain.js shotView): the story cards, the story's head actions, batch enhance and
// the project list all use it, so a shot never reads one way in one place and another way in
// another.
//
// - empty      nothing to show: no take in the story (never generated, or all trashed)
// - queued     a take waits for a machine
// - generating a take is being made
// - failed     the newest take failed
// - pick       finished takes, none chosen (or a newer finished take than the chosen one)
// - chosen     a chosen take, with its quality
// - hd         the chosen take is a 1080P/2K enhanced copy
//
// Trash: a trashed take leaves the story (its library entity goes), so it is never live,
// never counted, chosen, enhanced or exported. Trashing the chosen take leaves the shot with no
// chosen take: it reads "pick" (or "empty"), and nothing else is chosen for it silently.
// Restoring a take does not choose it again.

export const WAITING = Object.freeze([
  'planned',
  'waiting_capacity',
  'queued',
  'claimed',
  'submitting',
  'submission_unknown',
]);
export const RUNNING = Object.freeze(['generating', 'running', 'collecting', 'checking']);
const inFlight = (take) => WAITING.includes(take?.status) || RUNNING.includes(take?.status);

/** A finished take that is still in the story (not trashed). */
export const liveTake = (take) =>
  ['done', 'succeeded'].includes(take?.status) && !!take.asset?.entity?.id;

/** The take the shot uses now: chosen, finished and not trashed. */
export const chosenTake = (shot) =>
  shot.versions.find((take) => take.id === shot.chosen && liveTake(take)) || null;

// The enhanced copies made from a take, by its library entity.
const copiesOf = (shot, take) =>
  shot.versions.filter(
    (copy) =>
      copy.enhanced && copy.sourceEntityId && copy.sourceEntityId === take.asset?.entity?.id,
  );

/** The 1080P/2K copy of the chosen take, if one is finished or being made, for a tier. */
export function enhancedCopy(shot, tier) {
  const chosen = chosenTake(shot);
  if (!chosen) return null;
  const copies = copiesOf(shot, chosen).filter(
    (copy) => !tier || copy.effectiveRequest?.tier === tier,
  );
  return copies.find(liveTake) || copies.find(inFlight) || null;
}

export function shotState(shot) {
  const latest = shot.versions.at(-1);
  if (WAITING.includes(latest?.status)) return { id: 'queued', take: latest };
  if (RUNNING.includes(latest?.status)) return { id: 'generating', take: latest };
  if (latest?.status === 'failed') return { id: 'failed', take: latest };
  const live = shot.versions.filter(liveTake);
  if (!live.length) return { id: 'empty', take: null };
  const chosen = chosenTake(shot);
  // A newer finished take than the one in use waits to be looked at.
  if (!chosen || (liveTake(latest) && latest.id !== chosen.id)) return { id: 'pick', take: chosen };
  return { id: chosen.enhanced ? 'hd' : 'chosen', take: chosen };
}

/**
 * What a story-wide "enhance all" would do at a tier (h3-studio#893): the chosen original of
 * each shot that has no finished or running copy at that tier, and every other shot with why
 * it is left out. Reasons: empty, busy, pick, hd (already enhanced), copy (a copy at this tier
 * exists or is being made), unavailable (enhance is off or the take can't be enhanced).
 */
export function enhanceAllPlan(shots, tier, enabled) {
  const targets = [],
    skipped = [];
  for (const shot of shots) {
    const latest = shot.versions.at(-1),
      chosen = chosenTake(shot);
    const reason = !enabled
      ? 'unavailable'
      : inFlight(latest) && !latest.enhanced
        ? 'busy'
        : !chosen
          ? shot.versions.some(liveTake)
            ? 'pick'
            : 'empty'
          : chosen.enhanced
            ? 'hd'
            : enhancedCopy(shot, tier)
              ? 'copy'
              : null;
    if (reason) skipped.push({ shot, reason });
    else targets.push({ shot, take: chosen });
  }
  return { targets, skipped };
}

/**
 * Of the targets in order, how many fit in the balance when each costs `credits[i]`.
 * Unknown prices or balance: null (nothing is promised).
 */
export function howManyFit(credits, balance) {
  if (!Number.isFinite(balance) || credits.some((c) => !Number.isFinite(c))) return null;
  let left = balance,
    count = 0;
  for (const cost of credits) {
    if (cost > left) break;
    left -= cost;
    count += 1;
  }
  return count;
}
