/* Pure edits of the existing document. Cloud IO and execution stay in the controller. */
import { children, ancestors } from './store.js';
import { detachDeletedReferences } from './entity-references.js';
import {
  storyChapters,
  storyShots,
  patchStoryboardShot,
  storyboardDrop,
  storyboardView,
  newStoryboardEntity,
} from './storyboard-domain.js';

const find = (project, id) => project.entities.find((entity) => entity.id === id);
const list = (value) => (Array.isArray(value) ? value : []);
const owns = (value, key) => Object.hasOwn(value || {}, key);
const text = (value, max) => typeof value === 'string' && value.length <= max;
const plain = (value) => value && typeof value === 'object' && !Array.isArray(value);
const fields = (value, names) =>
  plain(value) && Object.keys(value).every((key) => names.includes(key));
const imageIds = (variant) =>
  list(variant.imageIds ?? Object.values(variant.gallery || {})).filter(Boolean);
const mark = (entity) => {
  entity.version++;
  entity.status = 'review';
};
const media = (project, id, types) =>
  typeof id === 'string' && types.includes(find(project, id)?.type);
const variants = (profile) =>
  profile.type === 'character'
    ? list(profile.data.looks)
    : profile.type === 'location'
      ? list(profile.data.versions).length
        ? profile.data.versions
        : [
            {
              id: 'legacy',
              name: '原场景',
              description: '',
              imageIds: list(profile.data.referenceAssetIds),
              version: 1,
            },
          ]
      : [
          {
            id: 'main',
            name: '主图',
            imageIds: list(profile.data.imageIds),
            version: profile.version,
          },
        ];
const variant = (profile, id) => variants(profile).find((value) => value.id === id);
const effectiveCast = (project, shot) => {
  const normalize = (values) =>
    list(values)
      .map((value) => (typeof value === 'string' ? { characterId: value, lookId: '' } : value))
      .filter(plain);
  if (shot.data.storyboard && owns(shot.data, 'cast')) return normalize(shot.data.cast);
  const inherited = list(find(project, shot.parentId)?.data.cast);
  return [
    ...new Map(
      normalize([...inherited, ...list(shot.data.cast)]).map((value) => [value.characterId, value]),
    ).values(),
  ];
};
const effectivePlace = (project, shot) =>
  owns(shot.data.storyboard, 'place')
    ? shot.data.storyboard.place
    : shot.data.locationId || find(project, shot.parentId)?.data.locationId
      ? {
          locationId: shot.data.locationId || find(project, shot.parentId)?.data.locationId,
          versionId: '',
        }
      : null;
const usesProfile = (project, shot, id) =>
  effectiveCast(project, shot).some((value) => value.characterId === id) ||
  effectivePlace(project, shot)?.locationId === id ||
  list(shot.data.storyboard?.propIds).includes(id) ||
  project.links.some((link) => link.source === id && link.target === shot.id);
