// How far a story is along the inky-help flow (script → world → storyboard → takes → edit),
// read from the same data the pages edit. Each stage gets done/total and the first unfinished
// item names the next step; Inky and the top bar show the same one.
import { sceneBeats } from './script-beats.js';
import { profileChecklist } from './story-bible.js';
import { planChecklist, planOf } from './shot-plan.js';

const count = (items, test) => items.filter(test).length;

/**
 * `state` is the storyboard view state: scenes (entities), profiles (profile views with
 * `bible`), shots (shot views with `entity`, `start`, `chosen`).
 * Returns { stages: {script, world, storyboard, edit}: {done, total}, next } where `next` is
 * { stage, key, id?, title? } or null when everything listed is done.
 */
export function storyFlow(state) {
  const scenes = state.scenes || [],
    profiles = (state.profiles || []).filter((p) =>
      ['character', 'location', 'prop'].includes(p.type),
    ),
    shots = state.shots || [];
  const sceneDone = (scene) => sceneBeats(scene).length > 0;
  const profileDone = (p) =>
    profileChecklist(p)
      .filter((item) => item.key !== 'used')
      .every((item) => item.done);
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
  let next = null;
  const scene = first(scenes, sceneDone);
  if (!scenes.length) next = { stage: 'script', key: 'writeScript' };
  else if (scene) next = { stage: 'script', key: 'splitBeats', id: scene.id, title: scene.title };
  else {
    const profile = first(profiles, profileDone);
    if (profile)
      next = { stage: 'world', key: 'finishProfile', id: profile.id, title: profile.name };
    else {
      const shot = first(shots, planned);
      if (shot)
        next = {
          stage: 'storyboard',
          key: planOf(shot.entity).beats.length ? 'planShot' : 'claimBeats',
          id: shot.id,
          title: shot.entity?.title || shot.title,
        };
      else {
        const waiting = first(shots, (s) => !!s.chosen);
        if (waiting)
          next = {
            stage: 'storyboard',
            key: 'chooseTake',
            id: waiting.id,
            title: waiting.entity?.title || waiting.title,
          };
        else if (shots.length) next = { stage: 'edit', key: 'cutFilm' };
      }
    }
  }
  return { stages, next };
}
