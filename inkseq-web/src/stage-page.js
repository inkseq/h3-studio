// What the World and Library pages show on each card, as plain data (StagePage.jsx draws it).
// Profiles are storyboard profile views ({ id, type, name, variants: [{ images }], voices,
// bible }); assets are storyboard assets ({ id, kind, name, entity }).
import { profileChecklist, voiceGaps } from './story-bible.js';

const list = (value) => (Array.isArray(value) ? value : []);
const shaOf = (asset) => {
  const sha = asset?.entity?.data?.metadata?.sha256;
  return typeof sha === 'string' && sha ? sha : '';
};

/** The profile's cover picture id: its default look's first picture, else any picture. */
export function coverId(p) {
  const look = list(p?.variants).find((v) => v.id === p.def) || list(p?.variants)[0];
  return list(look?.images)[0] || list(p?.variants).flatMap((v) => list(v.images))[0] || '';
}

/**
 * Plain facts and gaps for one card. `speaking` is how many places in the story give this
 * character lines (story-bible.js speakingPlaces). Returns
 * { looks, photos, voices, gaps: ['picture' | 'voice' | 'speaker'], done, total, items }.
 * A gap is only something the next step needs: every card needs a picture; a voice and a
 * speaker number only matter once the character has lines.
 */
export function cardFacts(p, { speaking = 0, used = false } = {}) {
  const photos = list(p?.variants).reduce((n, v) => n + list(v.images).length, 0);
  // Samples only: a voice chosen from our list alone is not one a shot can send.
  const voices = list(p?.voices).length || (p?.voice ? 1 : 0);
  const items = profileChecklist(p, { used });
  const gaps = [...(photos ? [] : ['picture']), ...voiceGaps(p, speaking)];
  return {
    looks: list(p?.variants).length,
    photos,
    voices,
    gaps,
    done: items.filter((item) => item.done).length,
    total: items.length,
    items,
  };
}

/**
 * Cards whose cover picture is the same picture as other cards' of the same kind (one file
 * filed on several cards, or the same file uploaded again). Returns Map(profileId →
 * { n, index }) for shared covers only: n cards share it and this one is index-th in order.
 */
export function sharedCovers(profiles, assetOf = () => null) {
  const keyOf = new Map();
  for (const p of list(profiles)) {
    const id = coverId(p);
    if (!id) continue;
    keyOf.set(p.id, p.type + ':' + (shaOf(assetOf(id)) || 'id:' + id));
  }
  const byKey = new Map();
  for (const p of list(profiles)) {
    const key = keyOf.get(p.id);
    if (!key) continue;
    if (!byKey.has(key)) byKey.set(key, []);
    byKey.get(key).push(p.id);
  }
  const out = new Map();
  for (const ids of byKey.values())
    if (ids.length > 1) ids.forEach((id, index) => out.set(id, { n: ids.length, index }));
  return out;
}

/**
 * Where to crop a group picture so the card shows its own person: the picture is cut into
 * `n` equal columns and the index-th one is shown from the top, as large as fills a frame of
 * aspect `frame` (width / height). Returns the <img> box as percentages of the frame
 * ({ left, top, width, height }), or null when the picture's size is unknown.
 */
export function memberCrop({ n, index, width, height, frame = 3 / 4 }) {
  if (!(n > 1) || !(width > 0) || !(height > 0) || !(frame > 0)) return null;
  const aspect = width / height;
  // Window in picture units: one column wide, as tall as the frame's shape asks.
  let wx = 1 / n,
    wy = (wx * aspect) / frame;
  if (wy > 1) {
    wy = 1;
    wx = frame / aspect;
  }
  const centre = (Math.min(Math.max(index, 0), n - 1) + 0.5) / n;
  const x = Math.min(Math.max(centre - wx / 2, 0), 1 - wx);
  const round = (v) => Math.round(v * 1000) / 10;
  return { left: round(-x / wx), top: 0, width: round(1 / wx), height: round(1 / wy) };
}

/** Which cards use each file: Map(assetId → [{ name, type }]), in profile order. */
export function fileOwners(profiles) {
  const out = new Map();
  for (const p of list(profiles)) {
    const ids = new Set([
      ...list(p.variants).flatMap((v) => list(v.images)),
      ...list(p.voices).map((v) => v?.asset),
      p.voice,
      p.ambient,
    ]);
    for (const id of ids) {
      if (!id) continue;
      if (!out.has(id)) out.set(id, []);
      if (!out.get(id).some((o) => o.name === p.name))
        out.get(id).push({ name: p.name, type: p.type });
    }
  }
  return out;
}

const FILE_NAME =
  /\.(png|jpe?g|webp|gif|heic|avif|bmp|tiff?|mp4|mov|webm|mkv|wav|mp3|m4a|aac|ogg|flac)$/i;
/** A title that is only the uploaded file's name (front-full.v3.png), not one a person gave. */
export const looksLikeFileName = (name) => FILE_NAME.test(String(name || '').trim());

/**
 * Copies of the same file (same content fingerprint) among these assets: Map(assetId →
 * { n, index, key }) for files that appear more than once.
 */
export function duplicateFiles(assets) {
  const byKey = new Map();
  for (const a of list(assets)) {
    const sha = shaOf(a);
    if (!sha) continue;
    const key = a.kind + ':' + sha;
    if (!byKey.has(key)) byKey.set(key, []);
    byKey.get(key).push(a.id);
  }
  const out = new Map();
  for (const [key, ids] of byKey)
    if (ids.length > 1) ids.forEach((id, index) => out.set(id, { n: ids.length, index, key }));
  return out;
}

/** Library order: files a shot uses in shot and take order, copies of one file side by side. */
export function libraryOrder(assets, duplicates = new Map()) {
  const rows = list(assets).map((a, at) => ({ a, at }));
  const first = new Map();
  for (const { a, at } of rows) {
    const key = duplicates.get(a.id)?.key;
    if (key && !first.has(key)) first.set(key, at);
  }
  const place = ({ a, at }) => {
    const key = duplicates.get(a.id)?.key;
    return key ? first.get(key) + at / 1e6 : at;
  };
  return rows
    .sort(
      (x, y) =>
        (x.a.use?.shot ?? Infinity) - (y.a.use?.shot ?? Infinity) ||
        (x.a.use?.take ?? 0) - (y.a.use?.take ?? 0) ||
        place(x) - place(y),
    )
    .map(({ a }) => a);
}
