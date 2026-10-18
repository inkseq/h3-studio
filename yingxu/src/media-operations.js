import { translate } from './i18n/locale.js';
import { validateLink } from './store.js';
import { resolveCanvasPositions } from './canvas-layout.js';
import { addLook, editLook, gallerySlots } from './character-model.js';
import { captureWorkspace, assertWorkspace } from './workspace-context.js';
import { setLocationReferences } from './location-model.js';
import { effectiveReferenceCount } from './cloud-model.js';

const mediaIdentity = (entity) =>
  JSON.stringify([
    entity?.type,
    entity?.data.fileId ?? null,
    entity?.data.cloudAssetId ?? null,
    entity?.data.cloudArtifactId ?? null,
  ]);
const find = (store, id) => store.getState().project.entities.find((e) => e.id === id);
const requireCommit = (store, ok) => {
  if (!ok) throw Error(store.getState().notice || '这次素材关联未保存，请核对当前作品后重试。');
};
async function guardedUpload(store, context, unchanged, message, upload) {
  // Latch a relevant change, even if the user later undoes it back to the old
  // ID. This subscription lasts only for this upload; no entity stays locked.
  let changed = false;
  const unsubscribe = store.subscribe(() => {
    if (!unchanged()) changed = true;
  });
  try {
    const result = await upload();
    assertWorkspace(store, context);
    if (changed || !unchanged()) throw Error(message);
    return result;
  } finally {
    unsubscribe();
  }
}

export async function replaceEntityMedia({ store, entityId, file, upload }) {
  const context = captureWorkspace(store),
    target = find(store, entityId);
  if (!target || !['image', 'video', 'audio'].includes(target.type))
    throw Error('原素材已删除，请重新选择。');
  const identity = mediaIdentity(target),
    result = await guardedUpload(
      store,
      context,
      () => {
        const live = find(store, entityId);
        return !!live && identity === mediaIdentity(live);
      },
      '上传期间这份素材已被替换或删除；保留你刚才的选择，请重新选择文件。',
      () => upload(file, target.type),
    );
  // Merge only newly uploaded media fields. Keep description, notes and any
  // unrelated metadata edited while the file was being transferred.
  requireCommit(
    store,
    store.updateEntity(entityId, {
      data: {
        cloudAssetId: null,
        cloudArtifactId: null,
        cloudContentPath: null,
        sourceJobId: null,
        sourceShotId: null,
        sourceShotVersion: null,
        sourceHash: null,
        oldVersion: false,
        simulation: false,
        metadata: {},
        ...result.data,
      },
    }),
  );
  return result;
}

export async function uploadLookImage({ store, characterId, lookId, slot, file, upload }) {
  const context = captureWorkspace(store),
    character = find(store, characterId),
    look = character?.data.looks?.find((l) => l.id === lookId);
  if (!look || !Object.hasOwn(gallerySlots, slot)) throw Error('造型已删除，请重新选择后上传。');
  const previous = look.gallery?.[slot] || '',
    result = await guardedUpload(
      store,
      context,
      () => {
        const live = find(store, characterId)?.data.looks?.find((l) => l.id === lookId);
        return !!live && (live.gallery?.[slot] || '') === previous;
      },
      '上传期间这张造型参考已另选或删除；保留你的新选择，没有覆盖。',
      () => upload(file, 'image'),
    );
  const owner = find(store, characterId),
    current = owner?.data.looks?.find((l) => l.id === lookId);
  if (!current) throw Error('造型已删除，请重新选择后上传。');
  if ((current.gallery?.[slot] || '') !== previous)
    throw Error('上传期间这张造型参考已另选；保留你的新选择，没有覆盖。');
  const imageId = `image-${crypto.randomUUID()}`;
  requireCommit(
    store,
    store.editProject((p) => {
      p.entities.push({
        id: imageId,
        type: 'image',
        title: `${owner.title} · ${current.name} · ${gallerySlots[slot]}`.slice(0, 160),
        description: '人物造型参考',
        parentId: null,
        order:
          Math.max(-1, ...p.entities.filter((e) => e.parentId === null).map((e) => e.order)) + 1,
        version: 1,
        status: 'draft',
        data: result.data,
      });
      p.layout.positions[imageId] = resolveCanvasPositions(p.entities, p.layout.positions)[imageId];
      const edited = editLook(p, characterId, lookId, { gallery: { [slot]: imageId } });
      if (!edited.ok) throw Error(edited.error);
    }),
  );
  return imageId;
}

export const freestyleRoles = (kind, role) =>
  kind === 'image' && role === 'reference'
    ? ['reference', 'identity']
    : kind === 'video'
      ? ['motion', 'reference']
      : kind === 'audio'
        ? ['audio', 'reference']
        : [role];
