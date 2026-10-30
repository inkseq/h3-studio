import { useI18n } from './i18n/I18nProvider.jsx';
import { profileLabel } from './model-visibility.js';
import React from 'react';

export function DeploymentProfileSelect({ capabilities, value, onChange, disabled = false, mode }) {
  const { t, locale } = useI18n();
  const profiles = capabilities?.deployment_profiles || [],
    known = profiles.some((p) => p.id === value),
    profile = profiles.find((p) => p.id === value),
    support = profile?.generation_support?.[mode];
  if (!profiles.length && !value) return null;
  return (
    <label className="qc-field qc-deployment-field">
      <span>{t('deployment.title')}</span>
      <select
        aria-label={t('deployment.title')}
        value={value || ''}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value || null)}
      >
        <option value="">{t('deployment.default')}</option>
        {value && !known && <option value={value}>{t('deployment.private')}</option>}
        {profiles.map((p) => (
          <option key={p.id} value={p.id}>
            {profileLabel(p, locale)}
          </option>
        ))}
      </select>
      {support?.enabled === false && <small>{t('deployment.disabled')}</small>}
      <small>{t('deployment.help')}</small>
    </label>
  );
}
