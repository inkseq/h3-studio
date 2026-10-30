import React, { useState } from 'react';
import { Button } from './ui/Button.jsx';
import { useI18n } from './i18n/I18nProvider.jsx';
import { preferenceBody, takeError } from './quick-chat-takes.js';
import { SettingRow, SettingsSection } from './ui/Settings.jsx';
export default function QuickChatPreferences({ controller, value, onSaved, busy }) {
  const { t } = useI18n(),
    [copies, setCopies] = useState(String(value.copies_max)),
    [fold, setFold] = useState(String(value.takes_fold_count)),
    [error, setError] = useState('');
  async function save(event) {
    event.preventDefault();
    setError('');
    try {
      const result = await controller.savePreferences(preferenceBody(value, copies, fold));
      onSaved(result);
    } catch (e) {
      setError(takeError(e, t));
    }
  }
  const number = (value, onChange) => (
    <input
      type="number"
      min="1"
      step="1"
      max="2147483647"
      value={value}
      disabled={busy}
      onChange={(e) => onChange(e.target.value)}
    />
  );
  return (
    <form onSubmit={save}>
      <SettingsSection
        title={t('takes.preferences')}
        help={t('takes.preferencesHelp')}
        actions={
          <Button variant="primary" type="submit" disabled={busy}>
            {t('takes.save')}
          </Button>
        }
      >
        <SettingRow label={t('takes.copiesMax')}>{number(copies, setCopies)}</SettingRow>
        <SettingRow label={t('takes.foldCount')}>{number(fold, setFold)}</SettingRow>
        {error && (
          <p role="alert" className="qc-error ui-settings-error">
            {error}
          </p>
        )}
      </SettingsSection>
    </form>
  );
}
