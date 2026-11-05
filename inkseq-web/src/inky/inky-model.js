// Inky (小墨, h3-studio#679): pure rules for what Inky shows. Inky only advises and points; the
// person makes every change. Items come from the server's Jev judgements; at most three are open.
import { guideHref } from '../guide.js';

export const INKY_KINDS = Object.freeze({
  continuity_mismatch: 'action',
  continuity_question: 'action',
  vague_action: 'action',
  emotion_not_shown: 'action',
  multiple_camera_moves: 'action',
  action_too_long: 'action',
  needs_keyframe: 'frames',
  upload_card: 'cast',
});
export const INKY_MAX_OPEN = 3;
// Advice about the action text: Inky can draft a new version of it.
// Not when two shots disagree: which one is right is the person's call.
export const isContinuity = (kind) => kind?.startsWith('continuity_');
export const canRewrite = (kind) => INKY_KINDS[kind] === 'action' && !isContinuity(kind);

const valid = (item) =>
  item &&
  typeof item.id === 'string' &&
  Object.hasOwn(INKY_KINDS, item.kind) &&
  typeof item.shot_id === 'string';

// Open items, newest judgement order kept, without ones the person dismissed or whose shot is gone.
export function openItems(items, { ignored = new Set(), shotIds = null } = {}) {
  return (Array.isArray(items) ? items : [])
    .filter(valid)
    .filter((item) => !ignored.has(item.id))
    .filter((item) => !shotIds || shotIds.has(item.shot_id))
    .slice(0, INKY_MAX_OPEN);
}

// Where "去修改" goes: the shot editor field the judgement is about.
export const itemField = (item) => item.field || INKY_KINDS[item.kind];
export const itemHref = (item, projectId) =>
  guideHref({ projectId, shotId: item.shot_id, field: itemField(item) });
// The earlier shot a continuity note compares with. Its card clash has none.
export const otherHref = (item, projectId) =>
  guideHref({ projectId, shotId: item.other_shot_id, field: 'action' });

// Mascot state for the corner button: quiet while the person types, notify when there is
// something new, otherwise idle.
export function cornerState({ count, typing, seen }) {
  if (typing) return 'quiet';
  if (count > seen) return 'notify';
  return 'idle';
}
