import { X } from 'lucide-react';
import { translate } from './i18n/locale.js';
import { useI18n } from './i18n/I18nProvider.jsx';
import React, { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';

// HTML dialogs keep keyboard focus inside the page instead of opening a browser
// JavaScript prompt. Closing a dialog never commits its draft.
function StoryboardDialog({ title, children, onClose, busy = false }) {
  const { t, locale } = useI18n();
  const dialog = useRef(),
    opener = useRef(document.activeElement);
  useEffect(() => {
    const node = dialog.current;
    node.showModal();
    return () => {
      node.close();
      queueMicrotask(() => {
        if (opener.current?.isConnected) opener.current.focus();
      });
    };
  }, []);
  return createPortal(
    <dialog
      ref={dialog}
      className="qc-dialog sb-portal sb-dialog"
      aria-label={title}
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) onClose();
      }}
      onClick={(event) => {
        if (event.target === event.currentTarget && !busy) onClose();
      }}
    >
      <div className="picker-head">
        <h2>{title}</h2>
        <button
          type="button"
          className="x big"
          aria-label={t('storyboard.copy1')}
          disabled={busy}
          onClick={onClose}
        >
          <X size={18} />
        </button>
      </div>
      {children}
    </dialog>,
    document.body,
  );
}

export function StoryboardNameForm({
  label,
  name,
  onName,
  onSubmit,
  onCancel,
  busy = false,
  error = '',
  locale = 'zh',
}) {
  const t = (key, values) => translate(locale, key, values);
  const valid = !!name.trim();
  const submit = (event) => {
    event.preventDefault();
    if (valid && !busy) onSubmit(name.trim());
  };
  return (
    <form onSubmit={submit} className="sb-name-form">
      <label>
        {label}
        <input
          autoFocus
          required
          value={name}
          disabled={busy}
          onChange={(event) => onName(event.target.value)}
        />
      </label>
      {error && (
        <p role="alert" className="warn">
          {error}
        </p>
      )}
      <div className="row sb-dialog-actions">
        <button type="button" disabled={busy} onClick={onCancel}>
          {t('storyboard.copy2')}
        </button>
        <button type="submit" className="primary" disabled={!valid || busy}>
          {busy ? t('storyboard.copy3') : t('storyboard.copy4')}
        </button>
      </div>
    </form>
  );
}

export function StoryboardNameDialog({ title, label = null, onSubmit, onClose }) {
  const { t, locale } = useI18n();
  const [name, setName] = useState(''),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(''),
    pending = useRef(false);
  async function submit(value) {
    if (pending.current) return;
    pending.current = true;
    setBusy(true);
    setError('');
    try {
      const receipt = await onSubmit(value);
      if (receipt?.ok === false) {
        setError(receipt.error || t('storyboard.copy5'));
        return;
      }
      onClose();
    } catch (problem) {
      setError(problem?.message || t('storyboard.copy6'));
    } finally {
      pending.current = false;
      setBusy(false);
    }
  }
  return (
    <StoryboardDialog title={title} onClose={onClose} busy={busy}>
      <StoryboardNameForm
        locale={locale}
        label={label || t('storyboard.name')}
        name={name}
        onName={setName}
        onSubmit={submit}
        onCancel={onClose}
        busy={busy}
        error={error}
      />
    </StoryboardDialog>
  );
}
