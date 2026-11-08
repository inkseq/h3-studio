// Bring a design that Inky made with the film skills (cast.json / art.json shaped JSON)
// into the story as candidates, and hand the story's bible back out in the same shape.
import React, { useRef, useState } from 'react';
import { Download, Upload } from 'lucide-react';
import { useI18n } from './i18n/I18nProvider.jsx';
import { useStoryboardUI as useStore } from './storyboard-ui-context.jsx';
import { Button } from './ui/Button.jsx';
import { Dialog, DialogActions } from './ui/Dialog.jsx';
import { Badge, Note } from './ui/Note.jsx';
import { exportBible, importPlan } from './story-bible.js';

const MAX_BYTES = 2_000_000;

export function BibleImportExport() {
  const { t } = useI18n();
  const { state, dispatch } = useStore();
  const input = useRef(null);
  const [plan, setPlan] = useState(null),
    [keep, setKeep] = useState(new Set()),
    [error, setError] = useState(''),
    [applying, setApplying] = useState(false);

  async function read(file) {
    setError('');
    try {
      if (!file || file.size > MAX_BYTES) throw Error('size');
      const items = importPlan(state.profiles, JSON.parse(await file.text()));
      if (!items.length) throw Error('empty');
      setPlan(items);
      setKeep(new Set(items.map((_, i) => i)));
    } catch {
      setError(t('bible.importInvalid'));
    }
  }

  async function apply() {
    setApplying(true);
    setError('');
    try {
      for (const [index, item] of plan.entries()) {
        if (!keep.has(index)) continue;
        let id = item.existing;
        if (!id) {
          const made = await dispatch({ type: 'newProfile', ptype: item.type, name: item.name });
          id = made?.id;
          if (!id) throw Error('create');
          if (item.desc) await dispatch({ type: 'profile', id, patch: { desc: item.desc } });
          // A new place gets one version per light state from the design.
          if (item.type === 'location' && item.lights?.length) {
            await dispatch({
              type: 'variant',
              pid: id,
              vid: 'main',
              patch: { name: item.lights[0] },
            });
            for (const name of item.lights.slice(1))
              await dispatch({ type: 'addVariant', pid: id, name });
          }
        }
        const patch = Object.fromEntries(
          Object.entries(item.patch).filter(([, value]) =>
            Array.isArray(value)
              ? value.length
              : typeof value === 'object'
                ? Object.values(value).some(Boolean)
                : !!value,
          ),
        );
        if (Object.keys(patch).length) {
          const result = await dispatch({ type: 'bible', pid: id, patch });
          if (result?.ok === false) throw Error('patch');
        }
      }
      setPlan(null);
    } catch {
      setError(t('bible.importFailed'));
    } finally {
      setApplying(false);
    }
  }

  function download() {
    const blob = new Blob([JSON.stringify(exportBible(state.profiles), null, 2)], {
      type: 'application/json',
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = (state.project?.title || 'story') + '-bible.json';
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

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
        <Dialog title={t('bible.importTitle')} onClose={() => !applying && setPlan(null)} wide>
          <p className="muted">{t('bible.importHelp')}</p>
          <div className="bible-import-list">
            {plan.map((item, index) => (
              <label key={item.type + item.name} className="bible-import-row">
                <input
                  type="checkbox"
                  checked={keep.has(index)}
                  onChange={(e) => {
                    const next = new Set(keep);
                    if (e.target.checked) next.add(index);
                    else next.delete(index);
                    setKeep(next);
                  }}
                />
                <Badge>{t('bible.kind.' + item.type)}</Badge>
                <b>{item.name}</b>
                <small className="muted">
                  {t(item.existing ? 'bible.importFill' : 'bible.importNew')}
                </small>
              </label>
            ))}
          </div>
          {error && <Note tone="warn">{error}</Note>}
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
