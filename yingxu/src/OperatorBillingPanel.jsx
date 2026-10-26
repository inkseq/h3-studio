import { Note } from './ui/Note.jsx';
import React, { useEffect, useState } from 'react';
import { useI18n } from './i18n/I18nProvider.jsx';
import { formatMoneyMicros as money } from './money.js';
import { usdInput, usdMicrousd } from './operator-budget-model.js';
import {
  billingAllocationInputs,
  billingReferenceKey,
  billingTimeBounds,
  validBillingRequest,
  validBillingPreview,
} from './operator-billing-model.js';

export function BillingPreviewTotals({ preview }) {
  const { t } = useI18n();
  return (
    <dl>
      {preview.request.action === 'settle' && (
        <>
          <div>
            <dt>{t('billing.providerCost')}</dt>
            <dd>{money(preview.totals.instance_actual_microusd)}</dd>
          </div>
          <div>
            <dt>{t('billing.allocated')}</dt>
            <dd>{money(preview.totals.job_allocated_microusd)}</dd>
          </div>
        </>
      )}
      <div>
        <dt>{t('billing.waiverExposure')}</dt>
        <dd>{money(preview.totals.waived_exposure_microusd)}</dd>
      </div>
    </dl>
  );
}

export default function OperatorBillingPanel({ controller, state, busy }) {
  const { t } = useI18n(),
    [owner, setOwner] = useState(state.account || ''),
    [selected, setSelected] = useState({}),
    [action, setAction] = useState('waive'),
    [reason, setReason] = useState(''),
    [evidence, setEvidence] = useState(''),
    [evidenceHash, setEvidenceHash] = useState(''),
    [amounts, setAmounts] = useState({}),
    [allocations, setAllocations] = useState({}),
    [preview, setPreview] = useState(null),
    [confirmed, setConfirmed] = useState(false),
    [reading, setReading] = useState(false),
    [error, setError] = useState(''),
    [notice, setNotice] = useState(''),
    [allocationInput, setAllocationInput] = useState(''),
    [createdAfter, setCreatedAfter] = useState(''),
    [createdBefore, setCreatedBefore] = useState('');
  const authorized = state.snapshot?.operator?.admin === true,
    page = state.billingReferences;
  const bounds = billingTimeBounds(createdAfter, createdBefore),
    filter = bounds && { owner_id: owner, ...bounds };
  const shown =
    page?.owner_id === owner &&
    filter &&
    JSON.stringify(state.billingFilter) === JSON.stringify(filter)
      ? page
      : null;
  useEffect(() => {
    setOwner(state.account || '');
    setSelected({});
    setPreview(null);
    setConfirmed(false);
    setError('');
    setNotice('');
  }, [state.account]);
  function invalidate() {
    setPreview(null);
    setConfirmed(false);
    setNotice('');
  }
  async function read(after) {
    if (!bounds) return;
    setReading(true);
    setError('');
    invalidate();
    try {
      await controller.readBillingReferences({ ...filter, ...(after ? { after } : {}) });
      if (!after) setSelected({});
    } catch (failure) {
      setError(failure.message);
    } finally {
      setReading(false);
    }
  }
  const rows = Object.values(selected),
    current =
      shown &&
      rows.every((row) =>
        shown.items.some(
          (item) =>
            billingReferenceKey(item) === billingReferenceKey(row) &&
            item.snapshot === row.snapshot &&
            item.eligible_actions.includes(action),
        ),
      );
  const request = {
    owner_id: owner,
    action,
    reason: reason.trim(),
    evidence_sha256: evidenceHash.trim(),
    evidence_note: evidence.trim(),
    items: rows.map((row) => ({
      reference_type: row.reference_type,
      reference_id: row.reference_id,
      expected_snapshot: row.snapshot,
      ...(action === 'settle'
        ? {
            actual_cost_microusd: usdMicrousd(amounts[billingReferenceKey(row)] || ''),
            ...(row.reference_type === 'job'
              ? { allocation_instance_id: (allocations[billingReferenceKey(row)] || '').trim() }
              : {}),
          }
        : {}),
    })),
  };
  const valid = current && validBillingRequest(request),
    locked = busy || reading;
  async function prepare() {
    setReading(true);
    setError('');
    setNotice('');
    try {
      const value = await controller.previewBilling(request);
      setPreview(value);
      setConfirmed(false);
    } catch (failure) {
      setError(failure.message);
    } finally {
      setReading(false);
    }
  }
  async function apply() {
    setError('');
    try {
      const result = await controller.applyBilling(preview);
      setNotice(t('billing.completed', { id: result.audit_event_id }));
      setPreview(null);
      setSelected({});
      setConfirmed(false);
    } catch (failure) {
      setError(failure.message);
    }
  }
  function importAllocations() {
    try {
      const result = billingAllocationInputs(allocationInput, rows);
      if (!result) throw Error();
      setAmounts(
        Object.fromEntries(
          Object.entries(result).map(([key, value]) => [key, usdInput(value.amount)]),
        ),
      );
      setAllocations(
        Object.fromEntries(
          Object.entries(result)
            .filter(([, value]) => value.allocation)
            .map(([key, value]) => [key, value.allocation]),
        ),
      );
      invalidate();
      setError('');
    } catch {
      setError(t('billing.invalidAllocations'));
    }
  }
  if (!authorized) return null;
  return (
    <section className="billing-admin" aria-label={t('billing.title')}>
      <header>
        <h3>{t('billing.title')}</h3>
        <p>{t('billing.help')}</p>
      </header>
      <form
        className="billing-owner"
        onSubmit={(event) => {
          event.preventDefault();
          void read();
        }}
      >
        <label>
          {t('billing.owner')}
          <input
            value={owner}
            disabled={locked}
            autoComplete="off"
            onChange={(event) => {
              setOwner(event.target.value);
              setSelected({});
              invalidate();
            }}
          />
        </label>
        <button type="submit" disabled={locked || !owner.trim() || !bounds}>
          {t(reading ? 'billing.loading' : 'billing.load')}
        </button>
      </form>
      <details className="billing-date-filter">
        <summary>{t('billing.dateFilter')}</summary>
        <Note>{t('billing.dateFilterHelp')}</Note>
        <label>
          {t('billing.createdAfter')}
          <input
            value={createdAfter}
            placeholder="2026-10-17T00:00:00+11:00"
            disabled={locked}
            onChange={(event) => {
              setCreatedAfter(event.target.value);
              setSelected({});
              invalidate();
            }}
          />
        </label>
        <label>
          {t('billing.createdBefore')}
          <input
            value={createdBefore}
            placeholder="2026-10-18T00:00:00+11:00"
            disabled={locked}
            onChange={(event) => {
              setCreatedBefore(event.target.value);
              setSelected({});
              invalidate();
            }}
          />
        </label>
        {!bounds && <p role="alert">{t('billing.dateInvalid')}</p>}
      </details>
      {shown && (
        <>
          <label>
            {t('billing.action')}
            <select
              value={action}
              disabled={locked}
              onChange={(event) => {
                setAction(event.target.value);
                setSelected({});
                invalidate();
              }}
            >
              <option value="waive">{t('billing.waive')}</option>
              <option value="settle">{t('billing.settle')}</option>
            </select>
          </label>
          <div className="billing-selection">
            <p>
              {t('billing.loaded', { count: shown.items.length })} ·{' '}
              {t('billing.selected', { count: rows.length })}
            </p>
            <button
              type="button"
              disabled={locked || !shown.items.some((row) => row.eligible_actions.includes(action))}
              onClick={() => {
                setSelected(
                  Object.fromEntries(
                    shown.items
                      .filter((row) => row.eligible_actions.includes(action))
                      .slice(0, 200)
                      .map((row) => [billingReferenceKey(row), row]),
                  ),
                );
                invalidate();
              }}
            >
              {t('billing.selectEligible')}
            </button>
            <button
              type="button"
              disabled={locked || !rows.length}
              onClick={() => {
                setSelected({});
                invalidate();
              }}
            >
              {t('billing.clearSelection')}
            </button>
          </div>
          <div className="billing-rows">
            {shown.items.map((row) => {
              const key = billingReferenceKey(row),
                eligible = row.eligible_actions.includes(action);
              return (
                <section className="billing-row" key={key}>
                  <div className="billing-row-title">
                    <label>
                      <input
                        type="checkbox"
                        disabled={locked || !eligible}
                        checked={!!selected[key]}
                        onChange={(event) => {
                          setSelected((previous) => {
                            const next = { ...previous };
                            if (event.target.checked) next[key] = row;
                            else delete next[key];
                            return next;
                          });
                          invalidate();
                        }}
                      />
                      {t('billing.kind.' + row.reference_type)}{' '}
                      <span>{row.reference_id.slice(0, 8)}</span>
                    </label>
                    {row.waived && <b>{t('billing.waived')}</b>}
                  </div>
                  <dl>
                    <div>
                      <dt>{t('billing.providerCost')}</dt>
                      <dd>
                        {row.provider_actual_cost_microusd === null
                          ? t('billing.unknown')
                          : money(row.provider_actual_cost_microusd)}
                      </dd>
                    </div>
                    <div>
                      <dt>{t('billing.userCharge')}</dt>
                      <dd>
                        {row.user_charge_microusd === null
                          ? t('billing.unknown')
                          : money(row.user_charge_microusd)}
                      </dd>
                    </div>
                    {row.reference_type === 'job' && (
                      <div>
                        <dt>{t('billing.allocated')}</dt>
                        <dd>
                          {row.allocated_cost_microusd === null
                            ? t('billing.unknown')
                            : money(row.allocated_cost_microusd)}
                        </dd>
                      </div>
                    )}
                  </dl>
                  {!eligible && <Note>{t('billing.unavailable')}</Note>}
                  {selected[key] && action === 'settle' && (
                    <div className="billing-item-inputs">
                      <label>
                        {t(
                          row.reference_type === 'job'
                            ? 'billing.allocationAmount'
                            : 'billing.actualAmount',
                        )}
                        <input
                          inputMode="decimal"
                          value={amounts[key] || ''}
                          disabled={locked}
                          onChange={(event) => {
                            setAmounts({ ...amounts, [key]: event.target.value });
                            invalidate();
                          }}
                        />
                      </label>
                      {row.reference_type === 'job' && (
                        <label>
                          {t('billing.allocationInstance')}
                          <input
                            value={allocations[key] || ''}
                            disabled={locked}
                            onChange={(event) => {
                              setAllocations({ ...allocations, [key]: event.target.value });
                              invalidate();
                            }}
                          />
                        </label>
                      )}
                    </div>
                  )}
                  <details>
                    <summary>{t('billing.details')}</summary>
                    <dl>
                      <div>
                        <dt>{t('billing.reference')}</dt>
                        <dd>{row.reference_id}</dd>
                      </div>
                      <div>
                        <dt>{t('billing.provider')}</dt>
                        <dd>{row.provider || t('billing.unknown')}</dd>
                      </div>
                      <div>
                        <dt>{t('billing.instance')}</dt>
                        <dd>{row.provider_instance_id || t('billing.unknown')}</dd>
                      </div>
                      <div>
                        <dt>{t('billing.state')}</dt>
                        <dd>{row.state}</dd>
                      </div>
                      {row.block_reason && (
                        <div>
                          <dt>{t('billing.blockReason')}</dt>
                          <dd>{row.block_reason}</dd>
                        </div>
                      )}
                    </dl>
                  </details>
                </section>
              );
            })}
          </div>
          {shown.next_cursor && (
            <button type="button" disabled={locked} onClick={() => void read(shown.next_cursor)}>
              {t('billing.more')}
            </button>
          )}
          <div className="billing-action-form">
            {action === 'settle' && (
              <details>
                <summary>{t('billing.importAllocations')}</summary>
                <Note>{t('billing.importHelp')}</Note>
                <label>
                  {t('billing.allocationJson')}
                  <textarea
                    value={allocationInput}
                    disabled={locked}
                    onChange={(event) => setAllocationInput(event.target.value)}
                  />
                </label>
                <button
                  type="button"
                  disabled={locked || !rows.length || !allocationInput.trim()}
                  onClick={importAllocations}
                >
                  {t('billing.applyAllocations')}
                </button>
              </details>
            )}
            <label>
              {t('billing.reason')}
              <textarea
                value={reason}
                maxLength={1000}
                disabled={locked}
                onChange={(event) => {
                  setReason(event.target.value);
                  invalidate();
                }}
              />
            </label>
            <label>
              {t('billing.evidence')}
              <textarea
                value={evidence}
                maxLength={2000}
                disabled={locked}
                onChange={(event) => {
                  setEvidence(event.target.value);
                  invalidate();
                }}
              />
            </label>
            <label>
              {t('billing.evidenceHash')}
              <input
                value={evidenceHash}
                disabled={locked}
                onChange={(event) => {
                  setEvidenceHash(event.target.value);
                  invalidate();
                }}
              />
            </label>
            <Note>{t(action === 'waive' ? 'billing.waiveHelp' : 'billing.settleHelp')}</Note>
            <button type="button" disabled={locked || !valid} onClick={() => void prepare()}>
              {t('billing.preview', { count: rows.length })}
            </button>
          </div>
        </>
      )}
      {preview && (
        <section className="billing-preview" aria-label={t('billing.previewTitle')}>
          <h4>{t('billing.previewTitle')}</h4>
          <p>
            {t('billing.previewAction', {
              action: t('billing.' + preview.request.action),
              owner: preview.request.owner_id,
              count: preview.totals.reference_count,
            })}
          </p>
          <p>{preview.request.reason}</p>
          <BillingPreviewTotals preview={preview} />
          <Note>{t('billing.layers')}</Note>
          <label className="billing-confirm">
            <input
              type="checkbox"
              checked={confirmed}
              disabled={locked || !valid}
              onChange={(event) => setConfirmed(event.target.checked)}
            />
            {t('billing.confirm')}
          </label>
          <button
            type="button"
            disabled={locked || !valid || !confirmed || !validBillingPreview(preview, request)}
            onClick={() => void apply()}
          >
            {t('billing.submit')}
          </button>
        </section>
      )}
      {error && <p role="alert">{error}</p>}
      {notice && <p role="status">{notice}</p>}
    </section>
  );
}
