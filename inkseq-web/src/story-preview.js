// What a story looks like while it opens, from the copy the works list already read. Read-only:
// the page around it is inert, and nothing here is ever saved (owner, 2026-11-04).
const KEEP = 30;
const previews = new Map();
const keyFor = (account, id) => account + ':' + id;

export function rememberStoryPreview(account, id, project) {
  const key = keyFor(account, id);
  previews.delete(key);
  previews.set(key, project);
  if (previews.size > KEEP) previews.delete(previews.keys().next().value);
}

export function storyPreview(account, id, title) {
  const known = previews.get(keyFor(account, id));
  // Its own id, so nothing treats the preview as the story the link asks for.
  // Pictures load when the story arrives; they can only be read for the story that is open.
  return known
    ? {
        ...known,
        id: 'story-opening',
        entities: known.entities.map((entity) =>
          entity.data?.fileId
            ? { ...entity, data: { ...entity.data, fileId: undefined, thumb: undefined } }
            : entity,
        ),
      }
    : { id: 'story-opening', title: title || '', entities: [], journey: {}, placeholder: true };
}
