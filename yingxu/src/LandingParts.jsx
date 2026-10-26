// Landing page parts (owner 2026-10-26, canvas "合并版"): the ink-drop stage, the chat demo,
// the seven steps and the before/after slider. Colours come from the skin tokens only, so the
// page follows the coral look and the system light/dark setting. Motion stops for
// prefers-reduced-motion (landing-parts.css).
import React from 'react';
import InkMark from './InkMark.jsx';
import { useI18n } from './i18n/I18nProvider.jsx';
import './landing-parts.css';

const Cursor = () => (
  <svg width="26" height="26" viewBox="0 0 24 24" aria-hidden="true">
    <path
      d="M4 2 L4 19 L8.5 15 L11.5 21.5 L14 20.3 L11.2 14 L17.5 14Z"
      className="lp-cursor-shape"
      strokeWidth="1.4"
      strokeLinejoin="round"
    />
  </svg>
);

const Reel = () => (
  <svg className="lp-reel" width="96" height="96" viewBox="0 0 100 100" aria-hidden="true">
    <circle cx="50" cy="50" r="44" className="lp-reel-body" />
    {[
      [50, 50, 8],
      [50, 24, 10],
      [50, 76, 10],
      [24, 50, 10],
      [76, 50, 10],
    ].map(([cx, cy, r]) => (
      <circle key={cx + '-' + cy} cx={cx} cy={cy} r={r} className="lp-reel-hole" />
    ))}
  </svg>
);

const Spark = () => (
  <svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true">
    <path
      d="M12 1 L14.2 9.8 L23 12 L14.2 14.2 L12 23 L9.8 14.2 L1 12 L9.8 9.8Z"
      className="lp-spark-shape"
    />
  </svg>
);

// The ink drop writes, plans, shoots; you mark one second; it redoes only that second.
export function LandingStage() {
  const { t } = useI18n();
  return (
    <section className="lp-stage-wrap" aria-label={t('landing.v2.stageLabel')}>
      <div className="lp-stage">
        <span className="lp-ground" />
        <div className="lp-station lp-s1">
          <span className="lp-story-title">{t('landing.v2.storyTitle')}</span>
          {[130, 104, 120, 80].map((w) => (
            <span key={w} className="lp-ani lp-write" style={{ '--w': w + 'px' }} />
          ))}
        </div>
        <span className="lp-label lp-l1">{t('landing.v2.stageWrite')}</span>
        <div className="lp-station lp-s2">
          {['pop1', 'pop2', 'pop3', 'pop2', 'pop3', 'pop1'].map((pop, i) => (
            <span key={i} className={'lp-ani lp-cell ' + pop + (i % 2 ? ' alt' : '')} />
          ))}
        </div>
        <span className="lp-label lp-l2">{t('landing.v2.stagePlan')}</span>
        <div className="lp-station lp-s3">
          <div className="lp-ani lp-screen" />
          <div className="lp-track">
            <span className="lp-ani lp-range" />
            <span className="lp-ani lp-ph" />
          </div>
        </div>
        <span className="lp-label lp-l3">{t('landing.v2.stageShoot')}</span>
        <span className="lp-ani lp-spark">
          <Spark />
        </span>
        <div className="lp-station lp-s4">
          <Reel />
        </div>
        <span className="lp-label lp-l4">{t('landing.v2.stageFilm')}</span>
        <span className="lp-cat" aria-hidden="true">
          <InkMark size={58} variant="cat" />
        </span>
        <span className="lp-ani lp-done">{t('landing.v2.stageDone')}</span>
        <div className="lp-ani lp-drop-x">
          <div className="lp-ani lp-drop-y">
            <span className="lp-shadow" />
            <InkMark size={96} variant="wink" />
          </div>
          <div className="lp-ani lp-say-drop">{t('landing.v2.stageDropSays')}</div>
        </div>
        <div className="lp-ani lp-cursor">
          <Cursor />
          <span className="lp-ani lp-say-you">{t('landing.v2.stageYouSay')}</span>
        </div>
      </div>
    </section>
  );
}

// The studio as it really works, played as a 16-second loop.
export function LandingChatDemo() {
  const { t } = useI18n();
  const cards = [1, 2, 3, 4, 5];
  return (
    <div className="lp-demo" role="img" aria-label={t('landing.v2.demoLabel')}>
      <div className="lp-demo-bar">
        <span />
        <span />
        <span />
        <small>{t('landing.v2.storyTitle')} · 映序</small>
      </div>
      <div className="lp-demo-app">
        <div className="lp-demo-chat">
          <small className="muted">{t('landing.v2.assistantName')}</small>
          <div className="lp-cl c02 lp-msg you">{t('landing.v2.chat1')}</div>
          <div className="lp-cl c10 lp-msg ai">{t('landing.v2.chat2')}</div>
          <div className="lp-cl c60 lp-msg you mark">{t('landing.v2.chat3')}</div>
          <div className="lp-cl c68 lp-msg ai">{t('landing.v2.chat4')}</div>
          <div className="lp-spacer" />
          <div className="lp-demo-input">{t('landing.v2.chatPlaceholder')}</div>
        </div>
        <div className="lp-demo-board">
          <div className="lp-chips">
            <small className="muted">{t('landing.v2.assets')}</small>
            <span className="lp-cl c14">{t('landing.v2.asset1')}</span>
            <span className="lp-cl c16">{t('landing.v2.asset2')}</span>
            <span className="lp-cl c18">{t('landing.v2.asset3')}</span>
          </div>
          <div className="lp-cards">
            {cards.map((n) => (
              <div
                key={n}
                className={'lp-cl c' + (18 + n * 2) + ' lp-card' + (n === 3 ? ' on' : '')}
              >
                <div className={'lp-thumb t' + n}>
                  <span className="lp-cl cdone" />
                  <span className="lp-cl cfill" />
                </div>
                <small>{t('landing.v2.shot' + n)}</small>
              </div>
            ))}
          </div>
          <div className="lp-cl c50 lp-compare">
            <div className="lp-take">
              <small className="muted">{t('landing.v2.before')}</small>
              <div className="lp-thumb t3 big" />
              <div className="lp-take-track">
                <span className="lp-cl cmark" />
                <span className="lp-cl cplay" />
              </div>
              <small className="lp-cl c60 lp-you-text">{t('landing.v2.markNote')}</small>
            </div>
            <div className="lp-cl c76 lp-take after">
              <small className="lp-brand-text">{t('landing.v2.after')}</small>
              <div className="lp-thumb t3 big warm" />
              <div className="lp-take-track">
                <span className="lp-fixed" />
              </div>
              <small className="muted">{t('landing.v2.onlyThis')}</small>
            </div>
          </div>
          <span className="lp-cl c86 lp-picked">{t('landing.v2.picked')}</span>
        </div>
      </div>
    </div>
  );
}

export function LandingFlow() {
  const { t } = useI18n();
  return (
    <div className="lp-flow-wrap">
      <ol className="lp-flow">
        {[1, 2, 3, 4, 5, 6, 7].map((n) => (
          <li key={n} className={'lp-node d' + (n - 1) + (n === 5 ? ' you' : '')}>
            <small>{t('landing.v2.flowTag' + n)}</small>
            <b>{t('landing.v2.flow' + n)}</b>
            <span className="muted">{t('landing.v2.flowBody' + n)}</span>
          </li>
        ))}
      </ol>
      <div className="lp-loop" aria-hidden="true">
        <div className="lp-loop-line">
          <span className="lp-loop-dot" />
        </div>
        <small>{t('landing.v2.loop')}</small>
      </div>
    </div>
  );
}
