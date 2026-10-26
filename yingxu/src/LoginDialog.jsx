import React, { useState } from 'react';
import { Button } from './ui/Button.jsx';
import { Dialog } from './ui/Dialog.jsx';
import { LogOut } from 'lucide-react';
import HostedSignIn from './HostedSignIn.jsx';
import { useAccountLabel } from './StudioHeader.jsx';
import { useCloud } from './CloudStudio.jsx';
import { cloudController as cloud } from './cloud-controller.js';
import { useI18n } from './i18n/I18nProvider.jsx';

const quietly = (promise) => promise.catch(() => {});

// Password / hosted sign-in dialog shared by the landing page and the Telegram connect page.
export function Login({ onClose, onDone, title, lead, passwordFirst = false }) {
  const { t } = useI18n();
  const c = useCloud(),
    who = useAccountLabel(c.account),
    [username, setUsername] = useState(''),
    [password, setPassword] = useState(''),
    [error, setError] = useState('');
  return (
    <Dialog
      title={c.account ? t('workspace.copy89') : title || t('workspace.copy90')}
      onClose={onClose}
    >
      {c.account ? (
        <>
          <p>
            {t('workspace.copy1')}
            <b>{who.label}</b>
          </p>
          <p>{t('workspace.copy118')}</p>
          <Button onClick={() => quietly(cloud.logout().then(onClose))}>
            <LogOut size={14} />
            {t('workspace.copy2')}
          </Button>
        </>
      ) : (
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            setError('');
            try {
              await cloud.login(username, password);
              onDone();
            } catch (err) {
              setError(err.message);
            }
          }}
        >
          <p>{lead ?? t('workspace.copy119')}</p>
          <HostedSignIn auth={c.auth} passwordFirst={passwordFirst} />
          <label className="qc-field">
            {t('workspace.copy3')}
            <input
              autoComplete="username"
              required
              maxLength={100}
              value={username}
              onChange={(e) => setUsername(e.target.value)}
            />
          </label>
          {!['username-test', 'username-only-test'].includes(c.auth?.authentication) && (
            <label className="qc-field">
              {t('workspace.copy4')}
              <input
                type="password"
                autoComplete="current-password"
                required
                maxLength={72}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </label>
          )}
          {['username-test', 'username-only-test'].includes(c.auth?.authentication) && (
            <p className="qc-warning">{t('workspace.copy120')}</p>
          )}
          {error && (
            <p className="qc-error" role="alert">
              {error}
            </p>
          )}
          <Button
            type="submit"
            className="qc-primary"
            disabled={c.busy || !c.auth || c.auth.auth_ready === false}
          >
            {t('workspace.copy5')}
          </Button>
          {!c.auth && (
            <Button onClick={() => quietly(cloud.initialize())}>{t('workspace.copy6')}</Button>
          )}
        </form>
      )}
    </Dialog>
  );
}
