// Browser-only composer state. Generation receipts and uploaded originals
// remain server-owned; an edit identity protects newer text, including ABA.
export const draftKey = (account, id) =>
  `yingxu:quick-chat:draft:${account || 'anonymous'}:${id || 'new'}`;
const token = () => globalThis.crypto.randomUUID();
export function readDraftRecord(storage, account, id) {
  let raw = '';
  try {
    raw = storage?.getItem(draftKey(account, id)) || '';
  } catch {
    // Browser storage is optional (private mode, blocked site data).
  }
  try {
    const value = JSON.parse(raw);
    if (
      value?.schema === 1 &&
      typeof value.text === 'string' &&
      typeof value.edit_id === 'string' &&
      Array.isArray(value.origins)
    )
      return value;
  } catch {
    // A damaged saved draft is ignored; the editor starts empty.
  }
  return { schema: 1, text: raw, edit_id: null, origins: [] };
}
export const readDraft = (account, id, storage = globalThis.localStorage) =>
  readDraftRecord(storage, account, id).text;
export function saveDraft(
  account,
  id,
  text,
  { edit = false, storage = globalThis.localStorage } = {},
) {
  try {
    const previous = readDraftRecord(storage, account, id);
    storage.setItem(
      draftKey(account, id),
      JSON.stringify({
        ...previous,
        text,
        edit_id: edit || text !== previous.text || !previous.edit_id ? token() : previous.edit_id,
      }),
    );
    return true;
  } catch {
    return false;
  }
}
export function draftSnapshot(storage, account, id) {
  const previous = readDraftRecord(storage, account, id);
  if (!previous.edit_id && !saveDraft(account, id, previous.text, { storage }))
    throw Error('无法保存输入草稿，尚未发送。');
  const value = readDraftRecord(storage, account, id);
  return { edit_id: value.edit_id };
}
export function carryDraft(storage, fromAccount, fromId, toAccount, toId) {
  try {
    const value = readDraftRecord(storage, fromAccount, fromId);
    storage.setItem(draftKey(toAccount, toId), JSON.stringify(value));
    return true;
  } catch {
    return false;
  }
}
export function rememberDraftOrigin(storage, account, id, snapshot, turnId) {
  if (!snapshot?.edit_id || !turnId) return;
  const value = readDraftRecord(storage, account, id),
    origins = value.origins.filter((origin) => origin.turn_id !== turnId);
  origins.push({ turn_id: turnId, edit_id: snapshot.edit_id });
  storage.setItem(
    draftKey(account, id),
    JSON.stringify({ ...value, origins: origins.slice(-100) }),
  );
}
export function clearAcceptedDraft(storage, account, id, receipt) {
  if (receipt?.status !== 'accepted' || !receipt.turn_id) return false;
  const value = readDraftRecord(storage, account, id);
  if (
    !value.edit_id ||
    !value.origins.some(
      (origin) => origin.turn_id === receipt.turn_id && origin.edit_id === value.edit_id,
    )
  )
    return false;
  storage.setItem(
    draftKey(account, id),
    JSON.stringify({
      ...value,
      text: '',
      edit_id: token(),
      origins: value.origins.filter((origin) => origin.turn_id !== receipt.turn_id),
    }),
  );
  return true;
}
