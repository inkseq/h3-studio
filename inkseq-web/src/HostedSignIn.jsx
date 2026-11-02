// WorkOS AuthKit sign-in (h3-studio#305): Google or email on a hosted page. Shown only when
// /api/auth/config reports it, below the username and password form (owner, 2026-11-02).
import React from 'react';
import { Button } from './ui/Button.jsx';
import { LogIn } from 'lucide-react';
import { useI18n } from './i18n/I18nProvider.jsx';
import { hostedSignInEnabled, hostedSignInUrl } from './account-access.js';

export default function HostedSignIn({ auth }) {
  const { t } = useI18n();
  if (!hostedSignInEnabled(auth)) return null;
  const here = () => window.location.pathname + window.location.search;
  return (
    <div className="hosted-sign-in">
      <Button
        variant="primary"
        className="keep-label hosted-sign-in-main"
        onClick={() => window.location.assign(hostedSignInUrl(here(), 'sign-in'))}
      >
        <LogIn size={17} aria-hidden="true" />
        {t('signIn.hosted')}
      </Button>
      <small className="muted hosted-sign-in-consent">
        {t('signIn.consentBefore')}
        <a href="/terms">{t('signIn.terms')}</a>
        {t('signIn.comma')}
        <a href="/privacy">{t('signIn.privacy')}</a>
        {t('signIn.and')}
        <a href="/content">{t('signIn.contentPolicy')}</a>
        {t('signIn.consentAfter')}
      </small>
    </div>
  );
}
