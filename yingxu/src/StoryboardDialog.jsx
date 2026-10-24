import { Dialog, DialogActions } from './ui/Dialog.jsx';
import { Button } from './ui/Button.jsx';
import { translate } from './i18n/locale.js';
import { useI18n } from './i18n/I18nProvider.jsx';
import React, { useEffect, useRef, useState } from 'react';

// HTML dialogs keep keyboard focus inside the page instead of opening a browser
// JavaScript prompt. Closing a dialog never commits its draft.
function StoryboardDialog({ title, children, onClose, busy = false }) {
  return (
    <Dialog
      title={title}
      onClose={onClose}
      busy={busy}
      closeOnBackdrop
      portal
      className="sb-portal sb-dialog"
    >
      {children}
    </Dialog>
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
      <DialogActions>
        <Button disabled={busy} onClick={onCancel}>
          {t('storyboard.copy2')}
        </Button>
        <Button type="submit" variant="primary" disabled={!valid || busy}>
          {busy ? t('storyboard.copy3') : t('storyboard.copy4')}
        </Button>
      </DialogActions>
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
