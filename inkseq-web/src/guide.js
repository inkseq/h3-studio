// 小墨's navigation targets (h3-studio#679): a suggestion only says where to go; the person
// makes the change. A target is an app link, so the same address works from chat, Telegram or
// email: /?view=workspace&project=<id>&entity=<shot>&field=<field>.
// cast is the editor's one cast slot: characters, place and props.
export const GUIDE_FIELDS = Object.freeze(['action', 'cast', 'frames', 'refs']);
const ID = /^[A-Za-z0-9_-]{1,160}$/;

export function guideHref({ projectId, shotId, field } = {}) {
  if (!ID.test(projectId || '')) return null;
  const params = new URLSearchParams({ view: 'workspace', project: projectId });
  if (shotId !== undefined) {
    if (!ID.test(shotId)) return null;
    params.set('entity', shotId);
  }
  if (field !== undefined) {
    if (shotId === undefined || !GUIDE_FIELDS.includes(field)) return null;
    params.set('field', field);
  }
  return '/?' + params;
}

export function guideField(search) {
  const field = new URLSearchParams(search).get('field');
  return GUIDE_FIELDS.includes(field) ? field : null;
}

const HIGHLIGHT_MS = 1600;

// Bring one editor field into view, focus its input and pulse it once. Returns the field, or
// null when it is not on screen yet (the caller may retry after the editor renders).
export function highlightGuideField(root, field, { reducedMotion = false } = {}) {
  if (!GUIDE_FIELDS.includes(field)) return null;
  const node = root.querySelector(`[data-guide-field="${field}"]`);
  if (!node) return null;
  node.scrollIntoView({ block: 'center', behavior: reducedMotion ? 'auto' : 'smooth' });
  const input = node.matches('input, textarea, select, button')
    ? node
    : node.querySelector('textarea, input, select, button');
  input?.focus({ preventScroll: true });
  node.classList.remove('guide-highlight');
  // Restart the pulse when the same field is targeted twice in a row.
  void node.offsetWidth;
  node.classList.add('guide-highlight');
  const view = root.defaultView || globalThis;
  view.setTimeout(() => node.classList.remove('guide-highlight'), HIGHLIGHT_MS);
  return node;
}
