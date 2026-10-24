import { Note } from './ui/Note.jsx';
import { operatorOfferReadiness as rawOfferReadiness } from './operator-model.js';
import { FloatingMessage } from './floating-notice.js';
import { operatorNodeStatus as rawOperatorNodeStatus } from './operator-presentation.js';
import { operatorFormatters, operatorDisplay } from './i18n/legacyOperatorPresentation.js';
import { useI18n, LanguageSwitcher } from './i18n/I18nProvider.jsx';
import React from 'react';
import {
  ChevronRight,
  Clock,
  Cpu,
  Info,
  Pause,
  Play,
  Server,
  ShieldCheck,
  Square,
} from 'lucide-react';
import {
  age,
  deadlineLabel,
  nodeDeadline,
  nodeRemovalConfirmation,
  dollars,
  reasonText,
  stateLabel,
  stateTone,
  providerLabel,
  operatorOfferReadiness,
  operatorSlotsLabel,
  roundedSpec,
  operatorRankReason,
} from './operator-model.js';
import {
  uniqueOperatorReasons,
  historicalNode,
  operatorNodeStatus,
  operatorBootstrap,
} from './operator-presentation.js';

const known = (value) => (Number.isFinite(value) ? value : '—');
const date = (value) =>
  typeof value === 'number' && Number.isFinite(value)
    ? new Date(value * 1000).toLocaleString('zh-CN')
    : '尚未记录';
