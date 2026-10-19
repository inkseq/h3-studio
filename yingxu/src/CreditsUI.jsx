// Credits chip, Settings → Credits and the not-enough-credits prompt (h3-studio#306/#307).
// Port of prototype account.jsx. Hidden until GET /v1/credits answers, so it can merge early.
import React, { useEffect, useMemo, useState } from 'react';
import {
  Check,
  Coins,
  CreditCard,
  Gift,
  History,
  Lock,
  RotateCcw,
  ShieldCheck,
  Sparkles,
} from 'lucide-react';
import { createCloudClient } from './cloud-client.js';
import { navigateApp } from './app-navigation.js';
import { useI18n } from './i18n/I18nProvider.jsx';
import {
  CREDITS_CHANGED,
  INSUFFICIENT_CREDITS,
  creditSummary,
  entryList,
  formatCredits,
  formatPrice,
  apiDate,
  packList,
  priceRates,
  waitForPurchase,
} from './credits.js';
import { mockCreditsFetch } from './credits-mock.js';
import './credits.css';

// Dev preview only: /settings?tab=credits&mockCredits shows the UI without a backend.
const mocked = () =>
  import.meta.env?.DEV && new URLSearchParams(globalThis.location?.search).has('mockCredits');

function useClient(account) {
  return useMemo(() => {
    const client = createCloudClient(mocked() ? { fetcher: mockCreditsFetch } : undefined);
    client.setAccount(account);
    return client;
  }, [account]);
}

// null while loading or unavailable (signed out, or the backend has no credits yet).
export function useCredits(account) {
  const api = useClient(account);
  const [credits, setCredits] = useState(null);
  useEffect(() => {
    if (!account && !mocked()) return undefined;
    let live = true;
    const read = () =>
      api
        .request('/v1/credits')
        .then((body) => live && setCredits(creditSummary(body)))
        .catch(() => live && setCredits(null));
    read();
    const onFocus = () => document.visibilityState === 'visible' && read();
    window.addEventListener(CREDITS_CHANGED, read);
    document.addEventListener('visibilitychange', onFocus);
    return () => {
      live = false;
      window.removeEventListener(CREDITS_CHANGED, read);
      document.removeEventListener('visibilitychange', onFocus);
    };
  }, [api, account]);
  return credits;
}

export function CreditsChip({ account }) {
  const { t, locale } = useI18n();
  const credits = useCredits(account);
  if (!credits) return null;
  const low = credits.available < 100;
  return (
    <button
      type="button"
      className={'credits-chip keep-label' + (low ? ' low' : '')}
      title={t('credits.chipTitle')}
      aria-label={t('credits.chipLabel', { count: formatCredits(credits.available, locale) })}
      onClick={() => navigateApp('/settings?tab=credits')}
    >
      <Coins size={14} aria-hidden="true" />
      <b>{formatCredits(credits.available, locale)}</b>
      <span className="credits-word">{t('credits.word')}</span>
    </button>
  );
}

const KIND = {
  purchase: CreditCard,
  grant: Gift,
  capture: Sparkles,
  hold: Sparkles,
  release: RotateCcw,
  refund: RotateCcw,
  waive: RotateCcw,
  adjust: ShieldCheck,
  expire: ShieldCheck,
};

