import { historySelectable } from './creation-history-model.js';

export function conversationRowContext(state, row) {
  if (!state.account || !row?.id || !Number.isSafeInteger(row.version) || row.version < 1)
    throw Error('quickSidebar.changed');
  return { account: state.account, id: row.id, version: row.version };
}
export function currentConversationRow(state, target) {
  const row = state.sessions.find((item) => item.id === target?.id);
  if (
    !target ||
    target.account !== state.account ||
    !row ||
    row.version !== target.version ||
    row.trash_state !== 'active'
  )
    throw Error('quickSidebar.changed');
  return row;
}
export async function trashConversationRows(controller, targets) {
  const state = controller.getState();
  if (state.historyView !== 'active' || !targets.length) throw Error('quickSidebar.changed');
  const rows = targets.map((target) => currentConversationRow(state, target));
  if (rows.some((row) => !historySelectable(row, 'active')))
    throw Error('quickSidebar.stillWorking');
  controller.selectHistory(rows.map((row) => row.id));
  const preview = await controller.previewHistory('trash');
  for (const target of targets) currentConversationRow(controller.getState(), target);
  if (preview.items.length && preview.items.every((item) => item.eligible))
    return controller.confirmHistory();
  return preview;
}
