// How far a story is along the inky-help flow (script → world → storyboard → takes → edit),
// read from the same data the pages edit. Each stage gets done/total and the first unfinished
// item names the next step; Inky and the top bar show the same one.
import { beatClaims, sceneBeats } from './script-beats.js';
import { profileChecklist } from './story-bible.js';
import { planChecklist, planOf } from './shot-plan.js';

const count = (items, test) => items.filter(test).length;
const parentOf = (shot) => shot.entity?.parentId ?? shot.parentId;
const promptOf = (shot) => shot.prompt ?? shot.entity?.data?.prompt ?? '';
const shotTitle = (shot) => shot.entity?.title || shot.title || '';

/** Whether the story uses a profile in any shot (cast, place or prop), whatever is selected. */
export function profileUsed(profile, shots = []) {
  return shots.some(
    (shot) =>
      (shot.cast || []).some((c) => c.id === profile.id) ||
      shot.place?.id === profile.id ||
      (shot.props || []).includes(profile.id),
  );
}

// Words that say a body is not one ordinary body: several heads or bodies, joined twins,
// creatures. "三个独立的年轻男子…各有自己的身体" is three ordinary people.
const UNUSUAL_BODY =
  /[两三四五六七八九多](?:个|颗)(?:头|脑袋)|[两三四五六七八九多](?:个|副)?(?:身体|身子|躯干)|双头|多头|三头六臂|连体|共用(?:一|同一)?(?:个|副)?身体|半人半|蛇尾|翅膀|触手|怪物|机器人|\b(?:two|three|four|many|multiple)[- ](?:heads?|bodies|torsos|headed|bodied)\b|\bconjoined\b|\bmulti-?(?:head|body|headed|bodied)\b|\b(?:centaur|creature|monster|robot)s?\b/i;
const unusualCounts = (body) =>
  !!body && ((body.heads ?? 1) !== 1 || (body.arms ?? 2) !== 2 || (body.legs ?? 2) !== 2);

/**
 * Whether a character needs its body written down (heads, arms, legs). An ordinary human does
 * not: an empty body reads as "常规" unless its description, its looks or its group mark it
 * as more than one ordinary body.
 */
export function needsBody(profile, profiles = []) {
  if (profile.type !== 'character') return false;
  const words = [profile.desc, ...(profile.variants || []).map((v) => v.desc)].join(' ');
  if (UNUSUAL_BODY.test(words)) return true;
  const group = profile.bible?.groupId;
  return (
    !!group &&
    profiles.some(
      (other) =>
        other.id !== profile.id &&
        other.bible?.groupId === group &&
        unusualCounts(other.bible?.body),
    )
  );
}

/**
 * The World checklist as the flow counts it: profileChecklist with "used" read from the
 * story's shots and the body counted as done for an ordinary body. Each item gets `ordinary:
 * true` when it was counted done that way, so a page can say "常规".
 */
export function flowChecklist(profile, { shots = [], profiles = [] } = {}) {
  return profileChecklist(profile, { used: profileUsed(profile, shots) }).map((item) =>
    item.key === 'body' && !item.done && !needsBody(profile, profiles)
      ? { ...item, done: true, ordinary: true }
      : item,
  );
}

/** Verdicts of the chosen takes, when the caller has them: Map shotId -> { verdict, reshoot }. */
const reviewOf = (reviews, id) => (reviews instanceof Map ? reviews.get(id) : reviews?.[id]);
const reshootOf = (review) => !!review && (review.reshoot === true || review.verdict === 'redo');

/**
 * `state` is the storyboard view state: scenes (entities), profiles (profile views with
 * `bible`), shots (shot views with `entity`, `cast`, `place`, `props`, `chosen`).
 * `reviews` (optional) holds what review said about each shot's chosen take:
 * Map or object shotId -> { verdict: 'keep' | 'redo' | null, reshoot?: boolean }.
 * Returns { stages: {script, world, storyboard, edit}: {done, total}, next } where `next` is
 * { stage, key, id?, title?, count? } or null when everything listed is done.
 */
