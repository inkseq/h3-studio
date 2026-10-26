import { Note } from './ui/Note.jsx';
import { Disclosure } from './ui/Disclosure.jsx';
import { Button } from './ui/Button.jsx';
import StudioHeader from './StudioHeader.jsx';
import { Dialog as SharedDialog } from './ui/Dialog.jsx';
import { FloatingMessage } from './floating-notice.js';
import { operatorFormatters } from './i18n/legacyOperatorPresentation.js';
import { useI18n, LanguageSwitcher } from './i18n/I18nProvider.jsx';
import React, {
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
  useSyncExternalStore,
} from 'react';
import {
  ArrowLeft,
  ArrowUpRight,
  Clock,
  Info,
  LogOut,
  Play,
  Plus,
  RefreshCw,
  Search,
  Server,
  Settings2,
  ShieldCheck,
} from 'lucide-react';
import { useCloud } from './CloudStudio.jsx';
import { cloudController as cloud } from './cloud-controller.js';
import { createOperatorController } from './operator-controller.js';
import {
  allowed,
  hasBoundOperatorSelection,
  staleSnapshot,
  operatorModels,
  operatorCandidateKey,
  operatorCandidateSelection,
  operatorCandidateCurrent,
  operatorCandidatePreviewMatches,
  initialOperatorMarket,
  operatorMarketReducer,
} from './operator-model.js';
import { usePageActive } from './AppRouteCache.jsx';
import './v2/ops-adapter.css';
import {
  operatorNodeGroups,
  operatorCapacitySummary,
  filterOperatorHistory,
  operatorExtensionCurrent,
  operatorExtensionReadback,
} from './operator-presentation.js';
import { Reasons, Badge, NodeCard, MachineRow, CandidateCard } from './operator-machine-view.jsx';
import DstackCapacity from './DstackCapacity.jsx';
export { NodeCard } from './operator-machine-view.jsx';