export function CreditsPanel({ account, active }) {
  const { t, locale } = useI18n();
  const api = useClient(account);
  const [credits, setCredits] = useState(null),
    [packs, setPacks] = useState([]),
    [rates, setRates] = useState([]),
    [log, setLog] = useState({ entries: [], next: null }),
    [buying, setBuying] = useState(null),
    [paid, setPaid] = useState(''),
    [error, setError] = useState('');
  const n = (value) => formatCredits(value, locale);
  useEffect(() => {
    if (!active) return undefined;
    let live = true;
    const settle = (promise, apply) => promise.then((body) => live && apply(body)).catch(() => {});
    settle(api.request('/v1/credits'), (body) => setCredits(creditSummary(body)));
    settle(api.request('/v1/billing/packs'), (body) => setPacks(packList(body)));
    settle(api.request('/v1/credits/prices'), (body) => setRates(priceRates(body)));
    settle(api.request('/v1/credits/entries'), (body) => setLog(entryList(body)));
    return () => {
      live = false;
    };
  }, [api, active]);

  // Back from Stripe: ?paid=<session>. Credits arrive by webhook, so poll briefly.
  useEffect(() => {
    if (!active) return undefined;
    const params = new URLSearchParams(location.search);
    if (!params.has('paid')) return undefined;
    let live = true;
    setPaid('waiting');
    const before = credits?.balance ?? null;
    waitForPurchase(() => api.request('/v1/credits').then(creditSummary), before).then((now) => {
      if (!live) return;
      setPaid(now ? 'done' : 'slow');
      if (now) {
        setCredits(now);
        window.dispatchEvent(new Event(CREDITS_CHANGED));
        api.request('/v1/credits/entries').then((body) => live && setLog(entryList(body)));
      }
      params.delete('paid');
      navigateApp('/settings?' + params, { replace: true, scroll: false });
    });
    return () => {
      live = false;
    };
  }, [active]);

  async function checkout(pack) {
    setBuying(pack.id);
    setError('');
    try {
      const { url } = await api.request('/v1/billing/checkout', {
        method: 'POST',
        body: { pack_id: pack.id },
        key: 'checkout-' + pack.id + '-' + Date.now(),
      });
      if (typeof url !== 'string' || !url.startsWith('https://'))
        throw Error(t('credits.noCheckout'));
      window.location.assign(url);
    } catch (failure) {
      setError(failure.message);
      setBuying(null);
    }
  }

  async function more() {
    const body = await api
      .request('/v1/credits/entries?cursor=' + encodeURIComponent(log.next))
      .catch(() => null);
    if (body)
      setLog((previous) => {
        const next = entryList(body);
        return { entries: [...previous.entries, ...next.entries], next: next.next };
      });
  }

  const standard = rates.find((row) => row.tier === 'standard');
  const pct =
    credits?.dailyLimit > 0 ? Math.min(100, (credits.todayUsed / credits.dailyLimit) * 100) : 0;
  const popular = packs.find((pack) => pack.popular)?.id || packs[1]?.id;
  return (
    <div className="credits-panel">
      {paid === 'waiting' && <p role="status">{t('credits.paidWaiting')}</p>}
      {paid === 'done' && (
        <p role="status" className="agent-ok">
          <Check size={14} aria-hidden="true" />
          {t('credits.paidDone')}
        </p>
      )}
      {paid === 'slow' && <p role="status">{t('credits.paidSlow')}</p>}
      {error && <p role="alert">{error}</p>}
      <div className="credits-hero card-box">
        <div>
          <small className="muted">{t('credits.balance')}</small>
          <div className="credits-big">
            <Coins size={22} aria-hidden="true" />
            {credits ? n(credits.available) : '—'}
            <span>{t('credits.word')}</span>
          </div>
          <small className="muted">
            {credits && standard
              ? t('credits.enoughFor', { count: Math.floor(credits.available / standard.perClip) })
              : ''}
            {t('credits.noExpiry')}
            {credits?.held ? ' · ' + t('credits.held', { count: n(credits.held) }) : ''}
          </small>
        </div>
        <div className="credits-today">
          <small className="muted">{t('credits.today')}</small>
          <b>{credits ? n(credits.todayUsed) : '—'}</b>
          {credits?.dailyLimit > 0 ? (
            <>
              <i className="bar">
                <b style={{ width: pct + '%' }} />
              </i>
              <small className="muted">
                {t('credits.dailyLimit', { count: n(credits.dailyLimit) })}
              </small>
            </>
          ) : (
            <small className="muted">{t('credits.noDailyLimit')}</small>
          )}
        </div>
      </div>

      {packs.length > 0 && (
        <div className="card-box">
          <h4>{t('credits.topUp')}</h4>
          <div className="packs">
            {packs.map((pack) => (
              <button
                key={pack.id}
                type="button"
                className={'pack keep-label' + (pack.id === popular ? ' best' : '')}
                data-ui-surface="pack"
                disabled={!!buying}
                onClick={() => void checkout(pack)}
              >
                {pack.id === popular && <em className="pack-tag">{t('credits.popular')}</em>}
                <b>
                  {n(pack.credits)}
                  <small> {t('credits.word')}</small>
                </b>
                {pack.bonus > 0 && (
                  <small className="good">{t('credits.bonus', { count: pack.bonus })}</small>
                )}
                <span className="pack-price">
                  {buying === pack.id
                    ? t('credits.opening')
                    : formatPrice(pack.cents, pack.currency)}
                </span>
              </button>
            ))}
          </div>
          <small className="muted">
            <Lock size={11} aria-hidden="true" /> {t('credits.stripeNote')}
          </small>
        </div>
      )}

      {rates.length > 0 && (
        <div className="card-box">
          <h4>{t('credits.pricesTitle')}</h4>
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
          <small className="muted">{t('credits.pricesNote')}</small>
        </div>
      )}

      <div className="card-box">
        <h4>
          <History size={14} aria-hidden="true" /> {t('credits.history')}
        </h4>
        {log.entries.length ? (
          <div className="credit-log">
            {log.entries.map((entry) => {
              const Icon = KIND[entry.kind] || ShieldCheck;
              return (
                <div key={entry.id}>
                  <Icon size={14} aria-hidden="true" />
                  <span className="clip">
                    {entry.reason || entry.label || t('credits.kind.' + entry.kind)}
                  </span>
                  <small className="muted">
                    {entry.reason || entry.label ? t('credits.kind.' + entry.kind) + ' · ' : ''}
                    {apiDate(entry.created_at)?.toLocaleDateString(locale) || ''}
                    {entry.receipt_url && (
                      <>
                        {' · '}
                        <a href={entry.receipt_url} target="_blank" rel="noreferrer">
                          {t('credits.receipt')}
                        </a>
                      </>
                    )}
                  </small>
                  <b className={entry.amount > 0 ? 'good' : ''}>
                    {entry.amount > 0 ? '+' : '−'}
                    {n(Math.abs(entry.amount))}
                  </b>
                </div>
              );
            })}
          </div>
        ) : (
          <p className="muted">{t('credits.noHistory')}</p>
        )}
        {log.next && (
          <button type="button" className="ghost small" onClick={() => void more()}>
            {t('credits.more')}
          </button>
        )}
        <small className="muted">{t('credits.support')}</small>
      </div>
    </div>
  );
}

