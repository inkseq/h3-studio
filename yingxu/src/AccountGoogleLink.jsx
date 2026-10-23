// Link Google, GitHub or email sign-in to a username-and-password account (h3-studio#395).
// Shown only when /api/auth/config enables hosted sign-in and /v1/account says this account
// has no hosted identity yet. The account keeps its stories and credits; the password still works.
import React, { useEffect, useState } from 'react';
import { Link2 } from 'lucide-react';
import { useI18n } from './i18n/I18nProvider.jsx';
import { hostedLinkUrl, hostedSignInEnabled } from './account-access.js';

export default function AccountGoogleLink({ api }) {
  const { t } = useI18n();
  const [state, setState] = useState(null);
  useEffect(() => {
    let alive = true;
    Promise.all([api.authConfig(), api.request('/v1/account')])
      .then(([config, account]) => {
        if (!alive || !hostedSignInEnabled(config)) return;
        setState(account?.authkit_connected === true ? 'linked' : 'unlinked');
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [api]);
  if (!state) return null;
  return (
    <div className="card-box">
      <b>{t('account.linkTitle')}</b>
      {state === 'linked' ? (
        <p className="muted">{t('account.linked')}</p>
      ) : (
        <>
          <p className="muted">{t('account.linkBody')}</p>
          <button
            type="button"
            className="primary keep-label"
            onClick={() => window.location.assign(hostedLinkUrl('/settings'))}
          >
            <Link2 size={15} aria-hidden="true" />
            {t('account.linkButton')}
          </button>
        </>
      )}
    </div>
  );
}