const quietly = (promise) => promise.catch(() => {});
const known = (value) => (Number.isFinite(value) ? value : '—');
// The console's dialogs use the shared Dialog (h3-studio#514); op-dialog keeps their content layout.
function Dialog({ title, subtitle, onClose, children, wide = false }) {
  return (
    <SharedDialog
      title={title}
      subtitle={subtitle}
      onClose={onClose}
      eyebrow="GPU CONTROL"
      size={wide ? 'xl' : 'wide'}
      className={'op-dialog' + (wide ? ' op-market-dialog' : '')}
    >
      {children}
    </SharedDialog>
  );
}
function RecipeEvidence({ profile }) {
  const { t: lo, locale } = useI18n();
  const { duration } = operatorFormatters(locale);
  return (
    <Disclosure className="op-details" summary={lo('legacyOp.copy2')}>
      <p>{lo('legacyOp.copy3')}</p>
      <div className="op-table-wrap">
        <table>
          <thead>
            <tr>
              <th>{lo('legacyOp.copy4')}</th>
              <th>{lo('legacyOp.copy5')}</th>
              <th>{lo('legacyOp.copy6')}</th>
              <th>{lo('legacyOp.copy7')}</th>
            </tr>
          </thead>
          <tbody>
            {(profile.verified_cases || []).flatMap((row) =>
              (row.measurements || []).map((m, index) => (
                <tr key={row.id + '-' + index}>
                  <td>
                    {row.mode === 'fl' ? lo('legacy.projection15') : lo('cloud.shot4')}
                    <small>{(row.input_roles || []).join(' · ')}</small>
                  </td>
                  <td>
                    {row.width} × {row.height}
                    <small>
                      {row.frames} {lo('legacyOp.copy10')} {row.fps} fps
                    </small>
                  </td>
                  <td>{row.steps}</td>
                  <td>
                    {duration(m.total_seconds)}
                    <small>
                      {m.measured_at?.slice(0, 10)} · n={m.sample_count}
                    </small>
                  </td>
                </tr>
              )),
            )}
          </tbody>
        </table>
      </div>
      {!profile.verified_cases?.length && <p>{lo('legacyOp.copy11')}</p>}
      {profile.warnings?.map((warning, index) => (
        <p key={index}>{warning}</p>
      ))}
      <p>
        {lo('legacyOp.copy12')}
        <code>{profile.id}</code>
      </p>
    </Disclosure>
  );
}
function Login() {
  const { t: lo } = useI18n();
  const c = useCloud(),
    [username, setUsername] = useState(''),
    [password, setPassword] = useState(''),
    [error, setError] = useState('');
  return (
    <section className="op-empty op-login">
      <ShieldCheck size={32} />
      <h2>{lo('legacyOp.copy13')}</h2>
      <p>{lo('legacyOp.copy14')}</p>
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          setError('');
          try {
            await cloud.login(username, password);
          } catch (error) {
            setError(error.message);
          }
        }}
      >
        <label>
          {lo('workspace.copy3')}
          <input
            autoComplete="username"
            required
            value={username}
            onChange={(e) => setUsername(e.target.value)}
          />
        </label>
        {!['username-only-test', 'username-test'].includes(c.auth?.authentication) && (
          <label>
            {lo('workspace.copy4')}
            <input
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </label>
        )}
        {['username-only-test', 'username-test'].includes(c.auth?.authentication) && (
          <Note>{lo('legacyOp.copy17')}</Note>
        )}
        <FloatingMessage tone="error" text={error ? error : null} />
        <Button
          type="submit"
          className="op-primary"
          disabled={c.busy || !c.auth || c.auth.auth_ready === false}
        >
          {lo('legacyOp.copy18')}
        </Button>
      </form>
    </section>
  );
}
function ManualReviewDialog({ node, busy, onClose, onReview }) {
  const { t: lo, locale } = useI18n();
  const { providerLabel } = operatorFormatters(locale);
  const [absent, setAbsent] = useState(false),
    [noCharge, setNoCharge] = useState(false),
    [error, setError] = useState('');
  return (
    <Dialog title={lo('legacyOp.copy19')} subtitle={lo('legacyOp.copy20')} onClose={onClose}>
      <p>
        {lo('legacyOp.copy21')} {providerLabel(node.provider)} {lo('legacyOp.copy22')}
      </p>
      <p>
        {node.provider_instance_id ? lo('legacyOp.copy23') : lo('legacyOp.copy24')}：
        <code>{node.provider_instance_id ?? node.id}</code>
      </p>
      {node.provider_instance_id == null && <Note>{lo('legacyOp.copy25')}</Note>}
      <label className="op-check">
        <input type="checkbox" checked={absent} onChange={(e) => setAbsent(e.target.checked)} />
        {lo('legacyOp.copy26')}
      </label>
      <label className="op-check">
        <input type="checkbox" checked={noCharge} onChange={(e) => setNoCharge(e.target.checked)} />
        {lo('legacyOp.copy27')}
      </label>
      <Note>{lo('legacyOp.copy28')}</Note>
      <FloatingMessage tone="error" text={error ? error : null} />
      <footer className="op-dialog-actions">
        <Button onClick={onClose}>{lo('legacy.panel36')}</Button>
        <Button
          className="op-primary"
          disabled={busy || !absent || !noCharge}
          onClick={async () => {
            try {
              await onReview(node, { account_absent: absent, no_continuing_charge: noCharge });
              onClose();
            } catch (error) {
              setError(error.message);
            }
          }}
        >
          {lo('legacyOp.copy30')}
        </Button>
      </footer>
    </Dialog>
  );
}
function ExtensionDialog({ node, busy, controller, onClose, now }) {
  const { t: lo, locale } = useI18n();
  const { date, providerLabel, dollars } = operatorFormatters(locale);
  const [minutes, setMinutes] = useState(30),
    [preview, setPreview] = useState(null),
    [error, setError] = useState(''),
    [loading, setLoading] = useState(false),
    [receipt, setReceipt] = useState(null);
  const seconds = minutes * 60,
    valid = Number.isInteger(minutes) && minutes > 0;
  const current = operatorExtensionCurrent(node, preview, seconds, now),
    changed =
      preview &&
      (preview.node_version !== node.version || preview.current_deadline !== node.hard_deadline);
  const supported = !!node.actions?.extend,
    submitted = !!receipt,
    saved = operatorExtensionReadback(node, preview, receipt);
  async function check() {
    setError('');
    setLoading(true);
    setPreview(null);
    try {
      setPreview(await controller.extensionPreview(node, seconds));
    } catch (error) {
      setError(error.message);
    } finally {
      setLoading(false);
    }
  }
  async function confirm() {
    if (!current) return;
    setError('');
    try {
      setReceipt(await controller.extend(preview));
    } catch (error) {
      setError(error.message);
    }
  }
  return (
    <Dialog
      title={lo('legacyOp.copy31')}
      subtitle={`${providerLabel(node.provider)} · ${node.gpu_model || node.id}`}
      onClose={onClose}
    >
      <p>
        {lo('legacyOp.copy32')}
        <b>{date(node.hard_deadline)}</b>
      </p>
      <Note>{lo('legacyOp.copy33')}</Note>
      {!supported ? (
        <p className="op-error">{lo('legacyOp.copy34')}</p>
      ) : (
        <>
          <label>
            {lo('legacyOp.copy35')}
            <div className="op-unit-input">
              <input
                type="number"
                min="1"
                step="1"
                value={minutes}
                disabled={busy || loading || submitted}
                onChange={(e) => {
                  setMinutes(Number(e.target.value));
                  setPreview(null);
                  setError('');
                }}
              />
              <span>{lo('legacyOp.copy36')}</span>
            </div>
          </label>
          <Button disabled={busy || loading || !valid || submitted} onClick={check}>
            <RefreshCw size={14} />
            {loading ? lo('workspace.agentDetail24') : lo('legacyOp.copy38')}
          </Button>
          {!preview && <Reasons items={node.actions.extend.blockers || []} />}
          {preview && (
            <div className="op-preview">
              <b>
                {submitted
                  ? saved
                    ? lo('legacyOp.copy39')
                    : lo('legacyOp.copy40')
                  : changed
                    ? lo('legacyOp.copy41')
                    : current
                      ? lo('legacyOp.copy42')
                      : lo('legacyOp.copy43')}
              </b>
              <dl>
                <dt>{lo('legacyOp.copy44')}</dt>
                <dd>{date(preview.current_deadline)}</dd>
                <dt>{lo('legacyOp.copy45')}</dt>
                <dd>{date(preview.new_deadline)}</dd>
                <dt>{lo('legacyOp.copy46')}</dt>
                <dd>{dollars(preview.estimated_incremental_cost_microusd)}</dd>
                <dt>{lo('legacyOp.copy47')}</dt>
                <dd>{dollars(preview.incremental_reservation_microusd)}</dd>
              </dl>
              <Reasons items={preview.blockers} />
              <Note>
                {lo('legacyOp.copy48')} {date(preview.expires_at)}。
              </Note>
            </div>
          )}
          {submitted && (
            <p role="status" className="op-note">
              {saved ? lo('legacyOp.copy49') : lo('legacyOp.copy50')}
              {lo('legacyOp.copy32')}
              {date(node.hard_deadline)}
              {lo('legacyOp.copy51')}
            </p>
          )}
        </>
      )}
      <FloatingMessage tone="error" text={error ? error : null} />
      <footer className="op-dialog-actions">
        <Button onClick={onClose}>
          {submitted ? lo('storyboard.copy58') : lo('legacy.panel36')}
        </Button>
        {supported && !submitted && (
          <Button className="op-primary" disabled={busy || loading || !current} onClick={confirm}>
            <Clock size={14} />
            {lo('legacyOp.copy53')}
          </Button>
        )}
      </footer>
    </Dialog>
  );
}
function PolicyDialog({ policy, busy, onSave, onClose }) {
  const { t: lo } = useI18n();
  const [draft, setDraft] = useState({ ...policy }),
    [error, setError] = useState('');
  const fields = [
    ['max_instances', lo('legacyOp.copy54'), lo('legacyOp.copy55')],
    ['max_physical_gpus', lo('legacyOp.copy56'), lo('legacyOp.copy57')],
    ['idle_shutdown_seconds', lo('legacyOp.copy58'), lo('legacy.journey55')],
    ['max_ttl_seconds', lo('legacyOp.copy60'), lo('legacy.journey55')],
  ];
  return (
    <Dialog title={lo('legacyOp.copy61')} subtitle={lo('legacyOp.copy62')} onClose={onClose}>
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          setError('');
          try {
            const { version, ...values } = draft;
            await onSave({ ...values, expected_version: policy.version });
            onClose();
          } catch (error) {
            setError(error.message);
          }
        }}
      >
        <label className="op-check">
          <input
            type="checkbox"
            checked={draft.enabled === true}
            onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })}
          />
          {lo('legacyOp.copy63')}
        </label>
        <Note>{lo('legacyOp.copy64')}</Note>
        <div className="op-form-grid">
          {fields.map(([key, label, unit]) => (
            <label key={key}>
              {label}
              <div className="op-unit-input">
                <input
                  type="number"
                  required
                  min={key === 'max_ttl_seconds' ? 120 : key === 'idle_shutdown_seconds' ? 1 : 0}
                  step="1"
                  value={draft[key] ?? ''}
                  onChange={(e) => setDraft({ ...draft, [key]: Number(e.target.value) })}
                />
                <span>{unit}</span>
              </div>
            </label>
          ))}
          <label>
            {lo('legacyOp.copy65')}
            <div className="op-unit-input">
              <input
                type="number"
                required
                min="0"
                step="0.01"
                value={
                  draft.max_hourly_cost_microusd == null ? '' : draft.max_hourly_cost_microusd / 1e6
                }
                onChange={(e) =>
                  setDraft({
                    ...draft,
                    max_hourly_cost_microusd: Math.round(Number(e.target.value) * 1e6),
                  })
                }
              />
              <span>{lo('legacyOp.copy66')}</span>
            </div>
          </label>
        </div>
        <Note>
          {lo('legacyOp.copy67')} {policy.version}。
        </Note>
        <FloatingMessage tone="error" text={error ? error : null} />
        <footer className="op-dialog-actions">
          <Button onClick={onClose}>{lo('legacy.panel36')}</Button>
          <Button type="submit" className="op-primary" disabled={busy}>
            {lo('legacyOp.copy68')}
          </Button>
        </footer>
      </form>
    </Dialog>
  );
}

