// Credits chip, Settings → Credits and the not-enough-credits prompt (h3-studio#306/#307).
// Port of prototype account.jsx. Hidden until GET /v1/credits answers, so it can merge early.
import React, { useEffect, useEffectEvent, useMemo, useState } from 'react';
import { Button } from './ui/Button.jsx';
import { Dialog, DialogActions } from './ui/Dialog.jsx';
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
  receiptList,
  waitForPurchase,
} from './credits.js';
import { mockCreditsFetch } from './credits-mock.js';
import './credits.css';
import { checkoutAnalytics, track } from './analytics.js';

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

// Adds the anonymous funnel id when analytics is on (h3-studio#725); older servers ignore it.
function withAnalytics(body) {
  const analytics = checkoutAnalytics();
  return analytics ? { ...body, analytics } : body;
}

export function CreditsChip({ account }) {
  const { t, locale } = useI18n();
  const credits = useCredits(account);
  if (!credits) return null;
  const low = credits.available < 100;
  return (
    <Button
      className={'credits-chip keep-label' + (low ? ' low' : '')}
      title={t('credits.chipTitle')}
      aria-label={t('credits.chipLabel', { count: formatCredits(credits.available, locale) })}
      onClick={() => navigateApp('/settings?tab=credits')}
    >
      <Coins size={14} aria-hidden="true" />
      <b>{formatCredits(credits.available, locale)}</b>
      <span className="credits-word">{t('credits.word')}</span>
    </Button>
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

// The line under the balance: what it buys, when the soonest credits expire, what is held.
function BalanceNote({ credits, standard, n }) {
  const { t, locale } = useI18n();
  const expiry = credits?.expiry;
  return (
    <small className="muted">
      {credits && standard
        ? t('credits.enoughFor', { count: Math.floor(credits.available / standard.perClip) })
        : ''}
      {expiry
        ? t('credits.expiresOn', {
            count: n(expiry.credits),
            date: new Intl.DateTimeFormat(locale === 'zh' ? 'zh-CN' : 'en-AU', {
              dateStyle: 'medium',
            }).format(expiry.at),
          })
        : t('credits.noExpiry')}
      {credits?.held ? ' · ' + t('credits.held', { count: n(credits.held) }) : ''}
    </small>
  );
}

export function CreditsPanel({ account, active }) {
  const { t, locale } = useI18n();
  const api = useClient(account);
  const [credits, setCredits] = useState(null),
    [packs, setPacks] = useState([]),
    [rates, setRates] = useState([]),
    [log, setLog] = useState({ entries: [], next: null }),
    [buying, setBuying] = useState(null),
    [paid, setPaid] = useState(''),
    [receipts, setReceipts] = useState([]),
    [openReceipt, setOpenReceipt] = useState(null),
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
    settle(api.request('/v1/billing/payments'), (body) => setReceipts(receiptList(body)));
    return () => {
      live = false;
    };
  }, [api, active]);

  // Back from Stripe: ?paid=<session>. Credits arrive by webhook, so poll briefly.
  const waitForPaid = useEffectEvent((isLive, params) => {
    const before = credits?.balance ?? null;
    waitForPurchase(() => api.request('/v1/credits').then(creditSummary), before).then((now) => {
      if (!isLive()) return;
      setPaid(now ? 'done' : 'slow');
      if (now) {
        setCredits(now);
        window.dispatchEvent(new Event(CREDITS_CHANGED));
        api.request('/v1/credits/entries').then((body) => isLive() && setLog(entryList(body)));
        api
          .request('/v1/billing/payments')
          .then((body) => isLive() && setReceipts(receiptList(body)))
          .catch(() => {});
      }
      params.delete('paid');
      navigateApp('/settings?' + params, { replace: true, scroll: false });
    });
  });
  useEffect(() => {
    if (!active) return undefined;
    const params = new URLSearchParams(location.search);
    if (!params.has('paid')) return undefined;
    let live = true;
    setPaid('waiting');
    waitForPaid(() => live, params);
    return () => {
      live = false;
    };
  }, [active]);

  async function checkout(pack) {
    track('credits_checkout_started', { pack: pack.id });
    setBuying(pack.id);
    setError('');
    try {
      const { url } = await api.request('/v1/billing/checkout', {
        method: 'POST',
        body: withAnalytics({ pack_id: pack.id }),
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
          <BalanceNote credits={credits} standard={standard} n={n} />
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
              <Button
                key={pack.id}
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
              </Button>
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
                  </small>
                  <b className={entry.onHold ? 'muted' : entry.shown > 0 ? 'good' : ''}>
                    {entry.shown > 0 ? '+' : '−'}
                    {n(Math.abs(entry.shown))}
                  </b>
                </div>
              );
            })}
          </div>
        ) : (
          <p className="muted">{t('credits.noHistory')}</p>
        )}
        {log.next && (
          <Button variant="ghost" size="sm" onClick={() => void more()}>
            {t('credits.more')}
          </Button>
        )}
        <small className="muted">{t('credits.support')}</small>
      </div>

      {receipts.length > 0 && (
        <div className="card-box">
          <h4>
            <CreditCard size={14} aria-hidden="true" /> {t('credits.purchases')}
          </h4>
          <div className="credit-log">
            {receipts.map((receipt) => (
              <div key={receipt.payment_id}>
                <Coins size={14} aria-hidden="true" />
                <span className="clip">
                  {t('credits.receiptItem', { credits: n(receipt.credits) })}
                </span>
                <small className="muted">
                  {apiDate(receipt.paid_at)?.toLocaleDateString(locale) || ''}
                  {receipt.status !== 'paid' && ' · ' + t('credits.status.' + receipt.status)}
                </small>
                <Button variant="ghost" size="sm" onClick={() => setOpenReceipt(receipt)}>
                  {t('credits.receipt')}
                </Button>
              </div>
            ))}
          </div>
        </div>
      )}
      {openReceipt && <ReceiptDialog receipt={openReceipt} onClose={() => setOpenReceipt(null)} />}
    </div>
  );
}

