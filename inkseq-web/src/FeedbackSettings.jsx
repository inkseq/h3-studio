import React, { useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useI18n } from './i18n/I18nProvider.jsx';
import { accountKey } from './data/query.js';
import { SettingRow, SettingsSection } from './ui/Settings.jsx';
import { readFeedbackSettings, saveFeedbackSettings } from './api/feedback.js';

export const checkedFeedbackSettings = (value) =>
  value && Number.isInteger(value.revision) && typeof value.enabled === 'boolean'
    ? { revision: value.revision, enabled: value.enabled }
    : null;

// Whether Inky and connected assistants may send short product feedback (h3-studio#635).
// Owner-only: agents can never turn it back on. The box flips at once and the server follows;
// a click during a save is sent after it, and a failed or conflicting save puts the saved
// value back and says so. The box never greys out; it waits as a placeholder until read.
export default function FeedbackSettings({ api, account, active }) {
  const { t } = useI18n(),
    client = useQueryClient(),
    key = accountKey(account, 'feedback-settings'),
    [notice, setNotice] = useState(''),
    queued = useRef(null),
    saved = useRef(null);
  const read = useQuery({
    queryKey: key,
    queryFn: async () => checkedFeedbackSettings(await readFeedbackSettings(api)),
    enabled: !!(active && api && account),
  });
  const save = useMutation({
    mutationFn: ({ enabled, revision }) =>
      saveFeedbackSettings(api, { enabled, revision }, 'feedback-settings-' + crypto.randomUUID()),
    onSuccess: (result) => {
      const value = checkedFeedbackSettings(result);
      if (!value) return;
      saved.current = value;
      const next = queued.current;
      queued.current = null;
      if (next !== null && next !== value.enabled) {
        save.mutate({ enabled: next, revision: value.revision });
        return;
      }
      client.setQueryData(key, value);
      setNotice(t('feedbackSettings.saved'));
    },
    onError: (error) => {
      queued.current = null;
      if (saved.current) client.setQueryData(key, saved.current);
      if (error?.status === 409) void client.invalidateQueries({ queryKey: key });
      setNotice(t(error?.status === 409 ? 'feedbackSettings.changed' : 'feedbackSettings.error'));
    },
  });
  if (!account) return null;
  const value = read.data;
  const change = (enabled) => {
    setNotice('');
    client.setQueryData(key, { ...value, enabled });
    if (save.isPending) {
      queued.current = enabled;
      return;
    }
    saved.current = value;
    save.mutate({ enabled, revision: value.revision });
  };
  return (
    <SettingsSection
      className="feedback-settings"
      title={t('feedbackSettings.title')}
      help={t('feedbackSettings.help')}
    >
      <SettingRow
        label={t('feedbackSettings.allow')}
        help={t('feedbackSettings.allowHelp')}
        extra={
          <small className="muted" role="status">
            {/* The line is always there, so the box never shifts when a note appears. */}
            {(read.isError ? t('feedbackSettings.error') : notice) || ' '}
          </small>
        }
      >
        {value ? (
          <input
            type="checkbox"
            checked={value.enabled}
            onChange={(event) => change(event.target.checked)}
          />
        ) : (
          <i className="ui-wait feedback-settings-wait" />
        )}
      </SettingRow>
    </SettingsSection>
  );
}
