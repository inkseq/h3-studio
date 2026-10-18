import { effectiveLook } from './character-model.js';
import { shotLocationIds } from './location-model.js';
const text = (value) => String(value ?? '').trim();
export function storyboardPrompt(
  project,
  shotId,
  { includeScene = false, includeCharacters = true, includeLocations = true } = {},
) {
  const shot = project.entities.find((e) => e.id === shotId && e.type === 'shot');
  if (!shot) throw Error('镜头已不存在。');
  const scene = project.entities.find((e) => e.id === shot.parentId),
    parts = [],
    add = (label, value) => {
      if (text(value)) parts.push(`${label}：${text(value)}`);
    };
  add('画面与动作', shot.description);
  const camera = [shot.data.shotSize, shot.data.cameraMove, shot.data.cameraHeight]
    .filter(Boolean)
    .join('；');
  add('拍法', camera);
  add('画面风格', shot.data.style);
  const related = project.links.filter((l) => l.target === shot.id);
  if (includeCharacters) {
    const ids = new Set(
      [...(scene?.data.cast || []), ...(shot.data.cast || [])].map((c) => c.characterId),
    );
    for (const link of related.filter((l) => l.role === 'identity'))
      if (project.entities.find((e) => e.id === link.source)?.type === 'character')
        ids.add(link.source);
    for (const id of ids) {
      const character = project.entities.find((e) => e.id === id);
      if (!character) continue;
      const binding = effectiveLook(project, shot, id),
        look = character.data.looks?.find((l) => l.id === binding?.lookId);
      add(
        `人物 ${character.title}`,
        [
          character.description,
          look && `造型 ${look.name}：${look.description || '按关联参考图保持外观'}`,
        ]
          .filter(Boolean)
          .join('；'),
      );
    }
  }
  if (includeLocations)
    for (const id of shotLocationIds(project, shot)) {
      const location = project.entities.find((e) => e.id === id && e.type === 'location');
      if (location) add(`场景 ${location.title}`, location.description);
    }
  if (includeScene) add('本场动作与台词（请只保留当前镜头需要的部分）', scene?.data.script);
  return parts.join('\n\n');
}
export function promptDraftPatch(shot, next, expected) {
  if (String(shot.data.prompt ?? shot.description ?? '') !== expected)
    throw Error('原提示词已经改变。请重新预览，避免覆盖刚才的修改。');
  if (!text(next)) throw Error('草稿为空，原提示词已保留。');
  if (next.length > 12000) throw Error('草稿超过 12000 字符，请删去不属于这个镜头的内容。');
  return { prompt: next, promptDraftPrevious: { text: expected, applied: next } };
}
export function restorePromptPatch(shot) {
  const previous = shot.data.promptDraftPrevious;
  if (!previous || shot.data.prompt !== previous.applied)
    throw Error('采用后已继续编辑。为保护新修改，请从下方复制原提示词。');
  return { prompt: previous.text, promptDraftPrevious: null };
}
