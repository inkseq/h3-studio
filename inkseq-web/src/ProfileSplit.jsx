// One person, one voice (inky-help L20261108-02): a character card that holds several people
// and has lines is split into one card per person in a group. The new cards are made in one
// batch (all or nothing, one Undo), which also puts all of them in every scene and shot that
// had the old card; the old card then goes to the trash through the usual trash flow, so it
// can be restored and is never deleted here.
import React, { useState } from 'react';
import { useI18n } from './i18n/I18nProvider.jsx';
import { useStoryboardUI as useStore } from './storyboard-ui-context.jsx';
import { useTrash } from './trash-provider.jsx';
import { store } from './store.js';
import { Button } from './ui/Button.jsx';
import { Dialog, DialogActions } from './ui/Dialog.jsx';
import { Field } from './ui/Field.jsx';
import { Note } from './ui/Note.jsx';
import { MAX_SPLIT, splitActions, splitBase } from './story-bible.js';

// The trash refuses a story with unsaved edits, so wait for autosave to catch up first.
function saved(timeout = 20000) {
  return new Promise((resolve) => {
    const done = () => !store.getState().workspace?.dirty;
    if (done()) return resolve(true);
    let stop = () => {};
    const timer = setTimeout(() => {
      stop();
      resolve(false);
    }, timeout);
    stop = store.subscribe(() => {
      if (!done()) return;
      clearTimeout(timer);
      stop();
      resolve(true);
    });
  });
}

export function ProfileSplit({ p, count }) {
  const { t } = useI18n();
  const { state, dispatch } = useStore();
  const trash = useTrash();
  const [open, setOpen] = useState(false),
    [names, setNames] = useState([]),
    [copyPhotos, setCopyPhotos] = useState(true),
    [phase, setPhase] = useState('');
  const busy = phase === 'saving';
  const base = splitBase(p.name);
  const nameFor = (i) => t('bible.splitMemberName', { name: base, n: i + 1 });
  const group = p.bible.groupId || base;
  const valid = splitActions(p, state.profiles, { names }) !== null;
  const lookName = t('bible.defaultLook.character');
  const look = p.variants.find((v) => v.id === p.def) || p.variants[0];
  const photos = look?.images?.length || 0;

  const start = () => {
    setNames(Array.from({ length: count }, (_, i) => nameFor(i)));
    setCopyPhotos(true);
    setPhase('');
    setOpen(true);
  };
  const resize = (n) =>
    setNames((old) => Array.from({ length: n }, (_, i) => old[i] ?? nameFor(i)));

  async function apply() {
    const actions = splitActions(p, state.profiles, {
      names,
      lookName: lookName === 'bible.defaultLook.character' ? '' : lookName,
      copyPhotos,
    });
    if (!actions) return;
    setPhase('saving');
    const result = await dispatch({ type: 'batch', actions });
    if (result?.ok === false || !Array.isArray(result?.ids)) {
      setPhase('failed');
      return;
    }
    let moved = false;
    if (trash?.account && (await saved())) {
      const receipt = await trash.request('trash', [
        { kind: 'entity', project_id: state.project.id, id: p.id },
      ]);
      moved = receipt?.state === 'completed';
    }
    setOpen(false);
    if (moved) dispatch({ type: 'openProfile', id: result.ids[0] });
    else setPhase('kept');
  }

  return (
    <>
      <Note tone="warn" as="div" className="bible-split-note">
        <span>{t('bible.manyPeople', { count })}</span>
        <Button size="sm" onClick={start}>
          {t('bible.split')}
        </Button>
      </Note>
      {phase === 'kept' && <Note tone="info">{t('bible.splitKept')}</Note>}
      {open && (
        <Dialog
          title={t('bible.splitTitle')}
          busy={busy}
          portal
          className="sb-portal density-compact"
          onClose={() => setOpen(false)}
        >
          <p className="muted">{t('bible.splitHelp', { group })}</p>
          <div className="bible-split">
            <Field label={t('bible.splitCount')}>
              <select
                value={names.length}
                disabled={busy}
                onChange={(e) => resize(Number(e.target.value))}
              >
                {Array.from({ length: MAX_SPLIT - 1 }, (_, i) => i + 2).map((n) => (
                  <option key={n} value={n}>
                    {n}
                  </option>
                ))}
              </select>
            </Field>
            {names.map((name, i) => (
              <Field key={i} label={t('bible.splitName', { n: i + 1 })}>
                <input
                  value={name}
                  maxLength={160}
                  disabled={busy}
                  onChange={(e) => setNames(names.map((x, j) => (j === i ? e.target.value : x)))}
                />
              </Field>
            ))}
            {/* Always shown, so nothing below moves; greyed out when there is nothing to copy. */}
            <label className="bible-split-copy">
              <input
                type="checkbox"
                checked={copyPhotos && photos > 0}
                disabled={busy || !photos}
                onChange={(e) => setCopyPhotos(e.target.checked)}
              />
              <span>
                {t('bible.splitCopyPhotos')}
                <small className="muted">
                  {photos
                    ? t('bible.splitCopyPhotosCount', { count: photos })
                    : t('bible.splitCopyPhotosNone')}
                </small>
              </span>
            </label>
          </div>
          {/* One status line that keeps its height, so the buttons below never move. */}
          <p
            className={'bible-split-status' + (phase === 'failed' || !valid ? ' warn' : '')}
            role={phase === 'failed' ? 'alert' : 'status'}
          >
            {phase === 'failed'
              ? t('bible.splitFailed')
              : busy
                ? t('bible.splitSaving')
                : valid
                  ? ''
                  : t('bible.splitNamesInvalid')}
          </p>
          <DialogActions>
            <Button disabled={busy} onClick={() => setOpen(false)}>
              {t('bible.cancel')}
            </Button>
            <Button variant="primary" disabled={busy || !valid} onClick={() => void apply()}>
              {t('bible.splitApply', { count: names.length })}
            </Button>
          </DialogActions>
        </Dialog>
      )}
    </>
  );
}