export function freestyleInputIds(project, shotId, { kind, role, guide = false }) {
  const shot = project.entities.find((e) => e.id === shotId);
  return guide
    ? (shot?.data.h3?.guides || []).map((g) => g.media_id)
    : [
        ...new Set(
          project.links
            .filter(
              (l) =>
                l.target === shotId &&
                freestyleRoles(kind, role).includes(l.role) &&
                project.entities.some((e) => e.id === l.source && e.type === kind),
            )
            .map((l) => l.source),
        ),
      ];
}

export async function uploadFreestyleFiles({
  store,
  shotId,
  files,
  kind,
  role,
  guide = false,
  maximum,
  countAllReferences = false,
  upload,
  locale = 'zh',
}) {
  const context = captureWorkspace(store),
    shot = find(store, shotId),
    recipe = shot?.data.h3?.recipeId ?? null,
    profile = shot?.data.h3?.deploymentProfileId ?? null;
  if (
    !shot ||
    !['shot', 'generation'].includes(shot.type) ||
    (shot.type === 'generation' && shot.data.recipe !== 'video')
  )
    throw Error(translate(locale, 'h3more.upload.openShot'));
  const draftMode = shot.data.h3?.inputMode ?? null;
  const unchanged = () => {
    const live = find(store, shotId);
    return (
      !!live &&
      live.type === shot.type &&
      (live.data.h3?.recipeId ?? null) === recipe &&
      (live.data.h3?.inputMode ?? null) === draftMode &&
      (live.data.h3?.deploymentProfileId ?? null) === profile
    );
  };
  const check = (count) => {
    assertWorkspace(store, context);
    if (!unchanged()) throw Error(translate(locale, 'h3more.upload.changed'));
    const used = countAllReferences
      ? effectiveReferenceCount(store.getState().project, shotId, kind)
      : freestyleInputIds(store.getState().project, shotId, { kind, role, guide }).length;
    if (!Number.isInteger(maximum) || maximum < 1 || used + count > maximum)
      throw Error(
        translate(locale, 'h3more.upload.limit', {
          maximum,
          inherited: countAllReferences ? translate(locale, 'h3more.upload.inherited') : '',
        }),
      );
  };
  check(files.length);
  for (const file of files) {
    check(1);
    const result = await guardedUpload(
      store,
      context,
      unchanged,
      translate(locale, 'h3more.upload.changed'),
      () => upload(file, kind || undefined),
    );
    check(1);
    const id = `${result.type}-${crypto.randomUUID()}`;
    requireCommit(
      store,
      store.editProject((p) => {
        const live = p.entities.find((e) => e.id === shotId);
        p.entities.push({
          id,
          type: result.type,
          title: file.name.slice(0, 160),
          description: '',
          parentId: null,
          order:
            Math.max(-1, ...p.entities.filter((e) => e.parentId === null).map((e) => e.order)) + 1,
          version: 1,
          status: 'draft',
          data: result.data,
        });
        p.layout.positions[id] = resolveCanvasPositions(p.entities, p.layout.positions)[id];
        if (guide)
          live.data.h3 = {
            ...live.data.h3,
            guides: [
              ...(live.data.h3?.guides || []),
              { media_id: id, time_seconds: 0, use_audio: false },
            ],
          };
        else {
          const checked = validateLink(p, id, shotId, role);
          if (!checked.ok) throw Error(checked.error);
          p.links.push({ id: `link-${crypto.randomUUID()}`, source: id, target: shotId, role });
        }
        live.version++;
        live.status = 'review';
      }),
    );
  }
}

export async function uploadLibraryFiles({ store, files, upload }) {
  const context = captureWorkspace(store),
    added = [];
  for (const file of files) {
    assertWorkspace(store, context);
    const result = await upload(file);
    assertWorkspace(store, context);
    const id = store.addEntity(result.type, null, {
      title: file.name.slice(0, 160),
      data: result.data,
    });
    requireCommit(store, id);
    added.push(id);
  }
  return added;
}

