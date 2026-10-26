import { useI18n } from './i18n/I18nProvider.jsx';
import { usePageActive } from './AppRouteCache.jsx';
import React, { useEffect, useMemo, useRef, useState } from 'react';
import { createP4OperatorClient } from './p4-operator-client.js';
import { money, time } from './p4-operator-model.js';
import {
  budgetCosts,
  budgetLabel,
  budgetUsage,
  usdInput,
  usdMicrousd,
} from './operator-budget-model.js';
import './operator-budget.css';
import './v2/ops-adapter.css';
import OperatorBillingPanel from './OperatorBillingPanel.jsx';
import { Consequences } from './CapacitySettings.jsx';
import { Button } from './ui/Button.jsx';

export function BudgetAccounts({ budgets = [], compact = false }) {
  const { t, locale } = useI18n();
  return (
    <div className={'budget-accounts' + (compact ? ' compact' : '')}>
      {budgets.map((budget) => {
        const retired = budget.limit_mode === 'settings' || budget.legacy_projection_only === true,
          usage = budgetUsage(budget),
          pending = budget.invoice_pending,
          costs = budgetCosts(budget);
        const status = retired
          ? 'ok'
          : budget.status === 'exhausted' || usage?.status === 'exhausted'
            ? 'exhausted'
            : budget.status === 'warning' || usage?.status === 'warning'
              ? 'warning'
              : 'ok';
        const amounts = retired
          ? [
              [t('operator.status98'), budget.spent_microusd],
              [t('operatorPanel.executionHold'), budget.reserved_microusd],
            ]
          : [
              [t('operator.status97'), budget.limit_microusd],
              [t('operator.status98'), budget.spent_microusd],
              [t('operator.status99'), budget.reserved_microusd],
              [t('operator.status100'), budget.remaining_microusd],
            ];
        return (
          <section
            key={budget.id}
            className={'budget-account ' + status}
            aria-label={budgetLabel(budget, locale)}
          >
            <header>
              <b>{budgetLabel(budget, locale)}</b>
              {status !== 'ok' && (
                <span role="status">
                  {status === 'exhausted' ? t('operator.status95') : t('operator.status96')}
                </span>
              )}
            </header>
            <dl>
              {amounts.map(([label, value]) => (
                <div key={label}>
                  <dt>{label}</dt>
                  <dd>{money(value)}</dd>
                </div>
              ))}
            </dl>
            <details className="budget-cost-details">
              <summary>{t('settings.costDetails')}</summary>
              {retired && <p>{t('operatorPanel.retiredCap')}</p>}
              <small>{t('operator.copy1')}</small>
              {costs.length > 0 && (
                <div className="budget-cost-breakdown">
                  {costs.map((cost) => (
                    <section key={cost.kind}>
                      <p>
                        <b>
                          {t(
                            cost.kind === 'job'
                              ? 'operatorPanel.confirmedJobInvoices'
                              : 'operatorPanel.confirmedRentalInvoices',
                          )}
                        </b>{' '}
                        · {money(cost.confirmed_cost_microusd)}
                      </p>
                      {cost.pending_invoice_count > 0 && (
                        <p className="budget-pending">
                          {t(
                            cost.kind === 'job'
                              ? 'operatorPanel.pendingJobInvoices'
                              : 'operatorPanel.pendingRentalInvoices',
                            {
                              count: cost.pending_invoice_count,
                              amount: money(cost.pending_estimate_microusd),
                            },
                          )}
                          <br />
                          <small>{t('operatorPanel.pendingCostsUnknown')}</small>
                        </p>
                      )}
                    </section>
                  ))}
                  <small>{t('operatorPanel.separateCostLayers')}</small>
                </div>
              )}
              {!costs.length && pending?.reservation_count > 0 && (
                <p className="budget-pending">
                  {t('operatorPanel.pendingInvoice', {
                    amount: money(pending.reserved_estimate_microusd),
                    count: pending.reservation_count,
                  })}
                  <br />
                  <small>{t('operatorPanel.pendingInvoiceHelp')}</small>
                </p>
              )}
            </details>
            {usage && (
              <progress
                max="100"
                value={usage.percent}
                aria-label={t('operatorPanel.usage', { value: usage.percent })}
              />
            )}
            {!compact && (
              <details>
                <summary>{t('operator.copy2')}</summary>
                {budget.daily7.length ? (
                  <ul>
                    {budget.daily7.map((day) => (
                      <li key={day.date}>
                        <time>{day.date}</time>
                        <b>{money(day.cost_microusd)}</b>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p>{t('operator.copy3')}</p>
                )}
                <p>{t('operator.copy4')}</p>
                {budget.unattributed_legacy && (
                  <p>
                    {t('operatorPanel.legacy', {
                      amount: money(budget.unattributed_legacy.cost_microusd),
                      count: budget.unattributed_legacy.reservation_count ?? '—',
                    })}
                  </p>
                )}
              </details>
            )}
          </section>
        );
      })}
    </div>
  );
}

export function BudgetWorkspace({ account, controller, state, busy }) {
  const { t } = useI18n();
  return (
    <>
      <OperatorBudgetPanel account={account} />
      {state.account === account && state.snapshot?.operator?.admin === true && (
        <>
          <p>
            <a href="/admin?section=limits">{t('operatorPanel.settingsAdmin')}</a>
          </p>
          <AdminBudgetPanel controller={controller} state={state} busy={busy} />
        </>
      )}
    </>
  );
}

// Each mounted view owns an account-bound read client. No budget state is saved
// in the project, draft or generation command journal.
export default function OperatorBudgetPanel({ account, compact = false }) {
  const { t, locale } = useI18n();
  const pageActive = usePageActive(),
    visible = useRef(pageActive);
  visible.current = pageActive;
  const client = useMemo(() => createP4OperatorClient(), []),
    generation = useRef(0),
    [state, setState] = useState({ account: null, value: null, loading: false, error: '' });
  async function read(after_id) {
    if (!account) return;
    const current = ++generation.current;
    setState((previous) => ({
      account,
      value: previous.account === account ? previous.value : null,
      loading: true,
      error: '',
    }));
    try {
      const value = await client.budgets({ after_id });
      if (value.account !== account) throw Error(t('operator.status101'));
      if (generation.current === current)
        setState((previous) => ({
          account,
          value:
            after_id && previous.account === account && previous.value
              ? {
                  ...value,
                  budgets: [
                    ...new Map(
                      [...previous.value.budgets, ...value.budgets].map((budget) => [
                        budget.id,
                        budget,
                      ]),
                    ).values(),
                  ],
                }
              : value,
          loading: false,
          error: '',
        }));
    } catch (error) {
      if (generation.current === current)
        setState({ account, value: null, loading: false, error: error.message });
    }
  }
  useEffect(() => {
    client.setAccount(account);
    generation.current++;
    setState({ account, value: null, loading: false, error: '' });
    if (account) void read();
    const timer = account
      ? setInterval(() => {
          if (visible.current && globalThis.document?.visibilityState !== 'hidden') void read();
        }, 30000)
      : null;
    return () => {
      clearInterval(timer);
      generation.current++;
      client.reset();
    };
  }, [account, client]);
  const value = state.account === account ? state.value : null;
  if (!account) return null;
  return (
    <section
      className={'operator-budget' + (compact ? ' compact' : '')}
      aria-label={t('operator.copy5')}
    >
      <header>
        <div>
          <h3>{t('operator.copy5')}</h3>
          <small>{t('operator.copy6')}</small>
        </div>
        <button type="button" disabled={state.loading} onClick={() => void read()}>
          {state.loading ? t('operator.status70') : t('operator.status102')}
        </button>
      </header>
      {value ? (
        <>
          <BudgetAccounts budgets={value.budgets} compact={compact} />
          <small>{t('operatorPanel.readBudget', { time: time(value.as_of, locale) })}</small>
          {!value.budgets.length && <p>{t('operator.copy7')}</p>}
          {value.next_after_id && (
            <button
              type="button"
              disabled={state.loading}
              onClick={() => void read(value.next_after_id)}
            >
              {t('operator.copy8')}
            </button>
          )}
        </>
      ) : (
        <p role={state.error ? 'alert' : 'status'}>{state.error || t('operator.status103')}</p>
      )}
    </section>
  );
}

function BudgetEditor({ row, busy, onSave }) {
  const { t, locale } = useI18n();
  const [input, setInput] = useState(() => usdInput(row.limit_microusd)),
    [error, setError] = useState('');
  useEffect(() => {
    setInput(usdInput(row.limit_microusd));
    setError('');
  }, [row.id, row.snapshot]);
  const limit = usdMicrousd(input),
    minimum = row.spent_microusd + row.reserved_microusd;
  const retired = row.limit_mode === 'settings' || row.legacy_projection_only === true;
  return (
    <section className="budget-admin-row">
      <h3>{budgetLabel(row, locale)}</h3>
      <details>
        <summary>{t('operatorPanel.accountIdentity')}</summary>
        <small>{row.id}</small>
      </details>
      <BudgetAccounts budgets={[row]} compact />
      <form
        onSubmit={async (event) => {
          event.preventDefault();
          setError('');
          try {
            await onSave(row, limit);
          } catch (error) {
            setError(error.message);
          }
        }}
      >
        <label>
          {t(retired ? 'operatorPanel.historicalLimit' : 'operator.copy9')}
          <input
            aria-label={t('operatorPanel.limitAria', { id: row.id })}
            inputMode="decimal"
            value={input}
            disabled={busy}
            onChange={(event) => setInput(event.target.value)}
          />
        </label>
        <button
          type="submit"
          disabled={busy || limit === null || limit < minimum || limit === row.limit_microusd}
        >
          {t('operator.copy10')}
        </button>
      </form>
      {limit !== null && limit < minimum && (
        <p role="status">{t('operatorPanel.minimum', { amount: money(minimum) })}</p>
      )}
      {error && <p role="alert">{error}</p>}
    </section>
  );
}
function PriceEditor({ row, busy, onPreview, onSave }) {
  const { t } = useI18n();
  const [max, setMax] = useState(() => usdInput(row.max_price_microusd)),
    [ceiling, setCeiling] = useState(() => usdInput(row.price_ceiling_microusd)),
    [review, setReview] = useState(null),
    [reason, setReason] = useState(''),
    [error, setError] = useState('');
  useEffect(() => {
    setMax(usdInput(row.max_price_microusd));
    setCeiling(usdInput(row.price_ceiling_microusd));
    setReview(null);
    setReason('');
    setError('');
  }, [row.id, row.snapshot]);
  const defaultValue = usdMicrousd(max),
    ceilingValue = usdMicrousd(ceiling),
    valid =
      defaultValue !== null &&
      defaultValue > 0 &&
      ceilingValue !== null &&
      ceilingValue >= defaultValue &&
      ceilingValue <= 9000000000000;
  return (
    <section className="budget-admin-row">
      <h3>{row.id}</h3>
      <small>{t('operatorPanel.account', { owner: row.owner_id })}</small>
      <form
        onSubmit={async (event) => {
          event.preventDefault();
          setError('');
          try {
            setReview(await onPreview(row, defaultValue, ceilingValue));
          } catch (error) {
            setError(error.message);
          }
        }}
      >
        <label>
          {t('operator.copy11')}
          <input
            aria-label={t('operatorPanel.priceAria', { id: row.id })}
            inputMode="decimal"
            disabled={busy || !!review}
            value={max}
            onChange={(event) => setMax(event.target.value)}
          />
        </label>
        <label>
          {t('operator.copy12')}
          <input
            aria-label={t('operatorPanel.ceilingAria', { id: row.id })}
            inputMode="decimal"
            disabled={busy || !!review}
            value={ceiling}
            onChange={(event) => setCeiling(event.target.value)}
          />
        </label>
        {!review && (
          <Button
            type="submit"
            disabled={
              busy ||
              !valid ||
              (defaultValue === row.max_price_microusd &&
                ceilingValue === row.price_ceiling_microusd)
            }
          >
            {t('operator.reviewPrice')}
          </Button>
        )}
      </form>
      {!valid && <p role="status">{t('operator.copy14')}</p>}
      {review && (
        <section className="cap-review" aria-label={t('capacity.reviewTitle')}>
          <Consequences preview={review} />
          <label className="cap-reason">
            {t('capacity.reason')}
            <textarea
              rows={2}
              maxLength={500}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
            <small className="muted">{t('capacity.reasonHelp')}</small>
          </label>
          <div className="cap-actions">
            <Button variant="ghost" disabled={busy} onClick={() => setReview(null)}>
              {t('capacity.back')}
            </Button>
            <Button
              variant="primary"
              disabled={busy || !reason.trim()}
              onClick={async () => {
                setError('');
                try {
                  await onSave(row, defaultValue, ceilingValue, { preview: review, reason });
                } catch (error) {
                  setReview(null);
                  setError(error.message);
                }
              }}
            >
              {t('capacity.confirm')}
            </Button>
          </div>
        </section>
      )}
      {error && <p role="alert">{error}</p>}
    </section>
  );
}
export function AdminBudgetPanel({ controller, state, busy }) {
  const { t, locale } = useI18n();
  const [notice, setNotice] = useState(''),
    [reading, setReading] = useState(false);
  const authorized = state.snapshot?.operator?.admin === true;
  async function read(after_id) {
    setReading(true);
    try {
      await controller.readBudgetAdmin({ after_id });
    } catch {
    } finally {
      setReading(false);
    }
  }
  useEffect(() => {
    if (authorized) void read();
  }, [controller, state.account, authorized]);
  async function save(fn) {
    const result = await fn();
    setNotice(
      result.changed
        ? t('operatorPanel.saved', { id: result.audit_event_id || t('operator.status105') })
        : t('operator.status106'),
    );
    await controller.readBudgetAdmin();
  }
  const budgets = state.adminBudgets,
    prices = state.adminPrices,
    audits = state.budgetAudits;
  if (!authorized) return null;
  return (
    <section className="operator-budget" aria-label={t('operator.status124')}>
      <header>
        <div>
          <h3>{t('operator.copy15')}</h3>
          <small>{t('operatorPanel.allBudgetAccounts')}</small>
        </div>
        <button type="button" disabled={busy || reading} onClick={() => void read()}>
          {t(reading ? 'operator.status70' : 'operator.copy17')}
        </button>
      </header>
      {notice && <p role="status">{notice}</p>}
      {budgets ? (
        <>
          {budgets.budgets.map((row) => (
            <BudgetEditor
              key={row.id}
              row={row}
              busy={busy || reading}
              onSave={(row, limit) => save(() => controller.updateBudget(row, limit))}
            />
          ))}
          <p>
            {t(
              budgets.next_after_id
                ? 'operatorPanel.accountsMore'
                : 'operatorPanel.accountsComplete',
              { count: budgets.budgets.length },
            )}
          </p>
          {budgets.next_after_id && (
            <button
              type="button"
              disabled={busy || reading}
              onClick={() => void read(budgets.next_after_id)}
            >
              {t('operatorPanel.loadBudgetAccounts')}
            </button>
          )}
        </>
      ) : (
        <p role="status">{t('operator.copy19')}</p>
      )}
      <OperatorBillingPanel controller={controller} state={state} busy={busy || reading} />
      <h3>{t('operator.copy20')}</h3>
      {prices ? (
        prices.profiles.map((row) => (
          <PriceEditor
            key={row.id}
            row={row}
            busy={busy || reading}
            onPreview={(row, max, ceiling) => controller.previewPrice(row, max, ceiling)}
            onSave={(row, max, ceiling, confirmation) =>
              save(() => controller.updatePrice(row, max, ceiling, confirmation))
            }
          />
        ))
      ) : (
        <p>{t('operator.copy21')}</p>
      )}
      <details className="budget-audits">
        <summary>{t('operator.copy22')}</summary>
        {audits?.events?.length ? (
          <ul>
            {audits.events.map((event) => (
              <li key={event.id}>
                {event.actor?.owner_id || event.actor?.actor_id || t('operator.status107')} ·{' '}
                {time(event.created_at, locale)} · {event.account_id}
                <br />
                {money(event.old?.limit_microusd)} → {money(event.new?.limit_microusd)} · {event.id}
              </li>
            ))}
          </ul>
        ) : (
          <p>{t('operator.copy23')}</p>
        )}
        {audits?.next_before && <p>{t('operator.copy24')}</p>}
      </details>
      <details className="budget-audits">
        <summary>{t('operator.copy25')}</summary>
        {prices?.audits?.length ? (
          <ul>
            {prices.audits.map((event) => (
              <li key={event.id}>
                {event.actor_owner || event.actor_id} · {time(event.created_at, locale)} ·{' '}
                {event.profile_id}
                <br />
                {t('operatorPanel.priceAudit', {
                  oldDefault: money(event.old?.max_price_microusd),
                  newDefault: money(event.new?.max_price_microusd),
                  oldCeiling: money(event.old?.price_ceiling_microusd),
                  newCeiling: money(event.new?.price_ceiling_microusd),
                })}
              </li>
            ))}
          </ul>
        ) : (
          <p>{t('operator.copy26')}</p>
        )}
      </details>
    </section>
  );
}
