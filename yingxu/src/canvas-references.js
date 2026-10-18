import { effectiveLook, gallerySlots } from './character-model.js';
// Read-only projections of casting and adopted takes, not additional stored links.
export function castCanvasLinks(project) {
  const result = [],
    explicit = project.links || [],
    find = (id) => project.entities.find((e) => e.id === id);
  for (const shot of project.entities.filter((e) => e.type === 'shot')) {
    const scene = find(shot.parentId),
      ids = new Set(
        [...(scene?.data.cast || []), ...(shot.data.cast || [])].map((c) => c.characterId),
      );
    for (const link of explicit)
      if (
        link.target === shot.id &&
        link.role === 'identity' &&
        find(link.source)?.type === 'character'
      )
        ids.add(link.source);
    for (const id of ids) {
      const character = find(id),
        binding = effectiveLook(project, shot, id),
        look = character?.data.looks?.find((l) => l.id === binding?.lookId);
      if (!look) continue;
      const add = (source, label) => {
        if (
          explicit.some((l) => l.source === source && l.target === shot.id) ||
          result.some((l) => l.source === source && l.target === shot.id)
        )
          return;
        result.push({
          id: `cast:${shot.id}:${id}:${source}`,
          source,
          target: shot.id,
          role: 'identity',
          label,
          derived: true,
        });
      };
      add(id, `${look.name} · ${binding.inherited ? '场戏继承' : '镜头造型'}`);
      for (const [slot, imageId] of Object.entries(look.gallery || {}))
        if (find(imageId)?.type === 'image')
          add(imageId, `人物${gallerySlots[slot] || '参考'} · ${look.name}`);
    }
    const selected = find(shot.data.selectedAssetId);
    if (selected && ['image', 'video'].includes(selected.type))
      result.push({
        id: `selected:${shot.id}:${selected.id}`,
        source: selected.id,
        target: shot.id,
        role: 'candidate',
        label: '已采用候选',
        derived: true,
      });
  }
  return result;
}
