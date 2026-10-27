import React, { useEffect, useEffectEvent, useState } from 'react';
import { SettingRow, SettingsSection } from './ui/Settings.jsx';
import { Disclosure } from './ui/Disclosure.jsx';
import { Button } from './ui/Button.jsx';
import { Badge, Note } from './ui/Note.jsx';
import { Spinner } from './ui/Field.jsx';
import { useI18n } from './i18n/I18nProvider.jsx';
import { duration } from './admin-overview-model.js';
import { apiDate } from './credits.js';
import {
  START_RULE_FIELDS,
  draftFromRules,
  parseRules,
  refusalCode,
  ruleTargets,
  sameRules,
  targetLabels,
} from './start-rules-model.js';

const PATH = '/v1/operator/dstack/admin/start-rules';
const CHANGED = ['start_rules_conflict', 'start_rules_confirmation_changed'];

function RuleChanges({ before, after }) {
  const { t } = useI18n();
  const text = (id, value) => (id === 'minimum_jobs' ? value : duration(value));
  return (
    <ul className="rules-diff">
      {START_RULE_FIELDS.filter(([id]) => before?.[id] !== after[id]).map(([id]) => (
        <li key={id}>
          {t('rules.field.' + id)}: {text(id, before?.[id])} → <b>{text(id, after[id])}</b>
        </li>
      ))}
    </ul>
  );
}

