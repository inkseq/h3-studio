// Quick Create attachments (h3-studio#726), frontend half: files go through the ordinary
// asset upload and the ordinary project actions. A photo put on a character, place or prop
// card is added exactly as the server's card pictures do (story_chat_images.py: the
// character's default look, the location's default version, the prop's imageIds), so it is
// a reference input for generation. Everything else lands in the story's library, ready in
// Works. Until the model can see attachments itself, the message tells it what was added.

export const ACCEPT = 'image/*,video/*,audio/*,.txt,.md,text/plain,text/markdown';
export const CARD_KINDS = { character: 'character', place: 'location', prop: 'prop' };

export function fileKind(file) {
  const type = file?.type || '';
  const name = (file?.name || '').toLowerCase();
  if (type.startsWith('image/')) return 'image';
  if (type.startsWith('video/')) return 'video';
  if (type.startsWith('audio/')) return 'audio';
  if (type.startsWith('text/') || name.endsWith('.txt') || name.endsWith('.md')) return 'text';
  return null;
}

// Cards a file can be put on: only photos go on cards; video and audio stay in the library.
export const cardTargets = (cards, kind) =>
  kind === 'image' ? cards.filter((card) => CARD_KINDS[card.kind]) : [];

const front = (ids, id) => [id, ...(ids || []).filter((item) => item !== id)];

// The card's data patch with the picture first (mirrors story_chat_images.card_actions).
export function cardPicturePatch(entity, imageId) {
  const data = structuredClone(entity.data || {});
  if (entity.type === 'character') {
    const looks = data.looks?.length
      ? data.looks
      : [{ id: 'main', name: '默认造型', description: '', gallery: {}, imageIds: [], version: 1 }];
    const look = looks.find((item) => item.id === data.defaultLookId) || looks[0];
    look.imageIds = front(look.imageIds, imageId);
    look.version = (look.version || 1) + 1;
    return { looks, defaultLookId: look.id };
  }
  if (entity.type === 'location') {
    const versions = data.versions?.length
      ? data.versions
      : [{ id: 'main', name: '默认', description: '', imageIds: [], version: 1 }];
    const version = versions.find((item) => item.id === data.defaultVersionId) || versions[0];
    version.imageIds = front(version.imageIds, imageId);
    version.version = (version.version || 1) + 1;
    return { versions, defaultVersionId: version.id };
  }
  return { imageIds: front(data.imageIds, imageId) };
}

// Project actions for one uploaded asset: into the story, and onto its card if one was chosen.
export function attachActions(project, { assetId, title, cardId }) {
  const imageId = 'asset-' + assetId;
  const actions = [{ op: 'asset.attach', asset_id: assetId, entity_id: imageId, title }];
  const card = cardId && project.entities.find((item) => item.id === cardId);
  if (card)
    actions.push({
      op: 'entity.update',
      entity_id: card.id,
      patch: { data: cardPicturePatch(card, imageId) },
    });
  return actions;
}

// What the message says was added, so the assistant can use it (until it sees the files).
export function attachmentNote(items, t) {
  if (!items.length) return '';
  const parts = items.map((item) =>
    item.cardName
      ? t('storyChat.note.onCard', { file: item.name, card: item.cardName })
      : t('storyChat.note.' + item.kind, { file: item.name }),
  );
  return t('storyChat.note.wrap', { list: parts.join(t('storyChat.note.separator')) });
}

// Text files become part of the message; the server takes at most `limit` characters.
export function joinText(current, addition, limit) {
  const joined = current ? current.replace(/\s+$/, '') + '\n\n' + addition : addition;
  return { text: joined.slice(0, limit), cut: joined.length > limit };
}

const READY_WAIT_MS = 90000;

// Upload one file and wait until the server can use it (video needs a moment to probe).
export async function uploadReady(api, file, projectId, { sleep, now = Date.now } = {}) {
  const wait = sleep || ((ms) => new Promise((resolve) => setTimeout(resolve, ms)));
  const clientId = 'chat-' + (globalThis.crypto?.randomUUID?.() || now().toString(36));
  let asset = await api.upload(file, projectId, clientId, 'upload:' + clientId);
  const until = now() + READY_WAIT_MS;
  while (asset?.status && asset.status !== 'ready') {
    if (asset.status === 'failed' || now() > until) throw Error('asset_not_ready');
    await wait(1500);
    asset = await api.asset(asset.asset_id);
  }
  if (!asset?.asset_id) throw Error('asset_not_ready');
  return asset.asset_id;
}