// Mounted once in App. cloud-client raises INSUFFICIENT_CREDITS on 402; nothing was queued.
export function LowCreditsDialog() {
  const { t, locale } = useI18n();
  const [block, setBlock] = useState(null);
  useEffect(() => {
    const open = (event) => setBlock(event.detail);
    window.addEventListener(INSUFFICIENT_CREDITS, open);
    return () => window.removeEventListener(INSUFFICIENT_CREDITS, open);
  }, []);
  useEffect(() => {
    if (!block) return undefined;
    const onKey = (event) => event.key === 'Escape' && setBlock(null);
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [block]);
  if (!block) return null;
  const close = () => setBlock(null);
  return (
    <div className="choose-overlay" onClick={close}>
      <div
        className="choose low-credits"
        role="dialog"
        aria-modal="true"
        aria-labelledby="low-credits-title"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="picker-head">
          <b id="low-credits-title">{t('credits.lowTitle')}</b>
        </div>
        <p>
          {t('credits.lowBody', {
            need: formatCredits(block.need, locale),
            have: formatCredits(block.have, locale),
          })}
        </p>
        <p className="muted">{t('credits.lowHint')}</p>
        <div className="row tight low-credits-actions">
          <button type="button" onClick={close}>
            {t('credits.lowBack')}
          </button>
          <button
            type="button"
            className="primary keep-label"
            onClick={() => {
              close();
              navigateApp('/settings?tab=credits');
            }}
          >
            <Coins size={15} aria-hidden="true" />
            {t('credits.lowTopUp')}
          </button>
        </div>
      </div>
    </div>
  );
}
