import React, { useEffect, useMemo, useState } from 'react';
import {
  ArrowRight,
  Clapperboard,
  Cloud,
  PenLine,
  Receipt,
  Scissors,
  UserRound,
} from 'lucide-react';
import InkMark from './InkMark.jsx';
import { dailyInkMark } from './ink-mark-daily.js';
import { PublicFooter } from './PublicPages.jsx';
import { Login } from './QuickChatWorkspace.jsx';
import { navigateApp } from './app-navigation.js';
import { usePageActive } from './AppRouteCache.jsx';
import { LanguageSwitcher, useI18n } from './i18n/I18nProvider.jsx';
import { useCloud } from './CloudStudio.jsx';
import { hostedSignInEnabled, hostedSignInUrl } from './account-access.js';
import './landing.css';

// A still storyboard drawn in CSS, so the page needs no image downloads.
function DemoBoard() {
  const { t } = useI18n();
  const shots = [
    ['landing.demoShot1', '0:06', 'a'],
    ['landing.demoShot2', '0:04', 'b'],
    ['landing.demoShot3', '0:05', 'c'],
  ];
  return (
    <figure className="landing-demo" aria-label={t('landing.demoLabel')}>
      <div className="landing-demo-chapter">{t('landing.demoChapter')}</div>
      <div className="landing-demo-cards">
        {shots.map(([title, time, tone], index) => (
          <div key={title} className="landing-demo-card">
            <div className={'landing-demo-frame tone-' + tone}>
              <span>{String(index + 1).padStart(2, '0')}</span>
              <span>{time}</span>
            </div>
            <b>{t(title)}</b>
          </div>
        ))}
      </div>
      <div className="landing-demo-timeline" aria-hidden="true">
        <span className="tone-a" style={{ flexGrow: 6 }} />
        <span className="tone-b" style={{ flexGrow: 4 }} />
        <span className="tone-c" style={{ flexGrow: 5 }} />
        <i className="landing-demo-playhead" />
      </div>
    </figure>
  );
}

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
    [login, setLogin] = useState(false),
    [passwordFirst, setPasswordFirst] = useState(false);
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
    } catch {}
    window.location.replace(hostedSignInUrl('/quick-chat', 'sign-in'));
  }, [retrying, c.auth]);
  const signinError =
    !code || retrying
      ? ''
      : code === 'rate_limited'
        ? t('landing.errorRateLimited')
        : t('landing.errorSignIn');
  const steps = [
    [PenLine, 'landing.step1Title', 'landing.step1Body'],
    [Clapperboard, 'landing.step2Title', 'landing.step2Body'],
    [Scissors, 'landing.step3Title', 'landing.step3Body'],
  ];
  const more = [
    [UserRound, 'landing.more1Title', 'landing.more1Body'],
    [Cloud, 'landing.more2Title', 'landing.more2Body'],
    [Receipt, 'landing.more3Title', 'landing.more3Body'],
  ];
  return (
    <div className="landing">
      <header className="landing-bar">
        <a className="logo" href="/">
          <InkMark size={26} variant={dailyInkMark()} />
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
        <button
          type="button"
          className="landing-link"
          disabled={opening}
          onClick={() => start('sign-in')}
        >
          {t('landing.signIn')}
        </button>
        <button
          type="button"
          className="primary small landing-bar-start"
          disabled={opening}
          onClick={() => start()}
        >
          {t('landing.start')}
        </button>
      </header>
      <main>
        <section className="landing-hero">
          <div className="landing-hero-copy">
            <h1>{t('landing.title')}</h1>
            <p className="landing-lede">{t('landing.lede')}</p>
            {signinError && (
              <p className="landing-error" role="alert">
                {signinError}
              </p>
            )}
            <div className="landing-ctas">
              <button
                type="button"
                className="primary landing-big"
                disabled={opening}
                onClick={() => start()}
              >
                {t('landing.start')}
                <ArrowRight size={18} />
              </button>
            </div>
            {hostedSignInEnabled(c.auth) && (
              <small className="muted">
                {t('landing.noAccount')}{' '}
                <button
                  type="button"
                  className="linkish"
                  onClick={() => {
                    setPasswordFirst(true);
                    setLogin(true);
                  }}
                >
                  {t('landing.passwordSignIn')}
                </button>
              </small>
            )}
          </div>
          <DemoBoard />
        </section>
        <section className="landing-section">
          <h2>{t('landing.howTitle')}</h2>
          <ol className="landing-steps">
            {steps.map(([Icon, title, body], index) => (
              <li key={title}>
                <span className="landing-step-n">{index + 1}</span>
                <Icon size={22} aria-hidden="true" />
                <h3>{t(title)}</h3>
                <p className="muted">{t(body)}</p>
              </li>
            ))}
          </ol>
        </section>
        <section className="landing-section">
          <h2>{t('landing.moreTitle')}</h2>
          <ul className="landing-more">
            {more.map(([Icon, title, body]) => (
              <li key={title}>
                <Icon size={18} aria-hidden="true" />
                <div>
                  <h3>{t(title)}</h3>
                  <p className="muted">{t(body)}</p>
                </div>
              </li>
            ))}
          </ul>
        </section>
        <section className="landing-final">
          <h2>{t('landing.finalTitle')}</h2>
          <button type="button" className="primary landing-big" onClick={() => start()}>
            {t('landing.start')}
            <ArrowRight size={18} />
          </button>
        </section>
      </main>
      <PublicFooter />
      {login && (
        <Login
          title={t('landing.signInTitle')}
          lead={t('landing.signInLead')}
          passwordFirst={passwordFirst}
          onClose={() => setLogin(false)}
          onDone={() => setLogin(false)}
        />
      )}
    </div>
  );
}