export async function uploadLocationImage({ store, locationId, file, upload }) {
  const context = captureWorkspace(store),
    location = find(store, locationId);
  if (location?.type !== 'location') throw Error('这个地点已经不存在，请重新选择。');
  const result = await guardedUpload(
    store,
    context,
    () => find(store, locationId)?.type === 'location',
    '上传期间地点已删除；没有将参考写入其他地点。',
    () => upload(file, 'image'),
  );
  const id = `image-${crypto.randomUUID()}`;
  requireCommit(
    store,
    store.editProject((project) => {
      const current = project.entities.find((entity) => entity.id === locationId);
      if (current?.type !== 'location') return false;
      project.entities.push({
        id,
        type: 'image',
        title: `${current.title} · ${file.name}`.slice(0, 160),
        description: '地点视觉参考',
        parentId: null,
        order:
          Math.max(
            -1,
            ...project.entities
              .filter((entity) => entity.parentId === null)
              .map((entity) => entity.order),
          ) + 1,
        version: 1,
        status: 'draft',
        data: result.data,
      });
      project.layout.positions[id] = resolveCanvasPositions(
        project.entities,
        project.layout.positions,
      )[id];
      const edited = setLocationReferences(project, locationId, [
        ...(current.data.referenceAssetIds || []),
        id,
      ]);
      if (!edited.ok) throw Error(edited.error);
    }),
  );
  return id;
}

export async function uploadCharacterMainImage({ store, characterId, file, upload }) {
  const character = find(store, characterId),
    look = character?.data.looks?.[0];
  if (character?.type !== 'character') throw Error('这个角色已经不存在，请重新选择。');
  if (look)
    return uploadLookImage({ store, characterId, lookId: look.id, slot: 'front', file, upload });
  const context = captureWorkspace(store),
    result = await guardedUpload(
      store,
      context,
      () => {
        const live = find(store, characterId);
        return live?.type === 'character' && !live.data.looks?.length;
      },
      '上传期间角色造型已经改变，请到图集明确选择参考。',
      () => upload(file, 'image'),
    );
  const imageId = `image-${crypto.randomUUID()}`,
    lookId = `look-${crypto.randomUUID()}`;
  requireCommit(
    store,
    store.editProject((project) => {
      const owner = project.entities.find((entity) => entity.id === characterId);
      if (owner?.type !== 'character' || owner.data.looks?.length) return false;
      project.entities.push({
        id: imageId,
        type: 'image',
        title: `${owner.title} · 主形象`.slice(0, 160),
        description: '人物主形象参考',
        parentId: null,
        order:
          Math.max(
            -1,
            ...project.entities
              .filter((entity) => entity.parentId === null)
              .map((entity) => entity.order),
          ) + 1,
        version: 1,
        status: 'draft',
        data: result.data,
      });
      project.layout.positions[imageId] = resolveCanvasPositions(
        project.entities,
        project.layout.positions,
      )[imageId];
      const edited = addLook(project, characterId, {
        id: lookId,
        name: '基础造型',
        gallery: { front: imageId },
      });
      if (!edited.ok) throw Error(edited.error);
    }),
  );
  return imageId;
}

export async function createEntityWithMedia({ store, kind, title, file, upload }) {
  if (!['image', 'video', 'audio', 'character', 'location'].includes(kind))
    throw Error('这种内容不支持直接上传文件。');
  const context = captureWorkspace(store),
    result = await upload(file, ['character', 'location'].includes(kind) ? 'image' : kind);
  assertWorkspace(store, context);
  const id = `${kind}-${crypto.randomUUID()}`,
    imageId = `image-${crypto.randomUUID()}`,
    lookId = `look-${crypto.randomUUID()}`;
  requireCommit(
    store,
    store.editProject((project) => {
      const order =
          Math.max(
            -1,
            ...project.entities
              .filter((entity) => entity.parentId === null)
              .map((entity) => entity.order),
          ) + 1,
        name = (title.trim() || file.name).slice(0, 160),
        owner = {
          id,
          type: kind,
          title: name,
          description: '',
          parentId: null,
          order,
          version: 1,
          status: 'draft',
          data: ['character', 'location'].includes(kind) ? {} : result.data,
        };
      project.entities.push(owner);
      project.layout.positions[id] = resolveCanvasPositions(
        project.entities,
        project.layout.positions,
      )[id];
      if (['character', 'location'].includes(kind)) {
        project.entities.push({
          id: imageId,
          type: 'image',
          title: `${name} · ${kind === 'character' ? '主形象' : '场景参考'}`.slice(0, 160),
          description: '',
          parentId: null,
          order: order + 1,
          version: 1,
          status: 'draft',
          data: result.data,
        });
        project.layout.positions[imageId] = resolveCanvasPositions(
          project.entities,
          project.layout.positions,
        )[imageId];
        if (kind === 'character')
          owner.data.looks = [
            {
              id: lookId,
              name: '基础造型',
              description: '',
              version: 1,
              gallery: { front: imageId },
            },
          ];
        else owner.data.referenceAssetIds = [imageId];
      }
    }),
  );
  store.select(id);
  return id;
}