function changedProfile(project, profile) {
  mark(profile);
  project.entities
    .filter((entity) => entity.type === 'shot' && usesProfile(project, entity, profile.id))
    .forEach(mark);
}
function saveVariant(profile, selected) {
  if (profile.type === 'location' && !list(profile.data.versions).length) {
    profile.data.versions = [selected];
    profile.data.defaultVersionId ||= selected.id;
  }
  if (profile.type === 'prop') profile.data.imageIds = selected.imageIds;
}
function shotView(project, shot, supplied) {
  const chapter = ancestors(project, shot.id).find((entity) => entity.type === 'chapter');
  const projected = storyboardView(project, {
    chapterId: chapter?.id,
    capabilities: supplied?.capabilities,
  });
  const value = projected.shots.find((value) => value.id === shot.id);
  // An explicit empty binding must not fall back to an inherited scene binding.
  value.cast = effectiveCast(project, shot).map((value) => ({
    id: value.characterId,
    variant: value.lookId || '',
  }));
  const place = effectivePlace(project, shot);
  value.place = place ? { id: place.locationId, variant: place.versionId || '' } : null;
  return { value, projected };
}
// Plain values: allowed fields, enums, lengths, numbers and switches.
function validShotScalars(patch) {
  if (owns(patch, 'aspect') && !['16:9', '9:16', '1:1'].includes(patch.aspect)) return false;
  for (const [key, max] of [
    ['title', 160],
    ['script', 120000],
    ['prompt', 24000],
  ])
    if (owns(patch, key) && !text(patch[key], max)) return false;
  if (owns(patch, 'mode') && !['FL', 'REF'].includes(patch.mode)) return false;
  if (owns(patch, 'quality') && !['draft', 'final'].includes(patch.quality)) return false;
  if (
    owns(patch, 'duration') &&
    !(Number.isFinite(patch.duration) && patch.duration > 0 && patch.duration <= 3600)
  )
    return false;
  for (const key of ['continuePrev', 'seedLock', 'keepAudio', 'newSeed'])
    if (owns(patch, key) && typeof patch[key] !== 'boolean') return false;
  return true;
}
// References: frames, refs, props, cast and place must name entities of the right kind.
function validShotLinks(project, patch) {
  for (const key of ['start', 'end'])
    if (
      owns(patch, key) &&
      patch[key] !== '' &&
      patch[key] !== null &&
      !media(project, patch[key], ['image'])
    )
      return false;
  for (const [key, types] of [
    ['refs', ['image', 'video', 'audio']],
    ['props', ['prop']],
  ])
    if (
      owns(patch, key) &&
      (!Array.isArray(patch[key]) ||
        patch[key].length > 100 ||
        new Set(patch[key]).size !== patch[key].length ||
        patch[key].some((id) => !media(project, id, types)))
    )
      return false;
  if (
    owns(patch, 'cast') &&
    (!Array.isArray(patch.cast) ||
      patch.cast.length > 100 ||
      new Set(patch.cast.map((value) => value?.id)).size !== patch.cast.length ||
      patch.cast.some(
        (value) =>
          !fields(value, ['id', 'variant']) ||
          find(project, value.id)?.type !== 'character' ||
          (value.variant && !variant(find(project, value.id), value.variant)),
      ))
  )
    return false;
  if (
    owns(patch, 'place') &&
    patch.place !== null &&
    (!fields(patch.place, ['id', 'variant']) ||
      find(project, patch.place.id)?.type !== 'location' ||
      (patch.place.variant && !variant(find(project, patch.place.id), patch.place.variant)))
  )
    return false;
  return true;
}
function validShotPatch(project, patch) {
  if (
    !fields(patch, [
      'title',
      'script',
      'prompt',
      'mode',
      'duration',
      'start',
      'end',
      'refs',
      'cast',
      'place',
      'props',
      'continuePrev',
      'quality',
      'seedLock',
      'aspect',
      'keepAudio',
      'newSeed',
    ])
  )
    return false;
  return validShotScalars(patch) && validShotLinks(project, patch);
}
// The first opt-in edit of a legacy shot copies its inherited cast, place, frames and refs
// into the edit, so the shot's storyboard is exact from then on.
function materializeLegacyContext(project, shot, clean) {
  clean.cast ??= effectiveCast(project, shot).map((value) => ({
    id: value.characterId,
    variant: value.lookId || '',
  }));
  const place = effectivePlace(project, shot);
  if (!owns(clean, 'place'))
    clean.place = place ? { id: place.locationId, variant: place.versionId || '' } : null;
  if (!owns(clean, 'start'))
    clean.start =
      project.links.find((link) => link.target === shot.id && link.role === 'firstFrame')?.source ||
      '';
  if (!owns(clean, 'end'))
    clean.end =
      project.links.find((link) => link.target === shot.id && link.role === 'lastFrame')?.source ||
      '';
  if (!owns(clean, 'refs'))
    clean.refs = project.links
      .filter(
        (link) =>
          link.target === shot.id &&
          ['reference', 'identity', 'motion', 'audio'].includes(link.role) &&
          media(project, link.source, ['image', 'video', 'audio']),
      )
      .map((link) => link.source);
}
function patchShot(project, shot, patch) {
  if (!validShotPatch(project, patch)) return false;
  if (
    !shot.data.storyboard &&
    shot.data.h3?.explicitReferences &&
    Object.keys(patch).some((key) => ['cast', 'place', 'props', 'continuePrev'].includes(key))
  )
    return false;
  const clean = { ...patch };
  for (const key of ['start', 'end']) if (clean[key] === null) clean[key] = '';
  if (
    !shot.data.storyboard &&
    !shot.data.h3?.explicitReferences &&
    Object.keys(clean).some((key) =>
      ['mode', 'cast', 'place', 'props', 'continuePrev'].includes(key),
    )
  )
    materializeLegacyContext(project, shot, clean);
  if (clean.place?.variant) {
    const location = find(project, clean.place.id);
    if (!list(location.data.versions).length) {
      saveVariant(location, variant(location, clean.place.variant));
      changedProfile(project, location);
    }
  }
  patchStoryboardShot(project, shot.id, clean);
  return { id: shot.id };
}
function clipAllowed(asset, start) {
  const duration = asset?.data.metadata?.source_duration ?? asset?.data.metadata?.duration;
  return (
    asset?.type === 'audio' &&
    Number.isFinite(start) &&
    start >= 0 &&
    start <= 360000 &&
    (!Number.isFinite(duration) || start + 3 <= duration + 0.01)
  );
}

