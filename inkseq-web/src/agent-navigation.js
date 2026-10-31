// Share links locate existing content; they never authorize a read or submit work.
const validId = (value) =>
  typeof value === 'string' &&
  value.length > 0 &&
  value.length <= 160 &&
  !/[\x00-\x20\x7f\\]/.test(value);
export function readAgentLink(search) {
  const params = new URLSearchParams(search);
  if (!params.has('project')) return null;
  if (params.getAll('project').length !== 1 || !validId(params.get('project'))) return null;
  if (
    params.getAll('entity').length > 1 ||
    (params.has('entity') && !validId(params.get('entity'))) ||
    params.getAll('panel').length > 1
  )
    return null;
  return {
    projectId: params.get('project'),
    entityId: params.get('entity') || null,
    activity: params.get('panel') === 'activity',
    // Opens the generate confirmation for shots without video; the user still confirms.
    generate: params.get('panel') === 'generate',
  };
}
export function agentLinkSearch({ projectId, entityId, activity = false, generate = false } = {}) {
  if (!validId(projectId) || (entityId != null && !validId(entityId))) return '';
  const params = new URLSearchParams({ project: projectId });
  if (entityId) params.set('entity', entityId);
  if (activity) params.set('panel', 'activity');
  else if (generate) params.set('panel', 'generate');
  return '?' + params.toString();
}
// A reference shared by multiple shots has no unambiguous single-shot destination.
export function freestyleEntityTarget(project, id) {
  const entity = project.entities.find((e) => e.id === id);
  if (!entity) return null;
  if (entity.type === 'shot') return { shotId: entity.id, entityId: entity.id };
  if (['image', 'video', 'audio'].includes(entity.type)) {
    const shots = project.entities.filter(
      (e) =>
        e.type === 'shot' &&
        (e.id === entity.data?.sourceShotId ||
          e.data?.selectedAssetId === id ||
          e.data?.candidateIds?.includes(id) ||
          project.links.some((l) => l.source === id && l.target === e.id)),
    );
    if (shots.length === 1) return { shotId: shots[0].id, entityId: entity.id };
  }
  return { shotId: null, entityId: entity.id };
}
export function freestyleLinkState(state, search, account) {
  const target = readAgentLink(search);
  if (
    !target ||
    !account ||
    state.workspace.mode !== 'cloud' ||
    state.workspace.account !== account ||
    state.project.id !== target.projectId
  )
    return null;
  return {
    activity: target.activity,
    target: target.entityId ? freestyleEntityTarget(state.project, target.entityId) : null,
  };
}
export function locateEntity(project, id) {
  const entity = project.entities.find((e) => e.id === id);
  if (!entity) return null;
  let node = entity,
    chapterId = null;
  const seen = new Set();
  while (node && !seen.has(node.id)) {
    seen.add(node.id);
    if (node.type === 'chapter') {
      chapterId = node.id;
      break;
    }
    node = project.entities.find((e) => e.id === node.parentId);
  }
  const section =
    entity.type === 'character'
      ? 'characters'
      : entity.type === 'location'
        ? 'locations'
        : ['image', 'audio', 'video'].includes(entity.type)
          ? 'assets'
          : 'story';
  return { entityId: entity.id, chapterId, section };
}
export function mergeActivity(previous, incoming) {
  const events = new Map(previous.map((item) => [item.id, item]));
  for (const item of incoming) events.set(item.id, item);
  return [...events.values()].sort((a, b) => b.project_version - a.project_version);
}