const short = (value) => String(value || '尚未确认').slice(0, 12);
export function Reasons({ items = [] }) {
  const { t: lo, locale } = useI18n();
  const { reasonText } = operatorFormatters(locale);
  const reasons = uniqueOperatorReasons(items);
  return reasons.length ? (
    <ul className="op-reasons">
      {reasons.map((item) => {
        const code = typeof item === 'string' ? item : item.code;
        return (
          <li key={code}>
            {reasonText(item)}
            <code>{code}</code>
          </li>
        );
      })}
    </ul>
  ) : null;
}
export function Badge({ state, stale = false }) {
  const { t: lo, locale } = useI18n();
  const { stateLabel } = operatorFormatters(locale);
  return (
    <span className={'op-badge ' + (stale ? 'warn' : stateTone(state))}>
      <i />
      {stale ? lo('legacyOp.copy320') : stateLabel(state)}
    </span>
  );
}
function Status({ node, now }) {
  const { t: lo, locale } = useI18n();
  const { operatorNodeStatus } = operatorFormatters(locale);
  const status = operatorNodeStatus(node, now);
  return (
    <span className={'op-badge ' + status.tone}>
      <i />
      {status.label}
    </span>
  );
}
export function MachineRow({
  node,
  profiles = [],
  now,
  onOpen,
  onExtend,
  onReview,
  disabled = false,
}) {
  const { t: lo, locale } = useI18n();
  const {
    date,
    short,
    age,
    providerLabel,
    dollars,
    nodeDeadline,
    deadlineLabel,
    operatorNodeStatus,
  } = operatorFormatters(locale);
  const history = historicalNode(node),
    profile = profiles.find((p) => p.id === node.runtime_profile_id),
    status = operatorNodeStatus(node, now),
    deadline = nodeDeadline(node, now);
  return (
    <article className={'op-machine-row' + (history ? ' historical' : '')}>
      <button
        type="button"
        className="op-machine-open"
        aria-label={lo('legacyOp.copy321', {
          x0: providerLabel(node.provider),
          x1: node.gpu_model || lo('legacyOp.copy322'),
          x2: short(node.id),
        })}
        onClick={() => onOpen(node)}
      >
        <span className="op-row-icon">
          <Server size={19} />
        </span>
        <span className="op-row-title">
          <b>
            {node.gpu_model || lo('legacyOp.copy323')} <small>× {known(node.gpu_count)}</small>
          </b>
          <small>
            {providerLabel(node.provider)} · {short(node.id)} ·{' '}
            {node.mode === 'fl' ? 'FL2VA' : node.mode === 'ref' ? 'Ref2VA' : lo('legacyOp.copy324')}
          </small>
          <small>{profile?.label || node.runtime_profile_id || lo('legacyOp.copy325')}</small>
        </span>
        <span className="op-row-state">
          <Status node={node} now={now} />
          <small>
            {history
              ? lo('legacyOp.copy326')
              : rawOperatorNodeStatus(node, now).label === '状态待核对'
                ? lo('legacyOp.copy327')
                : lo('legacyOp.copy278')}{' '}
            · {age(node.observed_at, now)}
          </small>
        </span>
        <span className="op-row-window">
          <b>{history ? lo('legacyOp.copy328') : deadline.label}</b>
          <small>
            {history ? date(node.hard_deadline) : deadlineLabel(deadline.deadline, now)}
          </small>
          <small>
            {history ? lo('legacyOp.copy329') : lo('legacyOp.copy87')}{' '}
            {dollars(node.hourly_cost_microusd)} {lo('legacyOp.copy114')}
          </small>
        </span>
        <ChevronRight size={17} />
      </button>
      {!history && node.actions?.extend && (
        <button
          type="button"
          className="op-row-extend"
          disabled={disabled}
          onClick={() => onExtend(node)}
        >
          <Clock size={14} />
          {lo('legacyOp.copy330')}
        </button>
      )}
      {node.actions?.manual_review?.allowed && (
        <button
          type="button"
          className="op-row-extend"
          disabled={disabled}
          onClick={() => onReview(node)}
        >
          <ShieldCheck size={14} />
          {lo('legacyOp.copy19')}
        </button>
      )}
    </article>
  );
}
export function NodeCard({ node, profiles = [], disabled, onAction, now = Date.now() }) {
  const { t: lo, locale } = useI18n();
  const {
    date,
    age,
    providerLabel,
    dollars,
    stateLabel,
    reasonText,
    nodeDeadline,
    nodeRemovalConfirmation,
    deadlineLabel,
    operatorBootstrap,
  } = operatorFormatters(locale);
  const profile = profiles.find((p) => p.id === node.runtime_profile_id),
    stopDeadline = nodeDeadline(node, now),
    removal = nodeRemovalConfirmation(node),
    history = historicalNode(node),
    bootstrap = operatorBootstrap(node, now);
  const actionReasons = [
    ...(node.actions?.drain?.allowed ? [] : node.actions?.drain?.blockers || []),
    ...(node.actions?.stop?.allowed ? [] : node.actions?.stop?.blockers || []),
  ];
  return (
    <article className={'op-node' + (history ? ' historical' : '')}>
      <header>
        <div className="op-node-icon">
          <Server size={22} />
        </div>
        <div className="op-node-title">
          <h3>
            {node.gpu_model || lo('legacyOp.copy323')} <small>× {known(node.gpu_count)}</small>
          </h3>
          <p>
            {providerLabel(node.provider)} · {node.id}
          </p>
        </div>
        <Status node={node} now={now} />
      </header>
      <div className="op-node-facts">
        <div>
          <label>
            {history || removal
              ? lo('legacyOp.copy331')
              : node.hourly_cost_basis === 'approved_ceiling'
                ? lo('legacyOp.copy87')
                : lo('legacyOp.copy332')}
          </label>
          <strong>
            {dollars(node.hourly_cost_microusd)}
            <small> {lo('legacyOp.copy114')}</small>
          </strong>
        </div>
        <div>
          <label>
            {history
              ? lo('legacyOp.copy333')
              : removal
                ? lo('legacyOp.copy44')
                : stopDeadline.label}
          </label>
          <strong className="op-small-value">
            {history || removal
              ? date(node.hard_deadline)
              : deadlineLabel(stopDeadline.deadline, now)}
          </strong>
        </div>
        <div>
          <label>{lo('legacyOp.copy334')}</label>
          <strong className="op-small-value">
            {profile?.label || node.runtime_profile_id || lo('legacyOp.copy335')}
            <small>
              {' '}
              ·{' '}
              {node.mode === 'fl'
                ? lo('legacy.projection15')
                : node.mode === 'ref'
                  ? lo('cloud.shot4')
                  : lo('legacyOp.copy336')}
            </small>
          </strong>
        </div>
      </div>
      {history && <p className="op-note op-history-context">{lo('legacyOp.copy337')}</p>}
      {removal && (
        <div className={'op-banner op-removal ' + removal.tone} role="status">
          <Info size={17} />
          <div>
            <b>{removal.label}</b>
            <p>{removal.message}</p>
            {node.actions?.manual_review?.allowed && <p>{lo('legacyOp.copy338')}</p>}
            <p>
              {lo('legacyOp.copy339')}
              {date(removal.last_checked_at)} · {age(removal.last_checked_at, now)}
            </p>
            {removal.last_checked_at != null && <p>{removal.observationText}</p>}
            <details className="op-details">
              <summary>{lo('legacyOp.copy340')}</summary>
              <dl>
                <dt>{lo('legacyOp.copy341')}</dt>
                <dd>{date(removal.requested_at)}</dd>
                <dt>{lo('legacyOp.copy342')}</dt>
                <dd>{date(removal.last_observation?.observed_at)}</dd>
                {removal.pending && (
                  <>
                    <dt>{lo('legacyOp.copy343')}</dt>
                    <dd>
                      {removal.next_check_at == null
                        ? lo('legacyOp.copy344')
                        : date(removal.next_check_at)}
                    </dd>
                  </>
                )}
              </dl>
              {removal.reviewed && (
                <p>
                  {lo('legacyOp.copy345')}
                  {removal.manual_review?.actor || lo('legacyOp.copy346')} ·{' '}
                  {date(removal.manual_review?.observed_at)}
                </p>
              )}
              <p>{lo('legacyOp.copy347')}</p>
            </details>
          </div>
        </div>
      )}
      {node.reason_code && !removal && (
        <div className={'op-banner op-removal ' + (history ? 'neutral' : 'warn')} role="status">
          <Info size={17} />
          <div>
            <b>
              {history ? lo('legacyOp.copy348') : ''}
              {reasonText(node.reason_code)}
            </b>
            <p>
              {history
                ? lo('legacyOp.copy349', { x0: date(node.observed_at) })
                : lo('legacyOp.copy350')}
            </p>
            <code>{node.reason_code}</code>
          </div>
        </div>
      )}
      <details
        className="op-details op-bootstrap"
        open={
          !history &&
          ['starting', 'preparing', 'blocked', 'failed', 'bootstrapping'].includes(
            node.runtime_state,
          )
        }
      >
        <summary>
          {history ? lo('legacyOp.copy351') : lo('legacyOp.copy352')} ·{' '}
          {bootstrap.freshness === 'fresh'
            ? lo('legacyOp.copy353')
            : bootstrap.freshness === 'stale'
              ? lo('legacyOp.copy354')
              : lo('legacyOp.copy355')}
        </summary>
        <p>
          {date(bootstrap.observed_at)} · {age(bootstrap.observed_at, now)}
          {bootstrap.freshness !== 'fresh' && !history ? lo('legacyOp.copy356') : ''}
        </p>
        {bootstrap.slots.length ? (
          bootstrap.slots.map((slot) => (
            <p key={slot.index}>
              <b>
                {lo('legacyOp.copy357')} {slot.index + 1}
              </b>{' '}
              · {slot.label}
              {slot.error_code && <> · {reasonText(slot.error_code)}</>}
              <br />
              <small>
                {lo('legacyOp.copy358')} <code>{slot.phase || 'unknown'}</code>
                {slot.failure_phase && (
                  <>
                    {' '}
                    {lo('legacyOp.copy359')} <code>{slot.failure_phase}</code>
                  </>
                )}
                {slot.error_code && (
                  <>
                    {' '}
                    {lo('legacyOp.copy360')} <code>{slot.error_code}</code> {lo('legacyOp.copy361')}{' '}
                    <code>{slot.error_type || 'UnknownSetupError'}</code>
                  </>
                )}
              </small>
            </p>
          ))
        ) : (
          <p>{lo('legacyOp.copy362')}</p>
        )}
        <p>{history ? lo('legacyOp.copy363') : lo('legacyOp.copy364')}</p>
      </details>
      <div className="op-slots">
        {history && node.slots?.length > 0 && <Note>{lo('legacyOp.copy365')}</Note>}
        {node.slots?.length ? (
          node.slots.map((slot, index) => (
            <div className="op-slot" key={slot.id}>
              <div>
                <Cpu size={15} />
                <b>
                  {lo('legacyOp.copy357')} {index + 1}
                </b>
                {history || removal ? (
                  <span className="op-badge neutral">
                    {history ? lo('legacyOp.copy366') : lo('legacyOp.copy367')} ·{' '}
                    {stateLabel(slot.state)}
                  </span>
                ) : slot.admission_allowed === false ? (
                  <span className="op-badge warn">
                    <i />
                    {lo('legacyOp.copy368')}
                  </span>
                ) : (
                  <Badge state={slot.state} stale={slot.stale} />
                )}
              </div>
              <p>
                {slot.current_job_id ? (
                  <>
                    {history
                      ? lo('legacyOp.copy369')
                      : removal
                        ? lo('legacyOp.copy370')
                        : lo('legacyOp.copy371')}{' '}
                    <code>{slot.current_job_id}</code>
                  </>
                ) : history || removal ? (
                  lo('legacyOp.copy372')
                ) : (
                  lo('legacyOp.copy373')
                )}{' '}
                · GPU {(slot.gpu_ids || []).join('、') || lo('legacyOp.copy313')}
              </p>
              <small>{slot.configuration_id || lo('legacyOp.copy374')}</small>
              {!history && !removal && slot.admission_allowed === false && (
                <Note>
                  {lo('legacyOp.copy375')}{' '}
                  {reasonText(slot.admission_reason_code || 'matching_worker_unavailable')}
                  <br />
                  <code>{slot.admission_reason_code || 'matching_worker_unavailable'}</code>
                </Note>
              )}
            </div>
          ))
        ) : (
          <Note>{history ? lo('legacyOp.copy376') : lo('legacyOp.copy377')}</Note>
        )}
      </div>
      <details className="op-details">
        <summary>{lo('legacyOp.copy378')}</summary>
        <dl>
          <dt>{lo('legacyOp.copy379')}</dt>
          <dd>
            {node.state === 'ready' ? lo('legacyOp.copy380') : stateLabel(node.state)} ·{' '}
            {node.state || lo('legacyOp.copy381')}
          </dd>
          <dt>{lo('legacyOp.copy382')}</dt>
          <dd>
            {node.runtime_state === 'ready'
              ? lo('legacyOp.copy383')
              : stateLabel(node.runtime_state)}{' '}
            · {node.runtime_state || lo('legacyOp.copy384')}
          </dd>
          <dt>{lo('legacyOp.copy23')}</dt>
          <dd>{node.provider_instance_id || lo('legacyOp.copy140')}</dd>
          <dt>{lo('legacyOp.copy278')}</dt>
          <dd>
            {date(node.observed_at)} · {age(node.observed_at, now)}
          </dd>
          <dt>{lo('legacyOp.copy385')}</dt>
          <dd>{date(node.hard_deadline)}</dd>
          <dt>{lo('legacyOp.copy386')}</dt>
          <dd>
            {history
              ? lo('legacyOp.copy326')
              : stopDeadline.verified
                ? lo('legacyOp.copy387')
                : lo('legacyOp.copy388')}{' '}
            · {date(node.provider_lifetime_observed_at)}
          </dd>
          <dt>{lo('legacyOp.copy389')}</dt>
          <dd>{node.version}</dd>
        </dl>
        <p>{lo('legacyOp.copy390')}</p>
      </details>
      <footer>
        <button
          disabled={disabled || !node.actions?.drain?.allowed}
          onClick={() => onAction('drain', node)}
        >
          <Pause size={14} />
          {lo('legacyOp.copy207')}
        </button>
        <button
          disabled={disabled || !node.actions?.stop?.allowed}
          onClick={() => onAction('stop', node)}
        >
          <Square size={13} />
          {lo('legacyOp.copy208')}
        </button>
        {!history && node.actions?.extend && (
          <button disabled={disabled} onClick={() => onAction('extend', node)}>
            <Clock size={14} />
            {lo('legacyOp.copy330')}
          </button>
        )}
        {node.actions?.manual_review?.allowed && (
          <button disabled={disabled} onClick={() => onAction('manualReview', node)}>
            <ShieldCheck size={14} />
            {lo('legacyOp.copy19')}
          </button>
        )}
      </footer>
      {!history && !node.actions?.manual_review?.allowed && (
        <Reasons items={node.actions?.manual_review?.blockers || []} />
      )}
      <Reasons items={actionReasons} />
    </article>
  );
}

