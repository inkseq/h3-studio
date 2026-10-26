import React, { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import QuickChatPreferences from './QuickChatPreferences.jsx';
import { checkedPreferences } from './quick-chat-takes.js';
import { useI18n } from './i18n/I18nProvider.jsx';
import { accountKey } from './data/query.js';

// Takes per generation and how many show before folding: one owner preference shared by
// Works and agents. It lived in the original Quick Create, removed 2026-10-24.
// Read and saved through the shared data layer (#514 R-C): only the Save button waits.
export default function TakePreferencesSettings({ api, account, active }) {
  const { t } = useI18n(),
    client = useQueryClient(),
    key = accountKey(account, 'quick-chat-preferences'),
    [saved, setSaved] = useState(false);
  const read = useQuery({
    queryKey: key,
    queryFn: async () => checkedPreferences(await api.request('/v1/quick-chat/preferences')),
    enabled: !!(active && api && account),
  });
  const save = useMutation({
    mutationFn: (body) =>
      api.request('/v1/quick-chat/preferences', {
        method: 'PATCH',
        body,
        key: 'take-preferences-' + crypto.randomUUID(),
      }),
    onSuccess: (result) => {
      client.setQueryData(key, checkedPreferences(result));
      setSaved(true);
    },
  });
  if (read.isError) return <p role="alert">{t('takes.preferencesError')}</p>;
  if (!read.data) return null;
  return (
    <>
      <QuickChatPreferences
        key={read.data.version}
        controller={{ savePreferences: (body) => save.mutateAsync(body) }}
        value={read.data}
        busy={save.isPending}
        onSaved={() => {}}
      />
      {saved && <p role="status">{t('takes.saved')}</p>}
    </>
  );
}
