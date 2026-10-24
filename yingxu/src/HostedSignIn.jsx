// WorkOS AuthKit sign-in (h3-studio#305): Google, GitHub or email on a hosted page.
// Shown only when /api/auth/config reports it. It sits first inside the existing username
// form; the username fields after it stay folded away until "existing account" is opened.
import React, { useState } from 'react';
import { LogIn } from 'lucide-react';
import { useI18n } from './i18n/I18nProvider.jsx';
import { hostedSignInEnabled, hostedSignInUrl } from './account-access.js';

// `passwordFirst` opens with the username form unfolded (an existing password account, #305).
export default function HostedSignIn({ auth, passwordFirst = false }) {
  const { t } = useI18n();
  const [legacy, setLegacy] = useState(passwordFirst);
  if (!hostedSignInEnabled(auth)) return null;
  const here = () => window.location.pathname + window.location.search;
  return (
    <div className={'hosted-sign-in' + (legacy ? ' legacy-open' : '')}>
      <button
        type="button"
        className="primary keep-label hosted-sign-in-main"
        onClick={() => window.location.assign(hostedSignInUrl(here(), 'sign-in'))}
      >
        <LogIn size={17} aria-hidden="true" />
        {t('signIn.hosted')}
      </button>
      <small className="muted hosted-sign-in-consent">
        {t('signIn.consentBefore')}
        <a href="/terms">{t('signIn.terms')}</a>
        {t('signIn.comma')}
        <a href="/privacy">{t('signIn.privacy')}</a>
        {t('signIn.and')}
        <a href="/content">{t('signIn.contentPolicy')}</a>
        {t('signIn.consentAfter')}
      </small>
      <button
        type="button"
        className="linkish hosted-sign-in-legacy"
        aria-expanded={legacy}
        onClick={() => setLegacy(!legacy)}
      >
        {t(legacy ? 'signIn.hideUsername' : 'signIn.orUsername')}
      </button>
    </div>
  );
}
