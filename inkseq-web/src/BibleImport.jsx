// Bring a design that Inky made with the film skills (cast.json / art.json shaped JSON, or an
// export from here) into the story as candidates, and hand the story's bible back out in the
// same shape. The preview lists what each card would change; applying is one batch, so a
// refused field leaves the story as it was instead of half imported.
import React, { useRef, useState } from 'react';
import { Download, Upload } from 'lucide-react';
import { useI18n } from './i18n/I18nProvider.jsx';
import { useStoryboardUI as useStore } from './storyboard-ui-context.jsx';
import { Button } from './ui/Button.jsx';
import { Dialog, DialogActions } from './ui/Dialog.jsx';
import { Badge, Note } from './ui/Note.jsx';
import { exportBible, importActions, importPlan } from './story-bible.js';

const MAX_BYTES = 2_000_000;

export function BibleImportExport() {
  const { t } = useI18n();
  const { state, dispatch, db } = useStore();
  const input = useRef(null);
  const [plan, setPlan] = useState(null),
    [keep, setKeep] = useState(new Set()),
    [error, setError] = useState(''),
    [applying, setApplying] = useState(false);
  // A default name only when the words are loaded; the store keeps its own otherwise.
  const word = (key) => (t(key) === key ? '' : t(key));

  async function read(file) {
    setError('');
    try {
      if (!file || file.size > MAX_BYTES) throw Error('size');
      const images = new Set(state.assets.filter((a) => a.kind === 'image').map((a) => a.id));
      const items = importPlan(state.profiles, JSON.parse(await file.text()), { images });
      if (!items.length) throw Error('empty');
      setPlan(items);
      setKeep(new Set(items.flatMap((item, i) => (item.changes.length ? [i] : []))));
    } catch {
      setError(t('bible.importInvalid'));
    }
  }

  async function apply() {
    setApplying(true);
    setError('');
    const actions = importActions(plan, keep, {
      profiles: state.profiles,
      lookName: word('bible.defaultLook.character'),
      placeName: word('bible.defaultLook.location'),
    });
    // Everything is checked and written together: nothing lands unless all of it does.
    const result = actions.length ? await dispatch({ type: 'batch', actions }) : { ok: true };
    setApplying(false);
    if (result?.ok === false) setError(t('bible.importFailed'));
    else setPlan(null);
  }

  function download() {
    const name = (id) => db.asset(id)?.name || '';
    const blob = new Blob(
      [JSON.stringify(exportBible(state.profiles, { assetName: name }), null, 2)],
      { type: 'application/json' },
    );
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = (state.project?.title || 'story') + '-bible.json';
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  const fields = (item) =>
    item.changes.map((key) => t('bible.field.' + key)).join(t('bible.listSep'));

  return (
    <div className="bible-io">
      <Button size="sm" variant="ghost" onClick={() => input.current?.click()}>
        <Upload size={13} aria-hidden="true" />
        {t('bible.import')}
      </Button>
      <Button size="sm" variant="ghost" onClick={download}>
        <Download size={13} aria-hidden="true" />
        {t('bible.export')}
      </Button>
      <input
        ref={input}
        type="file"
        accept="application/json,.json"
        hidden
        onChange={(e) => {
          void read(e.target.files?.[0]);
          e.target.value = '';
        }}
      />
      {error && !plan && <Note tone="warn">{error}</Note>}
      {plan && (
        <Dialog title={t('bible.importTitle')} onClose={() => setPlan(null)} busy={applying} wide>
          <p className="muted">{t('bible.importHelp')}</p>
          <div className="bible-import-list">
            {plan.map((item, index) => (
              <label key={item.type + item.name} className="bible-import-row">
                <input
                  type="checkbox"
                  checked={keep.has(index)}
                  disabled={!item.changes.length}
                  onChange={(e) => {
                    const next = new Set(keep);
                    if (e.target.checked) next.add(index);
                    else next.delete(index);
                    setKeep(next);
                  }}
                />
                <span className="bible-import-main">
                  <span className="bible-import-title">
                    <Badge>{t('bible.kind.' + item.type)}</Badge>
                    <b>{item.name}</b>
                    <small className="muted">
                      {t(item.existing ? 'bible.importFill' : 'bible.importNew')}
                    </small>
                  </span>
                  <small className="muted">
                    {item.changes.length
                      ? t('bible.importChanges', { fields: fields(item) })
                      : t('bible.importNoChange')}
                  </small>
                  {item.missing > 0 && (
                    <small className="bible-import-warn">
                      {t('bible.importMissing', { count: item.missing })}
                    </small>
                  )}
                  {item.skipped?.includes('speaker') && (
                    <small className="bible-import-warn">{t('bible.importSpeakerTaken')}</small>
                  )}
                </span>
              </label>
            ))}
          </div>
          <p className="bible-import-status" role="alert">
            {error}
          </p>
          <DialogActions>
            <Button disabled={applying} onClick={() => setPlan(null)}>
              {t('bible.cancel')}
            </Button>
            <Button
              variant="primary"
              disabled={applying || !keep.size}
              onClick={() => void apply()}
            >
              {t('bible.importApply', { count: keep.size })}
            </Button>
          </DialogActions>
        </Dialog>
      )}
    </div>
  );
}