function FilterFacts({ filters = {} }) {
  const { t: lo, locale } = useI18n();
  const { dollars, roundedSpec } = operatorFormatters(locale);
  const entries = [
    ['min_ram_gib', lo('legacyOp.copy69'), (value) => roundedSpec(value, 'GiB')],
    ['min_disk_gib', lo('legacyOp.copy70'), (value) => roundedSpec(value, 'GiB')],
    ['min_cpu_cores', lo('legacyOp.copy71'), (value) => roundedSpec(value, lo('legacyOp.copy72'))],
    ['min_download_mbps', lo('legacyOp.copy73'), (value) => roundedSpec(value, 'Mbps')],
    [
      'max_price_per_gpu_hour_microusd',
      lo('legacyOp.copy74'),
      (value) => dollars(value, { digits: 3 }) + lo('legacyOp.copy114'),
    ],
    [
      'allowed_countries',
      lo('legacyOp.copy75'),
      (value) => (value.length ? value.join('、') : lo('legacyOp.copy76')),
    ],
  ];
  return (
    <dl className="op-filter-facts">
      {entries
        .filter(
          ([key]) =>
            Array.isArray(filters[key]) || (Number.isFinite(filters[key]) && filters[key] > 0),
        )
        .map(([key, label, format]) => (
          <div key={key}>
            <dt>{label}</dt>
            <dd>{format(filters[key])}</dd>
          </div>
        ))}
    </dl>
  );
}
function MarketFilters({ result, catalog }) {
  const { t: lo, locale } = useI18n();
  const { providerLabel, dollars, roundedSpec } = operatorFormatters(locale);
  if (!result.filters?.length) return null;
  return (
    <Disclosure
      className="op-details op-market-filters"
      summary={
        <>
          {lo('legacyOp.copy77')}
          {result.excluded_count > 0 && lo('legacyOp.copy78', { x0: result.excluded_count })}
        </>
      }
    >
      <p>{lo('legacyOp.copy79')}</p>
      <p>{lo('legacyOp.copy80')}</p>
      {result.filters.map((profile) => (
        <section key={profile.runtime_profile_id}>
          <h4>
            {catalog?.profiles?.find((item) => item.id === profile.runtime_profile_id)?.label ||
              profile.runtime_profile_id}
          </h4>
          <Note>{lo('legacyOp.copy81')}</Note>
          <FilterFacts filters={profile.hard_requirements} />
          <Note>{lo('legacyOp.copy82')}</Note>
          <FilterFacts filters={profile.guidance} />
          {profile.deployments?.length ? (
            <Disclosure
              className="op-details"
              summary={
                <>
                  {lo('legacyOp.copy83')} {profile.deployments.length} {lo('legacyOp.copy84')}
                </>
              }
            >
              {profile.deployments.map((binding) => (
                <section key={binding.binding_id}>
                  <p>
                    <b>
                      {providerLabel(binding.provider)} · {binding.gpu_type} × {binding.gpu_count}
                    </b>{' '}
                    · {binding.enabled ? lo('legacyOp.copy85') : lo('legacyOp.copy86')}
                  </p>
                  <FilterFacts filters={binding.filters} />
                  <p>
                    {lo('legacyOp.copy87')}{' '}
                    {dollars(binding.hourly_cost_ceiling_microusd, { digits: 3 })}{' '}
                    {lo('legacyOp.copy88')} <code>{binding.binding_id}</code>
                  </p>
                </section>
              ))}
            </Disclosure>
          ) : (
            <Note>{lo('legacyOp.copy89')}</Note>
          )}
        </section>
      ))}
      {result.excluded?.length > 0 && (
        <Disclosure
          className="op-details"
          summary={
            <>
              {lo('legacyOp.copy90')} {result.excluded_count} {lo('legacyOp.copy84')}
            </>
          }
        >
          <p>{lo('legacyOp.copy91')}</p>
          <div className="op-table-wrap">
            <table>
              <thead>
                <tr>
                  <th>{lo('legacyOp.copy92')}</th>
                  <th>{lo('legacyOp.copy93')}</th>
                  <th>{lo('legacyOp.copy94')}</th>
                </tr>
              </thead>
              <tbody>
                {result.excluded.map((offer, index) => (
                  <tr
                    key={`${offer.provider}-${offer.offer_id}-${offer.runtime_profile_id}-${index}`}
                  >
                    <td>
                      {providerLabel(offer.provider)} · {offer.gpu_type} × {offer.gpu_count}
                      <small>
                        <code>{offer.offer_id}</code>
                      </small>
                    </td>
                    <td>
                      {lo('legacyOp.copy95')} {roundedSpec(offer.ram_gib, 'GiB')}
                      {Number.isFinite(offer.allocation_ram_gib) &&
                        offer.allocation_ram_gib !== offer.ram_gib && (
                          <small>
                            {lo('legacyOp.copy96')} {roundedSpec(offer.allocation_ram_gib, 'GiB')}
                          </small>
                        )}
                      <small>
                        {lo('operator.copy109')} {roundedSpec(offer.disk_gib, 'GiB')}
                      </small>
                    </td>
                    <td>
                      <Reasons items={offer.blockers} />
                      <small>{offer.runtime_profile_id}</small>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {result.excluded_count > result.excluded.length && (
            <p>
              {lo('legacyOp.copy98')} {result.excluded.length} {lo('legacyOp.copy99')}{' '}
              {result.excluded_count} {lo('legacyOp.copy100')}
            </p>
          )}
        </Disclosure>
      )}
    </Disclosure>
  );
}
function StartDrawer({ catalog, policy, controller, busy, onClose }) {
  const { t: lo, locale } = useI18n();
  const {
    date,
    age,
    duration,
    providerLabel,
    dollars,
    reasonText,
    runtimeDuration,
    operatorMarketProvider,
    operatorMarketSummary,
  } = operatorFormatters(locale);
  const models = useMemo(() => operatorModels(catalog), [catalog]),
    [market, dispatch] = useReducer(operatorMarketReducer, policy, initialOperatorMarket),
    [now, setNow] = useState(Date.now());
  const scanSignal = useRef(new AbortController());
  const { query, result, row, selection, preview, error } = market;
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  useEffect(() => {
    const signal = new AbortController();
    scanSignal.current = signal;
    return () => signal.abort();
  }, []);
  const model = models.find((item) => item.id === query.model_id),
    profile = model?.profiles.find((item) => item.id === selection?.runtime_profile_id);
  const ttl = runtimeDuration(policy, null, query.ttl_seconds),
    expired = !!preview && preview.expires_at * 1000 <= now;
  const current = operatorCandidateCurrent(result, query, row, now),
    matches = operatorCandidatePreviewMatches(selection, preview);
  const canStart =
    !!preview?.can_start &&
    hasBoundOperatorSelection(preview) &&
    matches &&
    !expired &&
    current &&
    !ttl.problem &&
    preview.policy_version === policy.version;
  function change(patch) {
    dispatch({ type: 'change', patch });
  }
  async function scan() {
    const revision = market.revision + 1;
    dispatch({ type: 'scan' });
    try {
      const response = await controller.refreshCandidates(
        query,
        (result) => dispatch({ type: 'refresh-progress', revision, result }),
        scanSignal.current.signal,
      );
      dispatch({ type: 'result', revision, result: response });
    } catch (error) {
      dispatch({ type: 'error', revision, error: error.message });
    }
  }
  async function choose(candidate) {
    const next = operatorCandidateSelection(catalog, query, candidate);
    if (!next || !operatorCandidateCurrent(result, query, candidate, Date.now())) return;
    const revision = market.revision + 1;
    dispatch({ type: 'select', row: candidate, selection: next });
    try {
      const response = await controller.preview(next);
      dispatch({ type: 'preview', revision, preview: response });
      if (!operatorCandidatePreviewMatches(next, response))
        dispatch({ type: 'error', revision, error: lo('legacyOp.copy101') });
    } catch (error) {
      dispatch({ type: 'error', revision, error: error.message });
    }
  }
  async function start() {
    if (!canStart) return;
    try {
      await controller.start(preview);
      onClose();
    } catch (error) {
      dispatch({ type: 'error', revision: market.revision, error: error.message });
    }
  }
  function back() {
    dispatch({ type: 'back' });
  }
  if (selection)
    return (
      <Dialog
        key="start-confirmation"
        title={row?.offer_kind === 'resource_sku' ? lo('legacyOp.copy102') : lo('legacyOp.copy103')}
        subtitle={`${providerLabel(selection.provider)} · ${selection.gpu_type} × ${selection.gpu_count}`}
        onClose={back}
      >
        <Note>
          {model?.label} · {query.mode === 'fl' ? lo('availability.fl') : lo('availability.ref')}{' '}
          {lo('legacyOp.copy106')} {duration(selection.ttl_seconds)}
        </Note>
        {market.loading === 'preview' && <p role="status">{lo('legacyOp.copy107')}</p>}
        {preview && (
          <div className="op-preview">
            <div>
              <b>
                {expired
                  ? lo('legacyOp.copy108')
                  : !matches
                    ? lo('legacyOp.copy109')
                    : !current
                      ? lo('legacyOp.copy110')
                      : canStart
                        ? lo('legacyOp.copy111')
                        : lo('legacyOp.copy112')}
              </b>
            </div>
            <dl>
              <dt>{lo('legacyOp.copy113')}</dt>
              <dd>
                {dollars((preview.selected_offer || row)?.hourly_cost_microusd, { digits: 3 })}{' '}
                {lo('legacyOp.copy114')}
              </dd>
              <dt>{lo('legacyOp.copy115')}</dt>
              <dd>{dollars(preview.reservation_microusd)}</dd>
              <dt>{lo('legacyOp.copy116')}</dt>
              <dd>{duration(selection.ttl_seconds)}</dd>
            </dl>
            <Reasons items={preview.blockers} />
            {preview.policy_version !== policy.version && (
              <p className="op-error">{lo('legacyOp.copy117')}</p>
            )}
            <Disclosure className="op-details" summary={lo('legacyOp.copy118')}>
              <p>
                {lo('legacyOp.copy119')} {dollars(preview.estimated_hourly_cost_microusd)}{' '}
                {lo('legacyOp.copy120')} {date(preview.expires_at)}。
              </p>
              <p>
                {lo('legacyOp.copy121')}
                <code>{selection.offer_id}</code>
              </p>
              {Number.isFinite(preview.minimum_ttl_seconds) && (
                <p>
                  {lo('legacyOp.copy122')}
                  {duration(preview.minimum_ttl_seconds)}
                </p>
              )}
              {profile && <RecipeEvidence profile={profile} />}
            </Disclosure>
            <Note>{lo('legacyOp.copy123')}</Note>
          </div>
        )}
        <FloatingMessage tone="error" text={error ? error : null} />
        <footer className="op-dialog-actions">
          <Button disabled={busy} onClick={back}>
            <ArrowLeft size={14} />
            {lo('legacyOp.copy124')}
          </Button>
          <Button className="op-primary" disabled={busy || !canStart} onClick={start}>
            <Play size={15} />
            {busy ? lo('legacyOp.copy125') : lo('legacyOp.copy126')}
          </Button>
        </footer>
      </Dialog>
    );
  return (
    <Dialog
      key="machine-list"
      wide
      title={lo('legacyOp.copy127')}
      subtitle={lo('legacyOp.copy128')}
      onClose={onClose}
    >
      <fieldset disabled={busy}>
        <section className="op-model-choice">
          <label>
            {lo('legacyOp.copy129')}
            <select
              aria-label={lo('legacyOp.copy129')}
              value={query.model_id}
              onChange={(e) => change({ model_id: e.target.value })}
            >
              <option value="">{lo('legacyOp.copy130')}</option>
              {models.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
          <div>
            <span className="op-field-label">{lo('legacyOp.copy4')}</span>
            <div className="op-mode-options" role="group" aria-label={lo('legacyOp.copy131')}>
              {[
                ['fl', 'FL2VA', lo('legacy.projection15')],
                ['ref', 'Ref2VA', lo('cloud.shot4')],
              ].map(([mode, label, note]) => (
                <Button
                  key={mode}
                  aria-pressed={query.mode === mode}
                  onClick={() => change({ mode })}
                >
                  <b>{label}</b>
                  <small>{note}</small>
                </Button>
              ))}
            </div>
          </div>
        </section>
        {model && (
          <p className="op-model-caption">
            {model.id} {lo('legacyOp.copy132')}
          </p>
        )}
        {ttl.problem && (
          <p className="op-error" role="alert">
            {ttl.problem}
          </p>
        )}
        <div className="op-market-query">
          <Button
            className="op-primary"
            disabled={busy || market.loading === 'candidates' || !model || !!ttl.problem}
            onClick={scan}
          >
            <RefreshCw size={14} />
            {market.loading === 'candidates'
              ? lo('legacyOp.copy133')
              : result
                ? lo('legacyOp.copy134')
                : lo('legacyOp.copy135')}
          </Button>
          <span>{lo('legacyOp.copy136')}</span>
          <Disclosure
            className="op-details op-start-window"
            summary={
              <>
                {lo('legacyOp.copy137')} {duration(query.ttl_seconds)}
              </>
            }
          >
            <label>
              {lo('legacyOp.copy138')}
              <div className="op-unit-input">
                <input
                  type="number"
                  min="2"
                  max={ttl.maximumMinutes ?? undefined}
                  step="1"
                  value={query.ttl_seconds / 60}
                  onChange={(e) => change({ ttl_seconds: Number(e.target.value) * 60 })}
                />
                <span>{lo('legacyOp.copy36')}</span>
              </div>
            </label>
            <p>
              {lo('legacyOp.copy139')} {ttl.maximumMinutes ?? lo('legacyOp.copy140')}{' '}
              {lo('legacyOp.copy141')}
            </p>
          </Disclosure>
        </div>
        {result ? (
          <section className="op-candidates" aria-label={lo('legacyOp.copy142')}>
            <div className="op-candidates-heading">
              <h3>{lo('legacyOp.copy143')}</h3>
              <small>
                {age(result.observed_at, now)}
                {lo('legacyOp.copy144')}
              </small>
            </div>
            <Note role="status">{operatorMarketSummary(result, now)}</Note>
            <div className="op-market-providers" aria-live="polite">
              {['lium', 'targon'].map((provider) => {
                const observation = operatorMarketProvider(result, provider, now);
                return (
                  <Disclosure
                    className="op-details"
                    key={provider}
                    summary={
                      <>
                        <b>{providerLabel(provider)}</b>
                        <span
                          className={
                            'op-badge ' +
                            (observation.fresh || observation.refreshing ? 'neutral' : 'warn')
                          }
                        >
                          {observation.label}
                        </span>
                        <small>{age(observation.observed_at, now)}</small>
                      </>
                    }
                  >
                    {Number.isFinite(observation.refresh_requested_at) && (
                      <p>
                        {lo('legacyOp.copy145')} {date(observation.refresh_requested_at)}
                      </p>
                    )}
                    <p>
                      {lo('legacyOp.copy146')} {date(observation.observed_at)}
                      {(observation.refresh_reason_code || observation.reason_code) && (
                        <>
                          {' · '}
                          {reasonText(observation.refresh_reason_code || observation.reason_code)}
                        </>
                      )}
                    </p>
                  </Disclosure>
                );
              })}
            </div>
            <MarketFilters result={result} catalog={catalog} />
            {result.candidates?.length ? (
              <div className="op-candidate-grid">
                {result.candidates.map((candidate, index) => (
                  <CandidateCard
                    key={operatorCandidateKey(candidate)}
                    row={candidate}
                    index={index}
                    current={operatorCandidateCurrent(result, query, candidate, now)}
                    busy={busy || !operatorCandidateSelection(catalog, query, candidate)}
                    onStart={choose}
                  />
                ))}
              </div>
            ) : (
              <div className="ui-empty ui-empty-compact">
                <Server size={25} />
                <p>{lo('legacyOp.copy147')}</p>
                <small>{lo('legacyOp.copy148')}</small>
              </div>
            )}
          </section>
        ) : (
          <div className="ui-empty ui-empty-compact">
            <Server size={27} />
            <p>
              {market.loading === 'candidates'
                ? lo('legacyOp.copy149')
                : model
                  ? lo('legacyOp.copy150')
                  : lo('legacyOp.copy151')}
            </p>
            <small>{lo('legacyOp.copy152')}</small>
          </div>
        )}
      </fieldset>
      <FloatingMessage tone="error" text={error ? error : null} />
      <footer className="op-dialog-actions">
        <Button onClick={onClose}>{lo('storyboard.copy58')}</Button>
      </footer>
    </Dialog>
  );
}

export default function OperatorConsole() {
  const { t: lo, locale } = useI18n();
  const { age, duration, dollars, stateLabel, reasonText } = operatorFormatters(locale);
  const pageActive = usePageActive();
  const c = useCloud(),
    controller = useMemo(() => createOperatorController(), []),
    state = useSyncExternalStore(controller.subscribe, controller.getState, controller.getState),
    [dialog, setDialog] = useState(null),
    [actionError, setActionError] = useState(''),
    [now, setNow] = useState(Date.now()),
    [machineView, setMachineView] = useState('current'),
    [historySearch, setHistorySearch] = useState(''),
    [historyProvider, setHistoryProvider] = useState('all');
  useEffect(() => {
    quietly(cloud.initialize());
    return () => controller.destroy();
  }, [controller]);
  useEffect(() => {
    if (pageActive) document.title = lo('legacyOp.copy153');
  }, [pageActive, locale, lo]);
  useEffect(() => {
    setDialog(null);
    setActionError('');
    setMachineView('current');
    setHistorySearch('');
    setHistoryProvider('all');
    void controller.setAccount(c.account);
  }, [c.account, controller]);
  useEffect(() => {
    if (!pageActive) return;
    let stopped = false,
      timer;
    const tick = async () => {
      setNow(Date.now());
      if (document.visibilityState !== 'hidden')
        try {
          await controller.poll();
        } catch {
          // A failed refresh already shows in the controller state; the next poll retries.
        }
      if (!stopped) timer = setTimeout(tick, 5000);
    };
    timer = setTimeout(tick, 5000);
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [controller, c.account, pageActive]);
  const snapshot = state.snapshot,
    stale = staleSnapshot(snapshot, now),
    profiles = state.catalog?.profiles || state.catalog?.deployment_profiles || [],
    busy = state.busy || !!state.pending;
  const groups = operatorNodeGroups(snapshot?.nodes),
    capacity = operatorCapacitySummary(snapshot, now),
    history = filterOperatorHistory(groups.history, {
      search: historySearch,
      provider: historyProvider,
    });
  const selectedNode = dialog?.node?.id
    ? (snapshot?.nodes || []).find((node) => node.id === dialog.node.id) || dialog.node
    : null;
  const openNode = (node) => setDialog({ type: 'detail', node });
  const nodeAction = (kind, node) => {
    setActionError('');
    setDialog(kind === 'extend' ? { type: 'extend', node } : { type: 'action', kind, node });
  };
  const row = (node) => (
    <MachineRow
      key={node.id}
      node={node}
      profiles={profiles}
      now={now}
      disabled={busy || stale}
      onOpen={openNode}
      onExtend={(node) => nodeAction('extend', node)}
      onReview={(node) => nodeAction('manualReview', node)}
    />
  );
  const unbound = (snapshot?.operations || []).filter(
    (op) =>
      !(op.node_ids || []).length &&
      ['accepted', 'running', 'waiting', 'unknown'].includes(op.state),
  );
  const refresh = () => quietly(controller.refresh());
  const content = !c.account ? (
    <Login />
  ) : state.denied ? (
    <section className="ui-empty">
      <ShieldCheck size={32} />
      <h2>{lo('legacyOp.copy154')}</h2>
      <p>{lo('legacyOp.copy155')}</p>
      <a href="/quick-chat">
        {lo('legacyOp.copy156')} <ArrowUpRight size={14} />
      </a>
    </section>
  ) : !snapshot ? (
    <section className="ui-empty">
      <Server size={34} />
      <h2>
        {state.loading
          ? lo('legacyOp.copy157')
          : state.unavailable
            ? lo('legacyOp.copy158')
            : lo('legacyOp.copy159')}
      </h2>
      <p>{lo('legacyOp.copy160')}</p>
      <Button onClick={refresh} disabled={state.loading}>
        <RefreshCw size={14} />
        {lo('legacyOp.copy161')}
      </Button>
    </section>
  ) : (
    <>
      <div className="op-overview">
        {[
          [
            lo('legacyOp.copy162'),
            capacity.ready_slots,
            lo('legacyOp.copy163', { x0: capacity.starting_nodes, x1: capacity.unknown_nodes }),
          ],
          [
            lo('legacyOp.copy164'),
            `${known(snapshot.summary?.jobs_running)} / ${known(snapshot.summary?.jobs_waiting)}`,
            lo('legacyOp.copy165'),
          ],
          [
            lo('legacyOp.copy166'),
            `${known(snapshot.summary?.nodes_active)} / ${known(snapshot.summary?.gpus_allocated)}`,
            lo('legacyOp.copy167'),
          ],
          [
            lo('legacyOp.copy168'),
            dollars(snapshot.summary?.hourly_cost_microusd),
            snapshot.summary?.hourly_cost_basis === 'approved_ceiling_including_pending_commands'
              ? lo('legacyOp.copy169')
              : lo('legacyOp.copy170'),
          ],
        ].map(([label, value, note]) => (
          <div key={label}>
            <label>{label}</label>
            <strong>{stale ? '—' : (value ?? '—')}</strong>
            <small>{stale ? lo('legacyOp.copy171') : note}</small>
          </div>
        ))}
      </div>
      <div className={'op-controller-line ' + (stale || snapshot.controller?.stale ? 'warn' : '')}>
        <span>
          <i />
          {snapshot.controller?.stale
            ? lo('legacyOp.copy172')
            : stateLabel(snapshot.controller?.state)}
        </span>
        <span>
          {lo('legacyOp.copy173')}
          {age(snapshot.controller?.last_heartbeat_at, now)}
        </span>
        <span>
          {lo('legacyOp.copy174')}
          {snapshot.policy?.enabled ? lo('legacyOp.copy175') : lo('legacyOp.copy176')}
        </span>
        <span>
          {lo('legacyOp.copy177')} {duration(snapshot.policy?.idle_shutdown_seconds)}{' '}
          {lo('legacyOp.copy178')}
        </span>
      </div>
      <section className="op-machine-section">
        <div className="op-section-heading">
          <div>
            <span className="op-eyebrow">CAPACITY</span>
            <h2>{lo('legacyOp.copy179')}</h2>
            <p>{lo('legacyOp.copy180')}</p>
          </div>
          <Button
            className="op-primary"
            disabled={busy || !allowed(snapshot, 'start', now)}
            onClick={() => setDialog({ type: 'start' })}
          >
            <Plus size={16} />
            {lo('legacyOp.copy181')}
          </Button>
        </div>
        <div className="op-machine-tabs" role="group" aria-label={lo('legacyOp.copy182')}>
          <Button
            aria-pressed={machineView === 'current'}
            onClick={() => setMachineView('current')}
          >
            {lo('legacyOp.copy183')}{' '}
            <span>{groups.current.length + groups.pending_review.length}</span>
          </Button>
          <Button
            aria-pressed={machineView === 'history'}
            onClick={() => setMachineView('history')}
          >
            {lo('legacyOp.copy184')} <span>{groups.history.length}</span>
          </Button>
        </div>
        {machineView === 'current' ? (
          <>
            <div className="op-list-heading">
              <h3>
                {lo('legacyOp.copy185')} <span>{groups.current.length}</span>
              </h3>
              <small>{lo('legacyOp.copy186')}</small>
            </div>
            {groups.current.length || unbound.length ? (
              <div className="op-machine-list">
                {groups.current.map(row)}
                {unbound.map((op) => (
                  <div className="op-operation-row" key={op.id}>
                    <Server size={18} />
                    <div>
                      <b>{lo('legacyOp.copy187')}</b>
                      <small>
                        {stateLabel(op.state)} · {age(op.updated_at, now)} · <code>{op.id}</code>
                      </small>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <div className="ui-empty ui-empty-compact">
                <Server size={22} />
                <p>{lo('legacyOp.copy188')}</p>
              </div>
            )}
            <div className="op-list-heading">
              <h3>
                {lo('legacyOp.copy189')} <span>{groups.pending_review.length}</span>
              </h3>
              <small>{lo('legacyOp.copy190')}</small>
            </div>
            {groups.pending_review.length ? (
              <div className="op-machine-list">{groups.pending_review.map(row)}</div>
            ) : (
              <p className="op-note op-list-note">{lo('legacyOp.copy191')}</p>
            )}
          </>
        ) : (
          <>
            <div className="op-history-filters">
              <label>
                <Search size={15} />
                <input
                  type="search"
                  placeholder={lo('legacyOp.copy192')}
                  aria-label={lo('legacyOp.copy193')}
                  value={historySearch}
                  onChange={(e) => setHistorySearch(e.target.value)}
                />
              </label>
              <select
                aria-label={lo('legacyOp.copy194')}
                value={historyProvider}
                onChange={(e) => setHistoryProvider(e.target.value)}
              >
                <option value="all">{lo('legacyOp.copy195')}</option>
                <option value="lium">Lium</option>
                <option value="targon">Targon</option>
              </select>
              <small>
                {history.length} / {groups.history.length} {lo('legacyOp.copy196')}
              </small>
            </div>
            <Note>{lo('legacyOp.copy197')}</Note>
            {history.length ? (
              <div className="op-machine-list">{history.map(row)}</div>
            ) : (
              <div className="ui-empty ui-empty-compact">
                <Search size={22} />
                <p>{groups.history.length ? lo('legacyOp.copy198') : lo('legacyOp.copy199')}</p>
              </div>
            )}
          </>
        )}
      </section>
      <Disclosure
        className="op-operations op-details"
        summary={
          <>
            {lo('legacyOp.copy200')} {snapshot.operations?.length || 0} {lo('legacyOp.copy196')}
          </>
        }
      >
        <p>{lo('legacyOp.copy201')}</p>
        {snapshot.operations?.length ? (
          <div className="op-table-wrap">
            <table>
              <thead>
                <tr>
                  <th>{lo('legacyOp.copy202')}</th>
                  <th>{lo('legacyOp.copy203')}</th>
                  <th>{lo('legacyOp.copy204')}</th>
                  <th>{lo('legacyOp.copy205')}</th>
                  <th>{lo('legacyOp.copy206')}</th>
                </tr>
              </thead>
              <tbody>
                {snapshot.operations.map((op) => (
                  <tr key={op.id}>
                    <td>
                      {{
                        start: lo('legacyOp.copy181'),
                        drain: lo('legacyOp.copy207'),
                        stop: lo('legacyOp.copy208'),
                        manual_review: lo('legacyOp.copy209'),
                        extend: lo('legacyOp.copy210'),
                      }[op.kind] || op.kind}
                    </td>
                    <td>
                      <Badge state={op.state} />
                      {op.reason_code && (
                        <small>
                          {reasonText(op.reason_code)} <code>{op.reason_code}</code>
                        </small>
                      )}
                    </td>
                    <td>{op.node_ids?.join('、') || lo('legacyOp.copy211')}</td>
                    <td>{age(op.updated_at, now)}</td>
                    <td>
                      <code>{op.id}</code>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Note>{lo('legacyOp.copy212')}</Note>
        )}
      </Disclosure>
    </>
  );
  return (
    <div className="op-shell v2-gpu-shell">
      <StudioHeader account={c.account} active="operator" />
      <a className="skip" href="#operator-main">
        {lo('legacyOp.copy213')}
      </a>
      <aside className="op-sidebar">
        <a className="op-brand" href="/quick-chat">
          Ⅱ <b>{lo('legacyOp.copy214')}</b>
          <small>STUDIO</small>
        </a>
        <a className="op-back" href="/quick-chat">
          <ArrowLeft size={15} />
          {lo('legacyOp.copy215')}
        </a>
        <div className="op-sidebar-label">{lo('legacyOp.copy216')}</div>
        <a className="active" href="/operator" aria-current="page">
          <Server size={17} />
          {lo('legacyOp.copy217')}
        </a>
        <div className="op-side-note">
          <ShieldCheck size={17} />
          <p>{lo('legacyOp.copy218')}</p>
        </div>
        <div className="op-sidebar-account">
          <span>{(c.account || lo('legacyOp.copy219')).slice(0, 1).toUpperCase()}</span>
          <div>
            <b>{c.account || lo('legacyOp.copy220')}</b>
            <small>{lo('legacyOp.copy221')}</small>
          </div>
          {c.account && (
            <Button
              className="op-icon"
              aria-label={lo('legacyOp.copy222')}
              onClick={() => quietly(cloud.logout())}
            >
              <LogOut size={16} />
            </Button>
          )}
        </div>
      </aside>
      <main id="operator-main">
        <header className="op-topbar">
          <LanguageSwitcher />
          <span>{lo('legacyOp.copy223')}</span>
          <div>
            <span>
              {snapshot
                ? lo('legacyOp.copy224', { x0: age(snapshot.observed_at, now) })
                : lo('legacyOp.copy225')}
            </span>
            <Button
              className="op-icon"
              aria-label={lo('legacyOp.copy226')}
              disabled={state.loading || !c.account}
              onClick={refresh}
            >
              <RefreshCw size={17} />
            </Button>
          </div>
        </header>
        <div className="op-content">
          <div className="op-page-heading">
            <div>
              <span className="op-eyebrow">GPU WORKSPACE</span>
              <h1>{lo('legacyOp.copy227')}</h1>
              <p>{lo('legacyOp.copy228')}</p>
            </div>
            <Button
              disabled={busy || !allowed(snapshot, 'update_policy', now)}
              onClick={() => setDialog({ type: 'policy' })}
            >
              <Settings2 size={16} />
              {lo('legacyOp.copy229')}
            </Button>
          </div>
          {state.error && !state.denied && (
            <div className="op-banner warn" role="alert">
              <Info size={17} />
              <span>{state.error}</span>
            </div>
          )}
          {snapshot && stale && (
            <div className="op-banner warn" role="status">
              {lo('legacyOp.copy230')}
            </div>
          )}
          {state.pending && (
            <div className="op-banner warn" role="alert">
              <div>
                <b>{lo('legacyOp.copy231')}</b>
                <p>{lo('legacyOp.copy232')}</p>
              </div>
              <Button disabled={state.busy} onClick={() => quietly(controller.recover())}>
                {lo('legacyOp.copy233')}
              </Button>
            </div>
          )}
          {content}
          {c.account && !state.denied && <DstackCapacity account={c.account} />}
          <footer className="op-page-foot">
            <ShieldCheck size={14} />
            {lo('legacyOp.copy234')}
          </footer>
        </div>
      </main>
      {dialog?.type === 'detail' && selectedNode && (
        <Dialog
          key={'detail-' + selectedNode.id}
          title={lo('legacyOp.copy235')}
          subtitle={lo('legacyOp.copy236')}
          onClose={() => setDialog(null)}
        >
          <NodeCard
            node={selectedNode}
            profiles={profiles}
            now={now}
            disabled={busy || stale}
            onAction={nodeAction}
          />
        </Dialog>
      )}
      {dialog?.type === 'extend' && selectedNode && (
        <ExtensionDialog
          key={'extend-' + selectedNode.id}
          node={selectedNode}
          now={now}
          controller={controller}
          busy={busy || stale}
          onClose={() => setDialog(null)}
        />
      )}
      {dialog?.type === 'start' && snapshot && (
        <StartDrawer
          catalog={state.catalog}
          policy={snapshot.policy}
          controller={controller}
          busy={busy || !allowed(snapshot, 'start', now)}
          onClose={() => setDialog(null)}
        />
      )}
      {dialog?.type === 'policy' && snapshot && (
        <PolicyDialog
          policy={snapshot.policy}
          busy={busy || !allowed(snapshot, 'update_policy', now)}
          onClose={() => setDialog(null)}
          onSave={(body) => controller.updatePolicy(body)}
        />
      )}
      {dialog?.type === 'action' && dialog.kind === 'manualReview' && (
        <ManualReviewDialog
          node={dialog.node}
          busy={busy || stale}
          onClose={() => setDialog(null)}
          onReview={(node, attestation) => controller.manualReview(node, attestation)}
        />
      )}
      {dialog?.type === 'action' && dialog.kind !== 'manualReview' && (
        <Dialog
          title={dialog.kind === 'drain' ? lo('legacyOp.copy237') : lo('legacyOp.copy238')}
          subtitle={dialog.node.gpu_model || dialog.node.id}
          onClose={() => setDialog(null)}
        >
          <p>{dialog.kind === 'drain' ? lo('legacyOp.copy239') : lo('legacyOp.copy240')}</p>
          <Note>{lo('legacyOp.copy241')}</Note>
          <FloatingMessage tone="error" text={actionError ? actionError : null} />
          <footer className="op-dialog-actions">
            <Button onClick={() => setDialog(null)}>{lo('legacy.panel36')}</Button>
            <Button
              className="op-primary"
              disabled={busy || stale}
              onClick={async () => {
                try {
                  await controller[dialog.kind](dialog.node);
                  setDialog(null);
                } catch (error) {
                  setActionError(error.message);
                }
              }}
            >
              {lo('legacyOp.copy242')}
              {dialog.kind === 'drain' ? lo('legacyOp.copy207') : lo('legacyOp.copy208')}
            </Button>
          </footer>
        </Dialog>
      )}
    </div>
  );
}
