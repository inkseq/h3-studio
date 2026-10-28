import React, { useEffect, useMemo, useState } from 'react';
import { Button } from './ui/Button.jsx';
import { FloatingMessage } from './floating-notice.js';
import { ArrowRight } from 'lucide-react';
import InkMark from './InkMark.jsx';
import { PublicFooter } from './PublicPages.jsx';
import { Login } from './LoginDialog.jsx';
import { navigateApp } from './app-navigation.js';
import { usePageActive } from './AppRouteCache.jsx';
import { LanguageSwitcher, useI18n } from './i18n/I18nProvider.jsx';
import { useCloud } from './CloudStudio.jsx';
import { hostedSignInEnabled, hostedSignInUrl } from './account-access.js';
import './landing.css';

import { LandingStage, LandingChatDemo, LandingFlow } from './LandingParts.jsx';

const RETRY_KEY = 'inkseq.signinRetryAt';
function retryAllowed() {
  try {
    return Date.now() - Number(sessionStorage.getItem(RETRY_KEY) || 0) > 120000;
  } catch {
    return false;
  }
}

export default function Landing() {
  const { t } = useI18n();
  const active = usePageActive(),
    [login, setLogin] = useState(false);
  useEffect(() => {
    if (active) document.title = t('landing.pageTitle');
  }, [active, t]);
  const c = useCloud();
  // Creating needs an account: hosted sign-in (email or Google) when enabled, else the dialog.
  // "Get started" opens the sign-up screen, "Log in" the sign-in screen (#395).
  // The hosted page can take a few seconds to open; one click is enough, so the buttons wait.
  const [opening, setOpening] = useState(false);
  useEffect(() => {
    const back = () => setOpening(false); // returning via the Back button
    window.addEventListener('pageshow', back);
    return () => window.removeEventListener('pageshow', back);
  }, []);
  const start = (hint = 'sign-up') => {
    if (!hostedSignInEnabled(c.auth)) return setLogin(true);
    if (opening) return;
    setOpening(true);
    window.location.assign(hostedSignInUrl('/quick-chat', hint));
  };
  // A sign-in that did not finish comes back here with ?signin_error=<code>.
  const code = useMemo(
    () => new URLSearchParams(window.location.search).get('signin_error') || '',
    [],
  );
  // state_invalid usually means the sign-in page sat open past its 10-minute window while the
  // person finished signing up; their hosted session is already there, so one quiet retry
  // completes it. Only once per two minutes, so a real failure still shows its message.
  const [retrying, setRetrying] = useState(() => code === 'state_invalid' && retryAllowed());
  useEffect(() => {
    if (!retrying || !c.auth) return;
    if (!hostedSignInEnabled(c.auth)) return setRetrying(false);
    try {
      sessionStorage.setItem(RETRY_KEY, String(Date.now()));
    } catch {
      // Browser storage is optional (private mode, blocked site data).
    }
    window.location.replace(hostedSignInUrl('/quick-chat', 'sign-in'));
  }, [retrying, c.auth]);
  const signinError =
    !code || retrying
      ? ''
      : code === 'rate_limited'
        ? t('landing.errorRateLimited')
        : t('landing.errorSignIn');
  return (
    <div className="landing">
      <header className="landing-bar">
        <a className="logo" href="/">
          <InkMark size={26} />
          <span>{t('app.name')}</span>
        </a>
        <div className="spacer" />
        <a
          className="landing-link"
          href="/pricing"
          onClick={(event) => {
            event.preventDefault();
            navigateApp('/pricing');
          }}
        >
          {t('landing.pricing')}
        </a>
        <LanguageSwitcher />
        <Button className="landing-link" disabled={opening} onClick={() => setLogin(true)}>
          {t('landing.signIn')}
        </Button>
        <Button
          variant="primary"
          size="sm"
          className="landing-bar-start"
          disabled={opening}
          onClick={() => start()}
        >
          {t('landing.start')}
        </Button>
      </header>
      <main>
        {/* Owner 2026-10-26: one page — the ink drop explains the idea, the studio proves it,
            seven steps map it, the slider shows "only the marked part changes". */}
        <section className="lp-hero">
          <h1>{t('landing.v2.title')}</h1>
          <p className="landing-lede">{t('landing.v2.lede')}</p>
          <FloatingMessage tone="error" text={signinError ? signinError : null} />
          <div className="landing-ctas">
            <Button
              variant="primary"
              className="landing-big"
              disabled={opening}
              onClick={() => start()}
            >
              {t('landing.start')}
              <ArrowRight size={18} />
            </Button>
            <a className="landing-secondary" href="#how">
              {t('landing.v2.howLink')}
            </a>
          </div>
        </section>
        <LandingStage />
        <section id="demo" className="lp-section">
          <header className="lp-head">
            <span className="lp-eyebrow">{t('landing.v2.demoEyebrow')}</span>
            <h2>{t('landing.v2.demoTitle')}</h2>
            <p className="muted">{t('landing.v2.demoLede')}</p>
          </header>
          <LandingChatDemo />
        </section>
        <section id="how" className="lp-section">
          <header className="lp-head">
            <span className="lp-eyebrow">{t('landing.v2.flowEyebrow')}</span>
            <h2>{t('landing.v2.flowTitle')}</h2>
          </header>
          <LandingFlow />
        </section>
        <section className="lp-section lp-agents">
          <p>{t('landing.v2.agents')}</p>
          <ul aria-label={t('landing.v2.agentsLabel')}>
            {['landing.v2.agentBuiltIn', 'Claude', 'ChatGPT', 'Telegram'].map((name) => (
              <li key={name}>{name.startsWith('landing.') ? t(name) : name}</li>
            ))}
          </ul>
        </section>
        <section className="landing-final">
          <h2>{t('landing.v2.finalTitle')}</h2>
          <div className="landing-ctas">
            <Button variant="primary" className="landing-big" onClick={() => start()}>
              {t('landing.start')}
              <ArrowRight size={18} />
            </Button>
            <a
              className="landing-secondary"
              href="/pricing"
              onClick={(event) => {
                event.preventDefault();
                navigateApp('/pricing');
              }}
            >
              {t('landing.v2.pricingLink')}
            </a>
          </div>
        </section>
      </main>
      <PublicFooter />
      {login && (
        <Login
          title={t('landing.signInTitle')}
          lead={t('landing.signInLead')}
          onClose={() => setLogin(false)}
          onDone={() => setLogin(false)}
        />
      )}
    </div>
  );
}
