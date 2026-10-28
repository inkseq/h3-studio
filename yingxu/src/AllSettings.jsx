// Admin → All settings (h3-studio#746): every tunable number on one read-only page, read live
// from where it is stored, with its latest change and a link to the page that edits it.
import React, { useEffect, useState } from 'react';
import { SettingRow, SettingsSection } from './ui/Settings.jsx';
import { Button } from './ui/Button.jsx';
import { Spinner } from './ui/Field.jsx';
import { useI18n } from './i18n/I18nProvider.jsx';
import { navigateApp } from './app-navigation.js';
import { NOT_STORED, capacityRows, priceRows } from './all-settings-model.js';
import { ruleTargets, targetLabels } from './start-rules-model.js';

const SPENDING = '/v1/operator/admin/spending-settings';
const RULES = '/v1/operator/dstack/admin/start-rules';

function useInventory(api, active) {
  const [data, setData] = useState(null);
  useEffect(() => {
    if (!active) return undefined;
    let live = true;
    const read = (path) => api.request(path).catch(() => null);
    (async () => {
      const [spending, prices, packs, policy, catalog] = await Promise.all([
        read(SPENDING),
        read('/v1/admin/credits/prices'),
        read('/v1/billing/packs'),
        read('/v1/legal/policy'),
        read('/v1/operator/dstack/catalog'),
      ]);
      const targets = ruleTargets(catalog?.profiles);
      const rules = await Promise.all(
        targets.map((target) =>
          read(RULES + '?profile_id=' + encodeURIComponent(target.id)).then((body) => ({
            target,
            value: body?.value || null,
          })),
        ),
      );
      if (live) setData({ spending, prices, packs, policy, rules, targets });
    })();
    return () => {
      live = false;
    };
  }, [api, active]);
  return data;
}

function Changed({ change }) {
  const { t, locale } = useI18n();
  if (!change) return null;
  const time = new Intl.DateTimeFormat(locale === 'zh' ? 'zh-CN' : 'en-AU', {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(new Date(change.observed_at * 1000));
  const actor = String(change.actor_id || '').replace(/^browser:/, '');
  return t('capacity.lastChange', { actor, time }) + (change.reason ? ' · ' + change.reason : '');
}

function EditLink({ path }) {
  const { t } = useI18n();
  return (
    <Button size="sm" onClick={() => navigateApp(path)}>
      {t('allSettings.edit')}
    </Button>
  );
}

function capacityLabel(t, id) {
  const [key, sub] = id.split('.');
  return t('capacity.field.' + key) + (sub ? ' · ' + sub : '');
}

export default function AllSettings({ api, active }) {
  const { t } = useI18n();
  const data = useInventory(api, active);
  if (!data) return <Spinner />;
  const { labels } = targetLabels(data.targets);
  return (
    <div className="all-settings">
      <p className="muted">{t('allSettings.intro')}</p>
      <SettingsSection
        title={t('allSettings.capacity')}
        help={t('allSettings.storedDb')}
        actions={<EditLink path="/admin?section=limits" />}
      >
        {capacityRows(data.spending).map((row) => (
          <SettingRow
            as="div"
            key={row.id}
            label={capacityLabel(t, row.id)}
            meta={<Changed change={row.change} />}
            unit={t('capacity.unit.' + row.unit)}
          >
            <b>{row.value || '—'}</b>
          </SettingRow>
        ))}
      </SettingsSection>
      <SettingsSection
        title={t('allSettings.rules')}
        help={t('allSettings.storedDb')}
        actions={<EditLink path="/admin?section=limits" />}
      >
        {data.rules.map(({ target, value }) =>
          ['job_seconds', 'boot_seconds', 'minimum_jobs'].map((id) => (
            <SettingRow
              as="div"
              key={target.id + id}
              label={t('rules.field.' + id)}
              meta={labels[target.id]}
              unit={t(id === 'minimum_jobs' ? 'rules.unit.jobs' : 'rules.unit.seconds')}
            >
              <b>{value?.[id] ?? '—'}</b>
            </SettingRow>
          )),
        )}
      </SettingsSection>
      <SettingsSection
        title={t('allSettings.prices')}
        help={t('allSettings.storedPrices')}
        actions={<EditLink path="/admin?section=pricing" />}
      >
        {priceRows(data.prices, data.packs).map((row) => (
          <SettingRow
            as="div"
            key={row.id}
            label={
              row.tier
                ? t('credits.tier.' + row.tier)
                : row.pack
                  ? t('allSettings.pack', { id: row.pack })
                  : t('allSettings.welcome')
            }
            unit={t('allSettings.unit.' + row.unit)}
          >
            <b>{row.value}</b>
          </SettingRow>
        ))}
        <SettingRow as="div" label={t('allSettings.terms')}>
          <b>{data.policy ? data.policy.version + ' · ' + data.policy.status : '—'}</b>
        </SettingRow>
      </SettingsSection>
      <SettingsSection title={t('allSettings.notStored')} help={t('allSettings.notStoredHelp')}>
        {NOT_STORED.map((row) => (
          <SettingRow
            as="div"
            key={row.id}
            label={t('allSettings.item.' + row.id)}
            meta={t('allSettings.where.' + row.where)}
            unit={t('allSettings.unit.' + row.unit)}
          >
            <b>{row.value}</b>
          </SettingRow>
        ))}
      </SettingsSection>
    </div>
  );
}