export function storyFlow(state, { reviews = null } = {}) {
  const scenes = state.scenes || [],
    allProfiles = state.profiles || [],
    profiles = allProfiles.filter((p) => ['character', 'location', 'prop'].includes(p.type)),
    shots = state.shots || [];
  const shotsOf = (scene) => shots.filter((shot) => parentOf(shot) === scene.id);
  // A scene is written when it is in beats and every beat is filmed by a shot.
  const sceneState = (scene) => {
    const beats = sceneBeats(scene);
    if (!beats.length) {
      const inShots =
        !(scene.data?.script || '').trim() && shotsOf(scene).some((s) => promptOf(s).trim());
      return inShots ? 'beatsFromShots' : 'splitBeats';
    }
    const claims = beatClaims(shotsOf(scene), beats);
    return beats.every((b) => claims.get(b.id).length > 0) ? 'done' : 'claimSceneBeats';
  };
  const sceneDone = (scene) => sceneState(scene) === 'done';
  const profileDone = (p) =>
    flowChecklist(p, { shots, profiles: allProfiles }).every((item) => item.done);
  const planned = (shot) => {
    const items = planChecklist(shot).filter((item) => item.key !== 'firstFrame');
    return items.every((item) => item.done);
  };
  const stages = {
    script: { done: count(scenes, sceneDone), total: scenes.length },
    world: { done: count(profiles, profileDone), total: profiles.length },
    storyboard: { done: count(shots, planned), total: shots.length },
    edit: { done: count(shots, (shot) => !!shot.chosen), total: shots.length },
  };
  const first = (list, test) => list.find((item) => !test(item));
  const shotStep = (stage, key, shot, extra = {}) => ({
    stage,
    key,
    id: shot.id,
    title: shotTitle(shot),
    ...extra,
  });

  // Once takes are chosen the work is reviewing and reshooting them, not the script again.
  const chosen = shots.filter((s) => s.chosen);
  if (chosen.length) return { stages, next: afterShooting(shots, chosen, reviews, shotStep) };

  let next = null;
  const scene = first(scenes, sceneDone);
  if (!scenes.length) next = { stage: 'script', key: 'writeScript' };
  else if (scene)
    next = { stage: 'script', key: sceneState(scene), id: scene.id, title: scene.title };
  else {
    const profile = first(profiles, profileDone);
    if (profile)
      next = { stage: 'world', key: 'finishProfile', id: profile.id, title: profile.name };
    else {
      const unplanned = first(shots, planned);
      if (unplanned)
        next = shotStep(
          'storyboard',
          planOf(unplanned.entity).beats.length ? 'planShot' : 'claimBeats',
          unplanned,
        );
      else if (shots.length) next = shotStep('storyboard', 'chooseTake', shots[0]);
    }
  }
  return { stages, next };
}

// The next step once some shots have a chosen take. With review data: reshoot what was marked,
// review what was not looked at, shoot what is missing, then cut. Without it nothing says the
// takes were looked at, so the step stays "review the chosen takes".
function afterShooting(shots, chosen, reviews, shotStep) {
  if (reviews) {
    const reshoot = chosen.filter((s) => reshootOf(reviewOf(reviews, s.id)));
    if (reshoot.length)
      return shotStep('storyboard', 'reshoot', reshoot[0], { count: reshoot.length });
    const unseen = chosen.filter((s) => !reviewOf(reviews, s.id)?.verdict);
    if (unseen.length)
      return shotStep('storyboard', 'reviewTakes', unseen[0], { count: unseen.length });
    const missing = shots.find((s) => !s.chosen);
    if (missing) return shotStep('storyboard', 'chooseTake', missing);
    return { stage: 'edit', key: 'cutFilm' };
  }
  return shotStep('storyboard', 'reviewTakes', chosen[0], { count: chosen.length });
}
