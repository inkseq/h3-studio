// WorkOS AuthKit sign-in (h3-studio#305): Google, GitHub or email on a hosted page.
// Shown only when /api/auth/config reports it; the username form stays for existing accounts.
import React from 'react';
import { LogIn } from 'lucide-react';
import { useI18n } from './i18n/I18nProvider.jsx';
import { hostedSignInEnabled, hostedSignInUrl } from './account-access.js';

export default function HostedSignIn({ auth }) {
  const { t } = useI18n();
  if (!hostedSignInEnabled(auth)) return null;
  const here = () => window.location.pathname + window.location.search;
  return (
    <div className="hosted-sign-in">
      <button
        type="button"
        className="primary keep-label"
        onClick={() => window.location.assign(hostedSignInUrl(here()))}
      >
        <LogIn size={16} aria-hidden="true" />
        {t('signIn.hosted')}
      </button>
      <small className="muted">
        {t('signIn.consentBefore')}
        <a href="/terms">{t('signIn.terms')}</a>
        {t('signIn.comma')}
        <a href="/privacy">{t('signIn.privacy')}</a>
        {t('signIn.and')}
        <a href="/content-policy">{t('signIn.contentPolicy')}</a>
        {t('signIn.consentAfter')}
      </small>
      <div className="hosted-sign-in-or" role="separator">
        <span>{t('signIn.orUsername')}</span>
      </div>
    </div>
  );
}
