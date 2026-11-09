// Names and keys for the storyboard, the world and the library (story tabs audit, 2026-11-09).
// Pure helpers: what a card, a file or a drag is called, and what a key on a time bar does.

// Names the app or an older build gave files; the library shows where the file is used instead.
const DEFAULT_NAMES = new Set(['Generated result', '生成结果', '成片截帧', 'Frame']);
const DEFAULT_PATTERNS = [/^台词音轨\s*·\s*镜头\s*\d+$/, /^dialogue track\s*·\s*shot\s*\d+$/i];
export const defaultAssetName = (name) =>
  !String(name || '').trim() ||
  DEFAULT_NAMES.has(String(name).trim()) ||
  DEFAULT_PATTERNS.some((re) => re.test(String(name).trim()));

/** A shot's name: its title, or "Shot N" when the title is empty (never empty quotes). */
export function shotName(shot, index, t) {
  const title = String(shot?.title || '').trim();
  return title || t('shot.untitled', { n: index + 1 });
}

/** "6.0 s" for a known length, '' otherwise. */
export function secondsLabel(seconds, t) {
  return Number.isFinite(seconds) && seconds > 0
    ? t('library.seconds', { seconds: seconds.toFixed(1) })
    : '';
}

/**
 * What a library file is called: its own name, or where it is used ("Shot 5 · Take 2") when the
 * name is one the app made up. `full` adds the length: "Shot 5 · Take 2 · 6.0 s".
 */
export function assetLabel(asset, t, { full = false } = {}) {
  if (!asset) return '';
  const use = asset.use;
  const parts = [];
  if (use && defaultAssetName(asset.name)) {
    parts.push(t('library.shotN', { n: use.shot }));
    if (use.take) parts.push(t('library.takeN', { n: use.take }));
    if (use.role !== 'take') parts.push(t('library.role.' + use.role));
  } else
    parts.push(
      defaultAssetName(asset.name)
        ? t(asset.kind === 'video' ? 'library.generatedVideo' : 'library.untitledFile')
        : asset.name,
    );
  const length = full ? secondsLabel(asset.seconds, t) : '';
  if (length) parts.push(length);
  return parts.join(' · ');
}

/**
 * dnd-kit announcements in the reader's language. `nameOf(id)` turns a draggable or drop
 * target id into what the person sees (a shot number and title, a file or profile name).
 */
export function dragAnnouncements(t, nameOf) {
  const name = (id) => nameOf(String(id ?? '')) || t('dnd.item');
  return {
    onDragStart: ({ active }) => t('dnd.start', { item: name(active.id) }),
    // Over itself is where it started: keep the "picked up" line instead of saying so.
    onDragOver: ({ active, over }) =>
      over && String(over.id) === String(active.id)
        ? undefined
        : over
          ? t('dnd.over', { item: name(active.id), target: name(over.id) })
          : t('dnd.outside', { item: name(active.id) }),
    onDragEnd: ({ active, over }) =>
      over && String(over.id) === String(active.id)
        ? t('dnd.inPlace', { item: name(active.id) })
        : over
          ? t('dnd.dropped', { item: name(active.id), target: name(over.id) })
          : t('dnd.droppedNowhere', { item: name(active.id) }),
    onDragCancel: ({ active }) => t('dnd.cancelled', { item: name(active.id) }),
  };
}

/**
 * The name of a drag id: shot:<id>, seg:<id>, segto:<id>, sceneto:<id>, asset:<id>,
 * profile:<id>, pdrop:<id>, vdrop:<pid>:<vid>, voicedrop:<pid>, slot:<slot>:<shot>.
 * `story` holds { shots, assets, profiles, scenes, sceneName }.
 */
export function dragItemName(id, story, t) {
  const [kind, a = '', b = ''] = String(id).split(':');
  const shot = (shotId) => {
    const index = story.shots.findIndex((s) => s.id === shotId);
    if (index < 0) return '';
    const title = String(story.shots[index].title || '').trim();
    return title ? t('dnd.shot', { n: index + 1, title }) : t('shot.untitled', { n: index + 1 });
  };
  const profile = (pid) => story.profiles.find((p) => p.id === pid);
  if (kind === 'shot' || kind === 'seg' || kind === 'segto') return shot(a);
  if (kind === 'sceneto') {
    const scene = (story.scenes || []).find((s) => s.id === a);
    return scene ? t('dnd.scene', { scene: story.sceneName(scene) }) : '';
  }
  if (kind === 'asset')
    return assetLabel(
      story.assets.find((x) => x.id === a),
      t,
    );
  if (kind === 'profile' || kind === 'pdrop') return profile(a)?.name || '';
  if (kind === 'vdrop') {
    const p = profile(a),
      variant = p?.variants.find((v) => v.id === b);
    return p ? (variant?.name ? p.name + ' · ' + variant.name : p.name) : '';
  }
  if (kind === 'voicedrop') return profile(a) ? t('dnd.voiceOf', { name: profile(a).name }) : '';
  if (kind === 'slot') {
    const where = shot(b);
    return where
      ? t('dnd.slot.' + (['start', 'end', 'refs'].includes(a) ? a : 'cast'), { shot: where })
      : '';
  }
  return '';
}

// One frame of the 24 fps takes; Shift moves a whole second.
const FRAME = 1 / 24;

/**
 * What a key does on a time bar (take review marks, kept parts). The common review-tool keys:
 * I/O set in and out at the playhead, Enter makes the mark, 1–4 pick keep/cut/fix/reshoot,
 * ←/→ step a frame (Shift: a second), Home/End jump, K or Space play/pause, J back a second,
 * L play. Returns null for keys the bar leaves alone.
 */
export function timeBarKey(event) {
  if (event.ctrlKey || event.metaKey || event.altKey) return null;
  const key = event.key;
  if (key === 'ArrowLeft' || key === 'ArrowRight')
    return { type: 'step', by: (event.shiftKey ? 1 : FRAME) * (key === 'ArrowLeft' ? -1 : 1) };
  if (key === 'Home') return { type: 'seek', to: 'start' };
  if (key === 'End') return { type: 'seek', to: 'end' };
  if (key === 'Enter') return { type: 'enter' };
  if (key === 'Escape') return { type: 'cancel' };
  if (key === ' ' || key === 'k' || key === 'K') return { type: 'toggle' };
  if (key === 'j' || key === 'J') return { type: 'step', by: -1, pause: true };
  if (key === 'l' || key === 'L') return { type: 'play' };
  if (key === 'i' || key === 'I') return { type: 'in' };
  if (key === 'o' || key === 'O') return { type: 'out' };
  if (/^[1-4]$/.test(key)) return { type: 'choice', index: Number(key) - 1 };
  if (key === 'Delete' || key === 'Backspace') return { type: 'delete' };
  return null;
}

/** The range an in point and an out point (or the playhead) cover, in order. */
export function inOutRange(inPoint, outPoint, now) {
  if (!Number.isFinite(inPoint)) return null;
  const end = Number.isFinite(outPoint) ? outPoint : now;
  if (!Number.isFinite(end)) return null;
  return { start: Math.min(inPoint, end), end: Math.max(inPoint, end) };
}
