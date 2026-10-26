// Link Google, GitHub or email sign-in to a username-and-password account (h3-studio#395/#305).
// Shown when /api/auth/config enables hosted sign-in. A password account sees a Link button;
// a linked or hosted account sees the verified email it signs in with. Loading and read
// failures are shown with a retry instead of silently hiding the card.
import React, { useCallback, useEffect, useState } from 'react';
import { Button } from './ui/Button.jsx';
import { Link2, RefreshCw } from 'lucide-react';
import { useI18n } from './i18n/I18nProvider.jsx';
import { hostedLinkUrl, hostedSignInEnabled } from './account-access.js';

export default function AccountGoogleLink({ api }) {
  const { t } = useI18n();
  const [state, setState] = useState({ kind: 'loading' });
  const load = useCallback(() => {
    let alive = true;
    setState({ kind: 'loading' });
    Promise.all([api.authConfig(), api.request('/v1/account')])
      .then(([config, account]) => {
        if (!alive) return;
        if (!hostedSignInEnabled(config)) return setState({ kind: 'off' });
        setState(
          account?.authkit_connected === true
            ? { kind: 'linked', email: account.email_verified ? account.email : '' }
            : { kind: 'unlinked' },
        );
      })
      .catch(() => alive && setState({ kind: 'error' }));
    return () => {
      alive = false;
    };
  }, [api]);
  useEffect(() => load(), [load]);
  if (state.kind === 'off') return null;
  return (
    <div className="card-box account-link" aria-busy={state.kind === 'loading' || undefined}>
      <b>{t('account.linkTitle')}</b>
      {state.kind === 'loading' && (
        <p className="muted" role="status">
          {t('account.linkLoading')}
        </p>
      )}
      {state.kind === 'error' && (
        <p className="muted" role="alert">
          {t('account.linkError')}{' '}
          <Button className="linkish" onClick={load}>
            <RefreshCw size={13} aria-hidden="true" />
            {t('account.linkRetry')}
          </Button>
        </p>
      )}
      {state.kind === 'linked' && (
        <p className="muted">
          {state.email ? t('account.linkedAs', { email: state.email }) : t('account.linked')}
        </p>
      )}
      {state.kind === 'unlinked' && (
        <>
          <p className="muted">{t('account.linkBody')}</p>
          <Button
            variant="primary"
            className="keep-label account-link-button"
            onClick={() => window.location.assign(hostedLinkUrl('/settings'))}
          >
            <Link2 size={15} aria-hidden="true" />
            {t('account.linkButton')}
          </Button>
        </>
      )}
    </div>
  );
}