// One handler per storyboard action; profile actions share the profile check below.
function projectAction(project, action) {
  if (!fields(action.patch, ['style']) || !text(action.patch.style, 12000)) return false;
  if ((project.journey?.storyboard?.style || '') === action.patch.style) return false;
  project.journey = {
    ...project.journey,
    storyboard: { ...project.journey?.storyboard, style: action.patch.style },
  };
  project.entities.filter((entity) => entity.type === 'shot').forEach(mark);
  return {};
}
// An optional title, when given, is non-blank text of at most 160 characters.
const badTitle = (value) => value !== undefined && (!text(value, 160) || !value.trim());
function addAction(project, action, { chapterId, view }) {
  if (badTitle(action.title) || badTitle(action.sceneTitle)) return false;
  const requested = action.sceneId === undefined ? null : find(project, action.sceneId);
  if (action.sceneId !== undefined && requested?.type !== 'scene') return false;
  // A shot added to a specific scene belongs to that scene's chapter (all chapters share one page).
  const existing =
    (requested && find(project, requested.parentId)) ||
    find(project, chapterId || view?.project.chapterId) ||
    children(project, null).find((entity) => entity.type === 'chapter');
  if (
    (existing && existing.type !== 'chapter') ||
    (requested && requested.parentId !== existing?.id)
  )
    return false;
  const chapter = existing || newStoryboardEntity(project, 'chapter', '第一章', null, {});
  const scene =
    requested ||
    children(project, chapter.id)
      .filter((entity) => entity.type === 'scene')
      .at(-1) ||
    newStoryboardEntity(project, 'scene', action.sceneTitle || '新场景', chapter.id, {
      script: '',
    });
  const added = newStoryboardEntity(project, 'shot', action.title || '新镜头', scene.id, {
    seconds: 5,
    prompt: '',
    script: '',
    cast: [],
    storyboard: {
      version: 1,
      mode: 'fl',
      quality: 'draft',
      place: null,
      propIds: [],
      firstFrameId: '',
      lastFrameId: '',
      continuePrevious: false,
      extraRefs: [],
    },
  });
  return { id: added.id };
}
function removeAction(project, action, { shot }) {
  if (shot?.type !== 'shot') return false;
  const parent = shot.parentId,
    ids = new Set([shot.id]);
  // Results and uploaded/profile assets are reusable entities, not disposable descendants.
  project.entities
    .filter((entity) => entity.parentId === shot.id)
    .forEach((entity) => {
      entity.parentId = parent;
      entity.version++;
    });
  detachDeletedReferences(project, ids);
  project.entities = project.entities.filter((entity) => !ids.has(entity.id));
  project.links = project.links.filter((link) => !ids.has(link.source) && !ids.has(link.target));
  delete project.layout.positions[shot.id];
  project.entities
    .filter((entity) => entity.type === 'shot' && entity.data.storyboard?.continuePrevious)
    .forEach(mark);
  return {};
}
function reorderAction(project, action, { view }) {
  const from = find(project, action.from),
    to = find(project, action.to);
  if (from?.type !== 'shot' || to?.type !== 'shot' || from.id === to.id) return false;
  const chapterOf = (entity) => ancestors(project, entity.id).find((row) => row.type === 'chapter');
  if (!chapterOf(from) || !chapterOf(to)) return false;
  // Shots can move across scenes and chapters; order is the story's reading order.
  const all = storyShots(project),
    oldIndex = all.indexOf(from),
    targetIndex = all.indexOf(to);
  if (from.parentId !== to.parentId) {
    const context = shotView(project, from, view).value;
    patchShot(project, from, { cast: context.cast, place: context.place });
    const oldParent = from.parentId;
    from.parentId = to.parentId;
    children(project, oldParent)
      .filter((entity) => entity.type === 'shot')
      .forEach((entity, index) => {
        entity.order = index;
      });
  }
  const siblings = children(project, to.parentId).filter(
    (entity) => entity.type === 'shot' && entity.id !== from.id,
  );
  const target = siblings.findIndex((entity) => entity.id === to.id);
  siblings.splice(target + (oldIndex < targetIndex ? 1 : 0), 0, from);
  siblings.forEach((entity, index) => {
    if (entity.order !== index) {
      entity.order = index;
      mark(entity);
    }
  });
  // Previous-shot identity changes even when another moved row kept its order.
  storyShots(project)
    .filter((entity) => entity.data.storyboard?.continuePrevious)
    .forEach(mark);
  return { id: from.id };
}
function moveToSceneAction(project, action, { view }) {
  const from = find(project, action.from),
    scene = find(project, action.scene);
  if (from?.type !== 'shot' || scene?.type !== 'scene' || from.parentId === scene.id) return false;
  const context = shotView(project, from, view).value;
  patchShot(project, from, { cast: context.cast, place: context.place });
  const oldParent = from.parentId;
  from.parentId = scene.id;
  from.order = children(project, scene.id).filter((e) => e.type === 'shot').length;
  mark(from);
  children(project, oldParent)
    .filter((entity) => entity.type === 'shot')
    .forEach((entity, index) => {
      if (entity.order !== index) {
        entity.order = index;
        mark(entity);
      }
    });
  storyShots(project)
    .filter((entity) => entity.data.storyboard?.continuePrevious)
    .forEach(mark);
  return { id: from.id };
}
function addChapterAction(project, action, _ctx) {
  if (
    !text(action.title, 160) ||
    !action.title.trim() ||
    !text(action.sceneTitle, 160) ||
    !action.sceneTitle.trim()
  )
    return false;
  const chapter = newStoryboardEntity(project, 'chapter', action.title.trim(), null, {});
  const last = storyChapters(project)
    .filter((row) => row.id !== chapter.id)
    .at(-1);
  chapter.order = (last?.order ?? 0) + 1;
  newStoryboardEntity(project, 'scene', action.sceneTitle.trim(), chapter.id, { script: '' });
  return { id: chapter.id };
}
function renameChapterAction(project, action, _ctx) {
  const chapter = find(project, action.id);
  if (chapter?.type !== 'chapter' || !text(action.title, 160) || !action.title.trim()) return false;
  if (chapter.title === action.title.trim()) return false;
  chapter.title = action.title.trim();
  chapter.version++;
  return { id: chapter.id };
}
function moveChapterAction(project, action, _ctx) {
  const chapters = storyChapters(project),
    index = chapters.findIndex((row) => row.id === action.id),
    next = index + (action.dir === -1 ? -1 : 1);
  if (index < 0 || ![-1, 1].includes(action.dir) || next < 0 || next >= chapters.length)
    return false;
  [chapters[index], chapters[next]] = [chapters[next], chapters[index]];
  chapters.forEach((row, n) => {
    if (row.order !== n) {
      row.order = n;
      row.version++;
    }
  });
  storyShots(project)
    .filter((entity) => entity.data.storyboard?.continuePrevious)
    .forEach(mark);
  return { id: action.id };
}
function updateGroupAction(project, action, { shot, view }) {
  if (shot?.type !== 'shot') return false;
  const { value, projected } = shotView(project, shot, view);
  let patch;
  if (action.type === 'update') patch = action.patch;
  if (action.type === 'drop') {
    if (!['smart', 'cast', 'refs', 'start', 'end', undefined].includes(action.slot)) return false;
    const item = action.profile
      ? { profile: projected.profiles.find((profile) => profile.id === action.profile) }
      : { asset: projected.assets.find((asset) => asset.id === action.asset) };
    if (!item.profile && !item.asset) return false;
    patch = storyboardDrop(
      value,
      item,
      action.slot === 'cast' ? 'smart' : action.slot || 'smart',
      projected,
    ).patch;
  }
  if (action.type === 'unref') patch = { refs: value.refs.filter((id) => id !== action.asset) };
  if (action.type === 'uncast')
    patch = { cast: value.cast.filter((value) => value.id !== action.id) };
  if (action.type === 'castVariant')
    patch = {
      cast: value.cast.map((value) =>
        value.id === action.id ? { ...value, variant: action.variant } : value,
      ),
    };
  if (action.type === 'placeVariant') {
    if (!value.place) return false;
    patch = { place: { ...value.place, variant: action.variant } };
  }
  if (action.type === 'unplace') patch = { place: null };
  if (action.type === 'unprop') patch = { props: value.props.filter((id) => id !== action.id) };
  return patch ? patchShot(project, shot, patch) : false;
}
function newProfileAction(project, action, _ctx) {
  if (
    !['character', 'location', 'prop'].includes(action.ptype) ||
    !text(action.name, 160) ||
    !action.name.trim()
  )
    return false;
  const data =
    action.ptype === 'character'
      ? {
          looks: [
            {
              id: 'main',
              name: '默认造型',
              description: '',
              gallery: {},
              imageIds: [],
              version: 1,
            },
          ],
          defaultLookId: 'main',
          voices: [],
          mainVoiceId: '',
        }
      : action.ptype === 'location'
        ? {
            versions: [{ id: 'main', name: '默认', description: '', imageIds: [], version: 1 }],
            defaultVersionId: 'main',
            ambient: null,
          }
        : { imageIds: [] };
  return { id: newStoryboardEntity(project, action.ptype, action.name.trim(), null, data).id };
}
function assetTitleAction(project, action, _ctx) {
  const asset = find(project, action.asset);
  if (
    !media(project, action.asset, ['image', 'video', 'audio']) ||
    typeof action.title !== 'string' ||
    !action.title.trim()
  )
    return false;
  asset.title = action.title;
  asset.version++;
  return { id: asset.id };
}
function dismissInboxAction(project, action, _ctx) {
  if (!media(project, action.asset, ['image', 'video', 'audio'])) return false;
  const asset = find(project, action.asset);
  asset.data.storyboardInbox = false;
  asset.version++;
  return { id: asset.id };
}
// A profile patch only sets fields this kind of profile has, and names real assets.
function validProfilePatch(project, profile, patch) {
  if (!fields(patch, ['name', 'desc', 'def', 'voice', 'ambient'])) return false;
  if (
    (owns(patch, 'name') && !text(patch.name, 160)) ||
    (owns(patch, 'desc') && !text(patch.desc, 24000))
  )
    return false;
  if (owns(patch, 'def') && (!variant(profile, patch.def) || profile.type === 'prop')) return false;
  const voice = list(profile.data.voices).find((value) => value.assetId === patch.voice);
  if (owns(patch, 'voice') && (profile.type !== 'character' || (patch.voice && !voice)))
    return false;
  return !(
    owns(patch, 'ambient') &&
    (profile.type !== 'location' ||
      (patch.ambient && !clipAllowed(find(project, patch.ambient), 0)))
  );
}
function profileAction(project, action, { profile }) {
  const patch = action.patch;
  if (!validProfilePatch(project, profile, patch)) return false;
  const voice = list(profile.data.voices).find((value) => value.assetId === patch.voice);
  if (owns(patch, 'name')) profile.title = patch.name;
  if (owns(patch, 'desc')) profile.description = patch.desc;
  if (owns(patch, 'def')) {
    const selected = variant(profile, patch.def);
    saveVariant(profile, selected);
    profile.data[profile.type === 'character' ? 'defaultLookId' : 'defaultVersionId'] = patch.def;
  }
  if (owns(patch, 'voice')) profile.data.mainVoiceId = voice?.id || '';
  if (owns(patch, 'ambient'))
    profile.data.ambient = patch.ambient
      ? {
          assetId: patch.ambient,
          clipStart:
            profile.data.ambient?.assetId === patch.ambient
              ? profile.data.ambient.clipStart || 0
              : 0,
        }
      : null;
  changedProfile(project, profile);
  return { id: profile.id };
}
function addVariantAction(project, action, { profile }) {
  if (
    profile.type === 'prop' ||
    !text(action.name || '新造型', 160) ||
    !(action.name || '新造型').trim() ||
    variants(profile).length >= 100
  )
    return false;
  if (profile.type === 'location' && !list(profile.data.versions).length)
    saveVariant(profile, variants(profile)[0]);
  const added = {
    id: 'variant-' + crypto.randomUUID(),
    name: (action.name || '新造型').trim(),
    description: '',
    imageIds: [],
    version: 1,
    ...(profile.type === 'character' ? { gallery: {} } : {}),
  };
  const field = profile.type === 'character' ? 'looks' : 'versions';
  profile.data[field] = [...list(profile.data[field]), added];
  changedProfile(project, profile);
  return { id: added.id };
}
function variantAction(project, action, { profile, selected }) {
  const patch = action.patch;
  if (!selected || !fields(patch, ['name', 'desc', 'voice'])) return false;
  if (profile.type === 'prop') {
    if (!fields(patch, ['desc']) || !text(patch.desc, 24000)) return false;
    profile.description = patch.desc;
    changedProfile(project, profile);
    return { id: profile.id };
  }
  if (
    (owns(patch, 'name') && (!text(patch.name, 160) || !patch.name.trim())) ||
    (owns(patch, 'desc') && !text(patch.desc, 24000))
  )
    return false;
  const voice = list(profile.data.voices).find((value) => value.assetId === patch.voice);
  if (owns(patch, 'voice') && (profile.type !== 'character' || (patch.voice && !voice)))
    return false;
  if (owns(patch, 'name')) selected.name = patch.name;
  if (owns(patch, 'desc')) selected.description = patch.desc;
  if (owns(patch, 'voice')) selected.voiceId = voice?.id || '';
  selected.version = (selected.version || 1) + 1;
  saveVariant(profile, selected);
  changedProfile(project, profile);
  return { id: profile.id };
}
function fileImageGroupAction(project, action, { profile, selected }) {
  if (!selected || !media(project, action.asset, ['image'])) return false;
  const images = imageIds(selected);
  if (action.type === 'coverImage' && !images.includes(action.asset)) return false;
  const next =
    action.type === 'unfileImage'
      ? images.filter((id) => id !== action.asset)
      : action.type === 'coverImage'
        ? [action.asset, ...images.filter((id) => id !== action.asset)]
        : [...new Set([...images, action.asset])];
  if (next.length > 100) return false;
  selected.imageIds = next;
  selected.version = (selected.version || 1) + 1;
  saveVariant(profile, selected);
  const asset = find(project, action.asset);
  asset.data.storyboardInbox = action.type === 'unfileImage';
  asset.version++;
  changedProfile(project, profile);
  return { id: profile.id };
}
function addVoiceGroupAction(project, action, { profile }) {
  if (profile.type !== 'character' || !media(project, action.asset, ['audio'])) return false;
  const voices = list(profile.data.voices),
    voice = voices.find((value) => value.assetId === action.asset);
  if (action.type === 'addVoice') {
    if (voice) return false;
    if (voices.length >= 100 || !clipAllowed(find(project, action.asset), 0)) return false;
    const added = { id: 'voice-' + crypto.randomUUID(), assetId: action.asset, clipStart: 0 };
    profile.data.voices = [...voices, added];
    profile.data.mainVoiceId ||= added.id;
    find(project, action.asset).data.storyboardInbox = false;
  } else if (action.type === 'removeVoice') {
    if (!voice) return false;
    profile.data.voices = voices.filter((value) => value.id !== voice.id);
    if (profile.data.mainVoiceId === voice.id)
      profile.data.mainVoiceId = profile.data.voices[0]?.id || '';
    list(profile.data.looks)
      .filter((value) => value.voiceId === voice.id)
      .forEach((value) => {
        value.voiceId = '';
        value.version++;
      });
    find(project, action.asset).data.storyboardInbox = true;
  } else {
    if (
      !voice ||
      !fields(action.patch, ['start']) ||
      !clipAllowed(find(project, action.asset), action.patch.start)
    )
      return false;
    voice.clipStart = action.patch.start;
  }
  changedProfile(project, profile);
  return { id: profile.id };
}
const SHOT_AND_PAGE_ACTIONS = {
  project: projectAction,
  add: addAction,
  remove: removeAction,
  reorder: reorderAction,
  moveToScene: moveToSceneAction,
  addChapter: addChapterAction,
  renameChapter: renameChapterAction,
  moveChapter: moveChapterAction,
  update: updateGroupAction,
  drop: updateGroupAction,
  unref: updateGroupAction,
  uncast: updateGroupAction,
  castVariant: updateGroupAction,
  placeVariant: updateGroupAction,
  unplace: updateGroupAction,
  unprop: updateGroupAction,
  newProfile: newProfileAction,
  assetTitle: assetTitleAction,
  dismissInbox: dismissInboxAction,
};
const PROFILE_ACTIONS = {
  profile: profileAction,
  addVariant: addVariantAction,
  variant: variantAction,
  fileImage: fileImageGroupAction,
  unfileImage: fileImageGroupAction,
  coverImage: fileImageGroupAction,
  addVoice: addVoiceGroupAction,
  removeVoice: addVoiceGroupAction,
  voice: addVoiceGroupAction,
};

export function applyStoryboardAction(project, action, { chapterId, view } = {}) {
  if (!plain(action) || typeof action.type !== 'string') return false;
  const shot = find(project, action.shot || action.id),
    profile = find(project, action.pid || action.id);
  if (Object.hasOwn(SHOT_AND_PAGE_ACTIONS, action.type))
    return SHOT_AND_PAGE_ACTIONS[action.type](project, action, { shot, profile, chapterId, view });
  if (!Object.hasOwn(PROFILE_ACTIONS, action.type)) return false;
  if (!['character', 'location', 'prop'].includes(profile?.type)) return false;
  const selected = variant(profile, action.vid);
  return PROFILE_ACTIONS[action.type](project, action, {
    shot,
    profile,
    selected,
    chapterId,
    view,
  });
}
