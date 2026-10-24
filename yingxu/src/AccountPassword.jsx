import { useI18n } from './i18n/I18nProvider.jsx';
import { FloatingMessage } from './floating-notice.js';
import React, { useState } from 'react';
import { cloudController as cloud } from './cloud-controller.js';

export default function AccountPassword({ busy, inline = false }) {
  const { t: at } = useI18n();
  const [open, setOpen] = useState(inline),
    [oldPassword, setOld] = useState(''),
    [password, setPassword] = useState(''),
    [confirm, setConfirm] = useState(''),
    [error, setError] = useState('');
  const clear = () => {
    setOld('');
    setPassword('');
    setConfirm('');
  };
  async function submit(e) {
    e.preventDefault();
    setError('');
    if (password !== confirm) {
      setError(at('adjunct.accountPassword.copy58'));
      return;
    }
    if (new TextEncoder().encode(password).length > 72) {
      setError(at('adjunct.accountPassword.copy59'));
      return;
    }
    const previous = oldPassword,
      next = password;
    clear();
    try {
      await cloud.changePassword(previous, next);
    } catch {
      setError(at('adjunct.accountPassword.copy60'));
    }
  }
  return (
    <section className="account-password">
      {inline && <h2>{at('adjunct.accountPassword.copy62')}</h2>}
      <button
        hidden={inline}
        type="button"
        disabled={busy}
        aria-expanded={open}
        onClick={() => {
          setOpen(!open);
          clear();
          setError('');
        }}
      >
        {open ? at('adjunct.accountPassword.copy61') : at('adjunct.accountPassword.copy62')}
      </button>
      {open && (
        <form onSubmit={submit}>
          <p>{at('adjunct.accountPassword.copy63')}</p>
          <label>
            {at('adjunct.accountPassword.copy64')}
            <input
              type="password"
              autoComplete="current-password"
              required
              disabled={busy}
              value={oldPassword}
              onChange={(e) => setOld(e.target.value)}
            />
          </label>
          <label>
            {at('adjunct.accountPassword.copy65')}
            <input
              type="password"
              autoComplete="new-password"
              minLength={12}
              maxLength={72}
              required
              disabled={busy}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </label>
          <label>
            {at('adjunct.accountPassword.copy66')}
            <input
              type="password"
              autoComplete="new-password"
              minLength={12}
              maxLength={72}
              required
              disabled={busy}
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
            />
          </label>
          <button disabled={busy}>{at('adjunct.accountPassword.copy67')}</button>
          <FloatingMessage tone="error" text={error ? error : null} />
        </form>
      )}
    </section>
  );
}