// An Inkseq purchase record from the original payment; Stripe, as reseller, issues the tax
// receipt. The browser prints it or saves a PDF.
function ReceiptDialog({ receipt, onClose }) {
  const { t, locale } = useI18n();
  const issuer = receipt.issuer || {};
  const rows = [
    [t('credits.receiptNo'), receipt.payment_id],
    [t('credits.receiptDate'), apiDate(receipt.paid_at)?.toLocaleDateString(locale) || ''],
    [
      t('credits.receiptFor'),
      t('credits.receiptItem', { credits: formatCredits(receipt.credits, locale) }),
    ],
    // Tax-exclusive packs (CR-74): the pack price, the tax Stripe added, and the amount paid.
    ...(receipt.tax_cents > 0
      ? [
          [t('credits.receiptPack'), formatPrice(receipt.base_cents, receipt.currency)],
          [t('credits.receiptTax'), formatPrice(receipt.tax_cents, receipt.currency)],
        ]
      : []),
    [t('credits.receiptAmount'), formatPrice(receipt.amount_cents, receipt.currency)],
    ...(receipt.refunded_cents > 0
      ? [[t('credits.status.refunded'), formatPrice(receipt.refunded_cents, receipt.currency)]]
      : []),
  ];
  return (
    <Dialog title={t('credits.receiptTitle')} onClose={onClose} className="receipt-sheet">
      <p>
        <b>{issuer.name || 'Inkseq'}</b>
        {issuer.support_email && ' · ' + issuer.support_email}
      </p>
      <dl className="receipt-rows">
        {rows.map(([label, value]) => (
          <React.Fragment key={label}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </React.Fragment>
        ))}
      </dl>
      <small className="muted">{t('credits.receiptGst')}</small>
      <DialogActions>
        <Button onClick={onClose}>{t('credits.close')}</Button>
        <Button variant="primary" onClick={() => window.print()}>
          {t('credits.print')}
        </Button>
      </DialogActions>
    </Dialog>
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
  if (!block) return null;
  const close = () => setBlock(null);
  return (
    <Dialog title={t('credits.lowTitle')} onClose={close} portal className="low-credits">
      <p>
        {t('credits.lowBody', {
          need: formatCredits(block.need, locale),
          have: formatCredits(block.have, locale),
        })}
      </p>
      <p className="muted">{t('credits.lowHint')}</p>
      <DialogActions>
        <Button onClick={close}>{t('credits.lowBack')}</Button>
        <Button
          variant="primary"
          className="keep-label"
          onClick={() => {
            close();
            navigateApp('/settings?tab=credits');
          }}
        >
          <Coins size={15} aria-hidden="true" />
          {t('credits.lowTopUp')}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
