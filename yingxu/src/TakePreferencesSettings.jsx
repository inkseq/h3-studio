import React, { useEffect, useState } from 'react';
import { FloatingMessage } from './floating-notice.js';
import QuickChatPreferences from './QuickChatPreferences.jsx';
import { checkedPreferences } from './quick-chat-takes.js';
import { useI18n } from './i18n/I18nProvider.jsx';

// Takes per generation and how many show before folding: one owner preference shared by
// Works and agents. It lived in the original Quick Create, removed 2026-10-24.
export default function TakePreferencesSettings({ api, active }) {
  const { t } = useI18n(),
    [value, setValue] = useState(null),
    [failed, setFailed] = useState(false),
    [saved, setSaved] = useState(false);
  useEffect(() => {
    if (!active || !api) return undefined;
    let live = true;
    setFailed(false);
    api
      .request('/v1/quick-chat/preferences')
      .then((result) => live && setValue(checkedPreferences(result)))
      .catch(() => live && setFailed(true));
    return () => {
      live = false;
    };
  }, [api, active]);
  if (failed) return <p role="alert">{t('takes.preferencesError')}</p>;
  if (!value) return null;
  const controller = {
    savePreferences: (body) =>
      api.request('/v1/quick-chat/preferences', {
        method: 'PATCH',
        body,
        key: 'take-preferences-' + crypto.randomUUID(),
      }),
  };
  return (
    <>
      <QuickChatPreferences
        key={value.version}
        controller={controller}
        value={value}
        busy={false}
        onSaved={(result) => {
          setValue(checkedPreferences(result));
          setSaved(true);
        }}
      />
      {saved && <p role="status">{t('takes.saved')}</p>}
    </>
  );
}
