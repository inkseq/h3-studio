import React, { useEffect, useState } from 'react';
import { Disclosure } from './ui/Disclosure.jsx';
import { Scissors } from 'lucide-react';
import { useI18n } from './i18n/I18nProvider.jsx';
import { useStoryboardUI } from './storyboard-ui-context.jsx';
import { Button } from './ui/Button.jsx';

const secs = (value) => String(Math.round(value * 10) / 10);
const newKey = () => 'script-windows-' + crypto.randomUUID();

/**
 * Generate a long shot in windows (h3-studio#597, owner 2026-10-25): the assistant splits the
 * description into windows of about the chosen length, each line spoken in one window only.
 * The real cut points come from the engine, so they are shown before anything is generated.
 */
export default function ScriptWindows({ shot }) {
  const { t } = useI18n(),
    { state, dispatch, adapter } = useStoryboardUI();
  const confirmed = shot.entity?.data?.script_windows || null;
  const [preview, setPreview] = useState(null),
    [length, setLength] = useState(''),
    [busy, setBusy] = useState(false),
    [open, setOpen] = useState(false),
    [error, setError] = useState('');
  useEffect(() => {
    setPreview(null);
    setError('');
  }, [shot.id]);
  if (!adapter?.previewScriptWindows || !(shot.duration >= 8)) return null;
  const locked = busy || state.busy || !!state.workbenchPending;

  async function run(windowSeconds) {
    setBusy(true);
    setError('');
    const key = newKey();
    try {
      let value = await adapter.previewScriptWindows(shot.id, { windowSeconds, key });
      // A pending split finishes server-side; the same key reads it without a second call.
      for (let i = 0; i < 20 && value?.status === 'pending'; i++) {
        await new Promise((resolve) => setTimeout(resolve, 2000));
        value = await adapter.previewScriptWindows(shot.id, { windowSeconds, key });
      }
      setPreview(value);
      setOpen(true);
      if (value?.status !== 'ready')
        setError(t(value?.status === 'stale' ? 'windows.stale' : 'windows.invalid'));
    } catch {
      setError(t('windows.failed'));
    } finally {
      setBusy(false);
    }
  }
  async function act(op, extra = {}) {
    setBusy(true);
    setError('');
    try {
      const result = await dispatch({
        type: 'workbenchAction',
        action: { op, shot_id: shot.id, ...extra },
      });
      if (result?.ok === false) throw Error(op);
      setPreview(null);
    } catch {
      setError(t('windows.failed'));
    } finally {
      setBusy(false);
    }
  }

  const ready = preview?.status === 'ready' ? preview : null;
  const lines = new Map((ready?.dialogue || []).map((line) => [line.id, line.text]));
  const options = preview?.supported_options?.length
    ? preview.supported_options
    : [5, 6, 7, 8, 9, 10];
  return (
    // The person opens and closes it; confirming or clearing keeps it open.
    <Disclosure
      className="script-windows"
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
      summary={
        <>
          <Scissors size={14} aria-hidden="true" />
          {t('windows.title')}
          {confirmed && (
            <span className="badge">
              {t('windows.confirmed', { count: confirmed.windows?.length || 0 })}
            </span>
          )}
        </>
      }
    >
      <p className="muted">{t('windows.help')}</p>
      <div className="row tight script-windows-controls">
        <label className="field inline-num">
          <span>{t('windows.length')}</span>
          <select value={length} disabled={locked} onChange={(e) => setLength(e.target.value)}>
            <option value="">{t('windows.auto')}</option>
            {options.map((value) => (
              <option key={value} value={value}>
                {t('windows.seconds', { n: value })}
              </option>
            ))}
          </select>
        </label>
        <Button size="sm" disabled={locked} onClick={() => run(Number(length) || null)}>
          {busy && !ready ? t('windows.pending') : t('windows.preview')}
        </Button>
        {confirmed && (
          <Button
            size="sm"
            variant="ghost"
            disabled={locked}
            onClick={() => act('shot.script_windows.clear')}
          >
            {t('windows.clear')}
          </Button>
        )}
      </div>
      {ready && (
        <>
          <ol className="script-windows-list">
            {ready.timing.windows.map((window, index) => {
              const part = ready.script_windows.windows[index] || {};
              return (
                <li key={window.index}>
                  <b>
                    {t('windows.window', {
                      n: index + 1,
                      start: secs(window.start_seconds),
                      end: secs(window.end_seconds),
                    })}
                  </b>
                  <span>{part.description}</span>
                  {(part.dialogue_ids || []).map((id) => (
                    <q key={id}>{lines.get(id)}</q>
                  ))}
                </li>
              );
            })}
          </ol>
          <Button
            size="sm"
            variant="primary"
            disabled={locked}
            onClick={() => act('shot.script_windows.confirm', { preview_id: ready.preview_id })}
          >
            {t('windows.confirm')}
          </Button>
        </>
      )}
      {error && (
        <p className="muted" role="alert">
          {error}
        </p>
      )}
    </Disclosure>
  );
}