function RuleHistory({ api, profileId }) {
  const { t, locale } = useI18n();
  const [items, setItems] = useState(null);
  const load = () =>
    api
      .request(PATH + '/history?profile_id=' + encodeURIComponent(profileId))
      .then((body) => setItems(body.items || []))
      .catch(() => setItems([]));
  return (
    <Disclosure
      className="admin-fold"
      summary={t('rules.history')}
      onToggle={(event) => event.currentTarget.open && items === null && void load()}
    >
      {items === null ? (
        <Spinner />
      ) : items.length ? (
        <ul className="rules-history">
          {items.map((item) => (
            <li key={item.id}>
              <small className="muted">
                {apiDate(item.at)?.toLocaleString(locale) || ''} ·{' '}
                {String(item.actor_id || '').replace(/^browser:/, '')}
              </small>
              <RuleChanges before={item.before?.value} after={item.after?.value || {}} />
              {item.reason && <small>{item.reason}</small>}
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">{t('rules.noHistory')}</p>
      )}
    </Disclosure>
  );
}

function TargetPicker({ targets, value, busy, onChange }) {
  const { t } = useI18n();
  const { labels, shared } = targetLabels(targets);
  if (targets.length === 1) return <p className="muted rules-target">{labels[targets[0].id]}</p>;
  return (
    <SettingRow label={t('rules.target')} help={t('rules.targetHelp')} meta={shared}>
      <select value={value} disabled={busy} onChange={(event) => onChange(event.target.value)}>
        {targets.map((item) => (
          <option key={item.id} value={item.id}>
            {labels[item.id]}
          </option>
        ))}
      </select>
    </SettingRow>
  );
}

function RuleConfirm({ preview, reason, busy, onReason, onCancel, onSave }) {
  const { t } = useI18n();
  return (
    <form
      className="rules-confirm"
      onSubmit={(event) => {
        event.preventDefault();
        onSave();
      }}
    >
      <b>{t('rules.reviewTitle')}</b>
      <RuleChanges before={preview.before?.value} after={preview.value} />
      <label>
        {t('rules.reason')}
        <input
          value={reason}
          maxLength={500}
          required
          autoFocus
          onChange={(event) => onReason(event.target.value)}
        />
      </label>
      <div className="rules-confirm-actions">
        <Button disabled={busy} onClick={onCancel}>
          {t('takes.cancel')}
        </Button>
        <Button type="submit" variant="primary" disabled={busy || !reason.trim()}>
          {t('rules.save')}
        </Button>
      </div>
    </form>
  );
}

// Admin start rules for the automatic GPU start planner. Until the planner is switched on
// (`execution_connected`), these are prepared settings that nothing uses yet.
export default function StartRulesPanel({ api, active }) {
  const { t } = useI18n();
  const [targets, setTargets] = useState(null),
    [profileId, setProfileId] = useState(''),
    [rules, setRules] = useState(null),
    [draft, setDraft] = useState(draftFromRules()),
    [review, setReview] = useState(null),
    [reason, setReason] = useState(''),
    [busy, setBusy] = useState(false),
    [notice, setNotice] = useState(''),
    [error, setError] = useState('');

  const loadTargets = useEffectEvent(() =>
    api.request('/v1/operator/dstack/catalog').then(
      (body) => {
        const list = ruleTargets(body.profiles);
        setTargets(list);
        setProfileId((current) => current || list[0]?.id || '');
      },
      () => setTargets([]),
    ),
  );
  useEffect(() => {
    if (active) void loadTargets();
  }, [api, active]);

  const loadRules = async () => {
    const body = await api.request(PATH + '?profile_id=' + encodeURIComponent(profileId));
    setRules(body);
    setDraft(draftFromRules(body.value));
    setReview(null);
  };
  const loadRulesEvent = useEffectEvent(loadRules);
  useEffect(() => {
    if (!profileId) return;
    setRules(null);
    setNotice('');
    setError('');
    loadRulesEvent().catch((failure) => setError(failure.message));
  }, [profileId]);

  if (targets === null) return <Spinner />;
  if (!targets.length) return null;

  const parsed = parseRules(draft);
  const changed = rules && parsed.value && !sameRules(parsed.value, rules.value);
  const edit = (id, text) => {
    setReview(null);
    setNotice('');
    setDraft((current) => ({ ...current, [id]: text }));
  };
  const fail = (failure) => {
    const code = refusalCode(failure);
    if (CHANGED.includes(code)) {
      setError(t('rules.err.changed'));
      void loadRules().catch(() => {});
    } else setError(code === 'start_rules_invalid' ? t('rules.err.invalid') : failure.message);
  };
  const run = async (step) => {
    setBusy(true);
    setError('');
    try {
      await step();
    } catch (failure) {
      fail(failure);
    } finally {
      setBusy(false);
    }
  };
  const check = () =>
    run(async () => {
      const body = {
        profile_id: profileId,
        expected_revision: rules.revision,
        value: parsed.value,
      };
      const preview = await api.request(PATH + '/preview', { method: 'POST', body });
      // One key per reviewed change, so a retried save replays instead of writing twice.
      setReview({ body, preview, key: 'start-rules-' + crypto.randomUUID() });
    });
  const save = () =>
    run(async () => {
      const saved = await api.request(PATH, {
        method: 'PUT',
        key: review.key,
        body: {
          ...review.body,
          reason: reason.trim(),
          confirmation_digest: review.preview.confirmation_digest,
        },
      });
      setRules(saved);
      setDraft(draftFromRules(saved.value));
      setReview(null);
      setReason('');
      setNotice(t(saved.execution_connected ? 'rules.saved' : 'rules.savedPrepared'));
    });

  const value = parsed.value || rules?.value;
  return (
    <SettingsSection
      className="rules"
      title={
        <>
          {t('rules.title')}{' '}
          {rules && (
            <Badge tone={rules.execution_connected ? 'accent' : ''}>
              {t(rules.execution_connected ? 'rules.inUse' : 'rules.prepared')}
            </Badge>
          )}
        </>
      }
      help={t('rules.intro')}
      actions={
        review ? null : (
          <Button variant="primary" disabled={busy || !changed} onClick={() => void check()}>
            {t('rules.check')}
          </Button>
        )
      }
    >
      {rules && !rules.execution_connected && <Note tone="info">{t('rules.notConnected')}</Note>}
      <TargetPicker targets={targets} value={profileId} busy={busy} onChange={setProfileId} />
      {!rules ? (
        error ? null : (
          <Spinner />
        )
      ) : (
        <>
          {START_RULE_FIELDS.map(([id, min, max]) => (
            <SettingRow
              key={id}
              label={t('rules.field.' + id)}
              help={t('rules.help.' + id)}
              unit={t(id === 'minimum_jobs' ? 'rules.unit.jobs' : 'rules.unit.seconds')}
              changed={String(rules.value[id]) !== draft[id]}
              extra={
                parsed.error?.id === id && (
                  <small className="ui-setting-error" role="alert">
                    {t('rules.err.range', { min, max })}
                  </small>
                )
              }
            >
              <input
                inputMode="numeric"
                value={draft[id]}
                disabled={busy}
                aria-invalid={parsed.error?.id === id}
                onChange={(event) => edit(id, event.target.value)}
              />
            </SettingRow>
          ))}
          {value && (
            <p className="rules-summary">
              {t('rules.summary', {
                boot: duration(value.boot_seconds),
                jobs: value.minimum_jobs,
                job: duration(value.job_seconds),
              })}
            </p>
          )}
        </>
      )}
      {review && (
        <RuleConfirm
          preview={review.preview}
          reason={reason}
          busy={busy}
          onReason={setReason}
          onCancel={() => setReview(null)}
          onSave={() => void save()}
        />
      )}
      {error && (
        <p className="ui-setting-error" role="alert">
          {error}
        </p>
      )}
      {notice && <Note tone="info">{notice}</Note>}
      {rules && <RuleHistory key={profileId} api={api} profileId={profileId} />}
    </SettingsSection>
  );
}