export function CandidateCard({ row, index, current, busy, onStart }) {
  const { t: lo, locale } = useI18n();
  const {
    date,
    providerLabel,
    dollars,
    operatorOfferReadiness,
    operatorSlotsLabel,
    roundedSpec,
    operatorRankReason,
  } = operatorFormatters(locale);
  const stock = Number.isFinite(row.available_count) && row.available_count > 0,
    readiness = !stock
      ? Number.isFinite(row.available_count)
        ? lo('legacyOp.copy391')
        : lo('legacyOp.copy392')
      : rawOfferReadiness(row),
    blocked = !!row.blockers?.length || readiness !== '待启动预览';
  return (
    <article className="op-candidate">
      <header>
        <span className="op-candidate-rank">{String(index + 1).padStart(2, '0')}</span>
        <div>
          <span className="op-candidate-provider">
            {providerLabel(row.provider)} ·{' '}
            {row.offer_kind === 'resource_sku' ? lo('legacyOp.copy393') : lo('legacyOp.copy394')}
          </span>
          <h4>
            {row.gpu_type} <span>× {known(row.gpu_count)}</span>
          </h4>
        </div>
        <span className={'op-badge ' + (current && readiness === '待启动预览' ? 'good' : 'warn')}>
          {current ? operatorDisplay(readiness, locale) : lo('legacyOp.copy395')}
        </span>
      </header>
      <div className="op-candidate-price">
        <strong>
          {dollars(row.hourly_cost_microusd, { digits: 3 })}
          <small> {lo('legacyOp.copy114')}</small>
        </strong>
        <span>
          {lo('legacyOp.copy396')} {known(row.gpu_count)} {lo('legacyOp.copy397')}
          <br />
          {lo('legacyOp.copy398')} {dollars(row.price_per_gpu_hour_microusd, { digits: 3 })}{' '}
          {lo('legacyOp.copy114')}
        </span>
      </div>
      <dl className="op-candidate-specs">
        <div>
          <dt>{lo('legacyOp.copy399')}</dt>
          <dd>
            {roundedSpec(row.ram_gib, 'GiB')}
            {Number.isFinite(row.allocation_ram_gib) && row.allocation_ram_gib !== row.ram_gib && (
              <small>
                {lo('legacyOp.copy96')} {roundedSpec(row.allocation_ram_gib, 'GiB')}
              </small>
            )}
          </dd>
        </div>
        <div>
          <dt>{lo('legacyOp.copy400')}</dt>
          <dd>{roundedSpec(row.disk_gib, 'GiB')}</dd>
        </div>
        <div>
          <dt>{lo('legacyOp.copy401')}</dt>
          <dd>{roundedSpec(row.download_mbps, 'Mbps')}</dd>
        </div>
        <div>
          <dt>{lo('legacyOp.copy402')}</dt>
          <dd>
            {known(row.available_count)}{' '}
            {row.offer_kind === 'resource_sku' ? lo('legacyOp.copy403') : lo('legacyOp.copy55')}
          </dd>
        </div>
      </dl>
      <p className="op-candidate-slots">
        <Cpu size={13} />
        {operatorSlotsLabel(row)}
      </p>
      <div className="op-rank-reasons">
        {(row.rank_reasons || [])
          .map(operatorRankReason)
          .filter(Boolean)
          .map((text) => (
            <span key={text}>{text}</span>
          ))}
      </div>
      <Reasons items={row.blockers || []} />
      {row.preference_hints?.length > 0 && (
        <Note>{row.preference_hints.map(reasonText).join(' ')}</Note>
      )}
      <details className="op-details">
        <summary>
          {row.offer_kind === 'resource_sku' ? lo('legacyOp.copy404') : lo('legacyOp.copy378')}
        </summary>
        <p>
          <code>{row.offer_id}</code>
        </p>
        <p>{row.offer_kind === 'resource_sku' ? lo('legacyOp.copy405') : lo('legacyOp.copy406')}</p>
        <p>
          {lo('legacyOp.copy407')} {date(row.observed_at)}
          {row.country && ` · ${row.country}`}
        </p>
      </details>
      <footer>
        <span>
          {!current
            ? lo('legacyOp.copy408')
            : !stock
              ? readiness
              : blocked
                ? lo('legacyOp.copy409')
                : lo('legacyOp.copy410')}
        </span>
        <button
          className="op-primary"
          type="button"
          disabled={busy || !current || !stock || blocked}
          onClick={() => onStart(row)}
        >
          <Play size={13} />
          {row.offer_kind === 'resource_sku' ? lo('legacyOp.copy411') : lo('legacyOp.copy412')}
        </button>
      </footer>
    </article>
  );
}
