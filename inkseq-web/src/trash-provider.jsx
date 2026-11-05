// The trash state every signed-in page shares. Its own file so the public pages' first script
// does not carry the trash page.
import React, { createContext, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { Button } from './ui/Button.jsx';
import { Dialog } from './ui/Dialog.jsx';
import { createCloudClient } from './cloud-client.js';
import { createTrashController, trashTargetKey, trashErrorKey } from './trash-commands.js';
import { useI18n } from './i18n/I18nProvider.jsx';
import { store } from './store.js';
import { cloudController as cloud } from './cloud-controller.js';

const Context = createContext(null);
export const useTrash = () => useContext(Context);
export function TrashProvider({ account, children }) {
  const { t } = useI18n(),
    identity = useRef(account);
  identity.current = account;
  const [busy, setBusy] = useState(false),
    [error, setError] = useState(''),
    [preview, setPreview] = useState(null),
    [notice, setNotice] = useState(null),
    [revision, setRevision] = useState(0),
    // #232 (#10): trashed items leave the screen at once; the server catches up behind it.
    [hidden, setHidden] = useState(() => new Set());
  // #232 (#14): a trash/restore notice is a short toast, not a banner that outlives the page.
  useEffect(() => {
    if (!notice) return;
    const timer = setTimeout(() => setNotice(null), 8000);
    return () => clearTimeout(timer);
  }, [notice]);
  const api = useMemo(() => {
    const client = createCloudClient();
    client.setAccount(account);
    return client;
  }, [account]);
  const controller = useMemo(
    () =>
      createTrashController({
        api,
        account,
        getAccount: () => identity.current,
        storage: localStorage,
      }),
    [account, api],
  );
  useEffect(() => {
    setPreview(null);
    setNotice(null);
    setError('');
    setBusy(false);
    setRevision((v) => v + 1);
  }, [account]);
  let pending;
  try {
    pending = controller.pending();
  } catch {
    pending = {};
  }
  async function run(fn) {
    if (!account || busy) return;
    const original = account;
    setBusy(true);
    setError('');
    try {
      const result = await fn();
      if (identity.current !== original) return;
      setRevision((v) => v + 1);
      return result;
    } catch (e) {
      if (identity.current === original) setError(t(trashErrorKey(e)));
    } finally {
      if (identity.current === original) setBusy(false);
    }
  }
  async function completed(receipt, current = store.getState()) {
    if (identity.current !== account) return;
    setPreview(null);
    setNotice({ action: receipt.action, targets: receipt.items.map((row) => row.target) });
    window.dispatchEvent(new CustomEvent('inkseq-trash-changed', { detail: receipt }));
    if (receipt.items.some((row) => row.target.kind === 'project'))
      void cloud.list({ replace: true }).catch(() => {});
    const targets = receipt.items.map((row) => row.target),
      fresh = () =>
        identity.current === account &&
        store.getState().workspaceEpoch === current.workspaceEpoch &&
        !store.getState().workspace.dirty;
    if (
      current.workspace.mode !== 'cloud' ||
      !fresh() ||
      !targets.some((row) => row.project_id === current.project.id)
    )
      return;
    if (
      receipt.action === 'trash' &&
      targets.some((row) => row.kind === 'project' && row.id === current.project.id)
    ) {
      store.leaveCloudProject();
      return;
    }
    try {
      const updated = await api.request('/v1/projects/' + encodeURIComponent(current.project.id));
      if (fresh())
        store.applyCloudCommand(updated.project, {
          account,
          expectedVersion: current.workspace.serverVersion,
          version: updated.version,
          expectedProject: current.project,
        });
    } catch {
      /* Preserve accepted receipt and any newer local edits. */
    }
  }
  const hide = (targets, on) =>
    setHidden((old) => {
      const next = new Set(old);
      for (const row of targets) next[on ? 'add' : 'delete'](row.id);
      return next;
    });
  async function request(action, targets) {
    if (action === 'trash') hide(targets, true);
    if (action === 'restore') hide(targets, false);
    const result = await run(async () => {
      const current = store.getState();
      if (
        current.workspace.mode === 'cloud' &&
        current.workspace.dirty &&
        targets.some((row) => row.project_id === current.project.id)
      )
        throw Error('trash.unsaved');
      const value = await controller.preview(action, targets);
      if (action === 'purge') {
        setPreview(value);
        return;
      }
      const receipt = await controller.start(value);
      if (receipt.state === 'completed') await completed(receipt, current);
      return receipt;
    });
    // A refused or failed move puts the item back where it was (the error shows above).
    if (action === 'trash' && !result) hide(targets, false);
    return result;
  }
  const value = { account, controller, busy, pending, revision, request, run, hidden };
  return (
    <Context.Provider value={value}>
      {children}
      {error && (
        <div className="trash-notice" role="alert">
          {error}
          <Button onClick={() => setError('')}>{t('trash.close')}</Button>
        </div>
      )}
      {pending && (
        <div className="trash-notice" role="status">
          <span>{t('trash.pending')}</span>
          <Button
            disabled={busy}
            onClick={() =>
              run(async () => {
                const r = await controller.recover();
                if (r.state === 'completed') await completed(r);
              })
            }
          >
            {t('trash.check')}
          </Button>
          {pending.operationId && (
            <Button
              disabled={busy}
              onClick={() =>
                run(async () => {
                  const r = await controller.resume();
                  if (r.state === 'completed') await completed(r);
                })
              }
            >
              {t('trash.resume')}
            </Button>
          )}
        </div>
      )}
      {notice && !pending && !error && (
        <div className="trash-notice" role="status">
          <span>
            {t(
              notice.action === 'trash'
                ? 'trash.moved'
                : notice.action === 'restore'
                  ? 'trash.restored'
                  : 'trash.deleted',
            )}
          </span>
          {notice.action === 'trash' && (
            <Button disabled={busy} onClick={() => request('restore', notice.targets)}>
              {t('trash.undo')}
            </Button>
          )}
          <Button onClick={() => setNotice(null)}>{t('trash.close')}</Button>
        </div>
      )}
      {preview && (
        <Dialog
          className="ops-dialog"
          title={t('trash.confirm')}
          onClose={() => {
            if (!busy) setPreview(null);
          }}
        >
          <p>{t('trash.irreversible')}</p>
          <ul>
            {preview.items.map((row) => (
              <li key={trashTargetKey(row.target)}>{row.title || t('trash.untitled')}</li>
            ))}
          </ul>
          <p>
            {t('trash.summary', {
              removed: preview.removed?.objects ?? 0,
              bytes: preview.removed?.bytes ?? 0,
              kept: preview.kept?.objects ?? 0,
            })}
          </p>
          <Button disabled={busy} onClick={() => setPreview(null)}>
            {t('trash.cancel')}
          </Button>
          <Button
            variant="danger"
            disabled={busy}
            onClick={() =>
              run(async () => {
                const r = await controller.start(preview);
                setPreview(null);
                if (r.state === 'completed') await completed(r);
              })
            }
          >
            {t('trash.purge')}
          </Button>
        </Dialog>
      )}
    </Context.Provider>
  );
}
