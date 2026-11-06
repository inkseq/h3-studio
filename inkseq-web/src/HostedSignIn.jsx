// WorkOS AuthKit sign-in (h3-studio#305): Google or email on a hosted page. Shown only when
// /api/auth/config reports it. It comes first; the username form folds below it, because
// people sign in with Google or email and usernames are for test accounts (owner, 2026-11-06).
import React from 'react';
import { Button } from './ui/Button.jsx';
import { Disclosure } from './ui/Disclosure.jsx';
import { LogIn } from 'lucide-react';
import { useI18n } from './i18n/I18nProvider.jsx';
import { hostedSignInEnabled, hostedSignInUrl } from './account-access.js';

// The username form: open on its own, folded under a link when hosted sign-in is offered.
export function UsernameSignIn({ auth, children }) {
  const { t } = useI18n();
  if (!hostedSignInEnabled(auth)) return children;
  return (
    <Disclosure className="username-sign-in" summary={t('signIn.useUsername')}>
      {children}
    </Disclosure>
  );
}

export default function HostedSignIn({ auth }) {
  const { t } = useI18n();
  if (!hostedSignInEnabled(auth)) return null;
  const here = () => window.location.pathname + window.location.search;
  return (
    <div className="hosted-sign-in">
      <Button
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
