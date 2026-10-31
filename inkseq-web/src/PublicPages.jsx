// Public pages (h3-studio#308): /pricing, /terms, /privacy, /content. No sign-in needed.
import React, { useEffect, useState } from 'react';
import { Button } from './ui/Button.jsx';
import { Check, Coins } from 'lucide-react';
import StudioHeader from './StudioHeader.jsx';
import { createCloudClient } from './cloud-client.js';
import { navigateApp } from './app-navigation.js';
import { usePageActive } from './AppRouteCache.jsx';
import { useI18n } from './i18n/I18nProvider.jsx';
import { formatCredits, formatPrice, packList, priceRates } from './credits.js';
import { LEGAL, LEGAL_STATUS, SUPPORT_EMAIL } from './legal-content.js';
import './public-pages.css';

// Launch prices from #306/#307. Shown only until the public price endpoints answer.
const LAUNCH_PRICES = { rates: { fast: 2, standard: 4, refine: 12 } };
const LAUNCH_PACKS = {
  packs: [
    { id: 'starter', credits: 500, amount_cents: 500 },
    { id: 'creator', credits: 2100, amount_cents: 2000, bonus_percent: 5, popular: true },
    { id: 'studio', credits: 5500, amount_cents: 5000, bonus_percent: 10 },
    { id: 'pro', credits: 11500, amount_cents: 10000, bonus_percent: 15 },
  ],
};

export function PublicFooter() {
  const { t } = useI18n();
  const go = (path) => (event) => {
    event.preventDefault();
    navigateApp(path);
  };
  return (
    <footer className="public-footer">
      <nav aria-label={t('legal.footer')}>
        {[
          ['/pricing', 'pricing.title'],
          ['/terms', 'signIn.terms'],
          ['/privacy', 'signIn.privacy'],
          ['/content', 'signIn.contentPolicy'],
        ].map(([href, label]) => (
          <a key={href} href={href} onClick={go(href)}>
            {t(label)}
          </a>
        ))}
        <a href={'mailto:' + SUPPORT_EMAIL}>{SUPPORT_EMAIL}</a>
      </nav>
      <small className="muted">© {new Date().getFullYear()} Inkseq</small>
    </footer>
  );
}

function usePageTitle(title) {
  const active = usePageActive();
  useEffect(() => {
    if (active) document.title = title + ' · Inkseq';
  }, [active, title]);
}

export function PricingPage({ account }) {
  const { t, locale } = useI18n();
  usePageTitle(t('pricing.title'));
  const [rates, setRates] = useState(() => priceRates(LAUNCH_PRICES)),
    [packs, setPacks] = useState(() => packList(LAUNCH_PACKS));
  useEffect(() => {
    const api = createCloudClient();
    let live = true;
    api
      .request('/v1/credits/prices', { anonymous: true })
      .then((body) => live && priceRates(body).length && setRates(priceRates(body)))
      .catch(() => {});
    api
      .request('/v1/billing/packs', { anonymous: true })
      .then((body) => live && packList(body).length && setPacks(packList(body)))
      .catch(() => {});
    return () => {
      live = false;
    };
  }, []);
  const n = (value) => formatCredits(value, locale);
  return (
    <div className="home">
      <StudioHeader account={account} active="public" />
      <main className="home-main public-page">
        <h1>{t('pricing.title')}</h1>
        <p className="public-lead">{t('pricing.lead')}</p>

        <section className="card-box">
          <h2>{t('credits.pricesTitle')}</h2>
          <div className="price-table">
            {rates.map((row) => (
              <div key={row.tier}>
                <b>{t('credits.tier.' + row.tier)}</b>
                <small className="muted">{t('credits.tierHelp.' + row.tier)}</small>
                <span>{t('credits.perSecond', { count: row.rate })}</span>
                <span className="muted">{t('credits.perClip', { count: row.perClip })}</span>
              </div>
            ))}
          </div>
        </section>

        <section className="card-box">
          <h2>{t('pricing.packs')}</h2>
          <div className="packs">
            {packs.map((pack) => (
              <div key={pack.id} className={'pack' + (pack.popular ? ' best' : '')}>
                {pack.popular && <em className="pack-tag">{t('credits.popular')}</em>}
                <b>
                  {n(pack.credits)}
                  <small> {t('credits.word')}</small>
                </b>
                {pack.bonus > 0 && (
                  <small className="good">{t('credits.bonus', { count: pack.bonus })}</small>
                )}
                <span className="pack-price">{formatPrice(pack.cents, pack.currency)}</span>
              </div>
            ))}
          </div>
          <Button
            variant="primary"
            className="keep-label pricing-cta"
            onClick={() => navigateApp(account ? '/settings?tab=credits' : '/quick-chat')}
          >
            <Coins size={15} aria-hidden="true" />
            {t(account ? 'pricing.topUp' : 'pricing.start')}
          </Button>
        </section>

        <section className="card-box">
          <h2>{t('pricing.fairTitle')}</h2>
          <ul className="pricing-points">
            {['quote', 'failed', 'expiry', 'refund', 'caps', 'stripe'].map((key) => (
              <li key={key}>
                <Check size={14} aria-hidden="true" />
                <span>{t('pricing.point.' + key)}</span>
              </li>
            ))}
          </ul>
        </section>
        <PublicFooter />
      </main>
    </div>
  );
}

export function LegalPage({ account, doc }) {
  const { t, locale } = useI18n();
  const page = LEGAL[doc],
    text = page[locale] || page.en;
  usePageTitle(text.title);
  return (
    <div className="home">
      <StudioHeader account={account} active="public" />
      <main className="home-main public-page legal-page">
        {LEGAL_STATUS === 'draft' && (
          <p className="legal-draft" role="note">
            {t('legal.draft')}
          </p>
        )}
        <h1>{text.title}</h1>
        <p className="muted">{t('legal.version', { version: page.version })}</p>
        {text.blocks.map(([kind, value], index) =>
          kind === 'h' ? (
            <h2 key={index}>{value}</h2>
          ) : kind === 'ul' ? (
            <ul key={index}>
              {value.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          ) : (
            <p key={index}>{value}</p>
          ),
        )}
        <PublicFooter />
      </main>
    </div>
  );
}
