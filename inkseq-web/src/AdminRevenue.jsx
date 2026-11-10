// Revenue card on Admin → Pricing (h3-studio#485): what customers paid in the last 7, 30 or
// 90 days, from GET /v1/admin/revenue. The card keeps its shape while a period loads; each
// period is read once and kept, so switching back is instant.
import React, { useEffect, useState } from 'react';
import { Segmented } from './ui/Segmented.jsx';
import { useI18n } from './i18n/I18nProvider.jsx';
import { formatCredits } from './credits.js';
import { REVENUE_DAYS, revenueCells, revenueDetail, testOrders } from './admin-money-model.js';

const pct = (value) =>
  typeof value === 'number' && Number.isFinite(value) ? Math.round(value * 100) + '%' : '—';

export function AdminRevenue({ api, active }) {
  const { t, locale } = useI18n();
  const [days, setDays] = useState(30),
    [periods, setPeriods] = useState({}),
    [state, setState] = useState('loading');
  const body = periods[days];
  useEffect(() => {
    if (!active || body) return undefined;
    let live = true;
    api
      .request('/v1/admin/revenue?days=' + days)
      .then((value) => {
        if (!live) return;
        setPeriods((previous) => ({ ...previous, [days]: value }));
        setState('ready');
      })
      .catch((failure) => live && setState(failure.status === 404 ? 'missing' : 'error'));
    return () => {
      live = false;
    };
  }, [api, active, days, body]);
  const cells = revenueCells(body),
    detail = revenueDetail(body),
    test = testOrders(body),
    count = (value) => (value == null ? '—' : formatCredits(value, locale)),
    fees = body?.net_estimate;
  const message =
    state === 'missing'
      ? t('admin.notDeployed')
      : state === 'error'
        ? t('admin.revenueError')
        : body && !body.payments_ready
          ? t('admin.revenueNotReady')
          : null;
  return (
    <div className="card-box admin-stats admin-revenue" aria-busy={!body && state !== 'missing'}>
      <div className="admin-revenue-head">
        <h4>{t('admin.revenue')}</h4>
        <Segmented
          label={t('admin.revenuePeriod')}
          value={days}
          onChange={setDays}
          options={REVENUE_DAYS.map((value) => ({
            value,
            label: t('admin.revenueDays', { count: value }),
          }))}
        />
      </div>
      <dl>
        <div>
          <dt>{t('admin.revenueGross')}</dt>
          <dd>{cells.gross}</dd>
        </div>
        <div>
          <dt>{t('admin.revenueNet')}</dt>
          <dd>{cells.net}</dd>
        </div>
        <div>
          <dt>{t('admin.revenueOrders')}</dt>
          <dd>{count(cells.orders)}</dd>
        </div>
        <div>
          <dt>{t('admin.revenuePayers')}</dt>
          <dd>
            {count(cells.payers)}
            <small className="muted">
              {' '}
              {t('admin.revenueFirst', { count: cells.firstTime ?? 0 })}
            </small>
          </dd>
        </div>
        <div>
          <dt>{t('admin.revenueOutstanding')}</dt>
          <dd>{count(cells.outstanding)}</dd>
        </div>
      </dl>
      <small className="muted admin-revenue-line" role={message ? 'status' : undefined}>
        {message ||
          (detail
            ? t('admin.revenueDetail', {
                refunded: detail.refunded,
                disputes: detail.disputes,
                sold: formatCredits(detail.sold, locale),
                used: formatCredits(detail.used, locale),
              })
            : ' ')}
      </small>
      {test && (
        <small className="muted admin-revenue-line">
          {t('admin.revenueTest', { orders: test.orders, gross: test.gross })}
        </small>
      )}
      <small className="muted admin-revenue-line">
        {t('admin.revenueNote', {
          fee: pct(fees?.fee_rate),
          fixed: fees ? '$' + Number(fees.fixed_fee_usd_per_order).toFixed(2) : '—',
        })}
      </small>
    </div>
  );
}
