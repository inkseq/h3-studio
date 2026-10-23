import React, { useEffect, useState } from 'react';
import { ImagePlus } from 'lucide-react';
import { useI18n } from './i18n/I18nProvider.jsx';

// Admin on/off for story-chat card pictures (h3-studio#466). Hidden where the server has no
// such setting yet. Turning it off never touches pictures already drawn or being settled.
export default function StoryChatAdminSwitch({ api, active }) {
  const { t } = useI18n();
  const [value, setValue] = useState(null);
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  useEffect(() => {
    if (!active) return undefined;
    let live = true;
    api
      .request('/v1/admin/story-chat/settings')
      .then((next) => live && setValue(next))
      .catch(() => live && setValue(null));
    return () => {
      live = false;
    };
  }, [api, active]);
  if (!value) return null;
  const save = async () => {
    setBusy(true);
    setMessage('');
    try {
      const next = await api.request('/v1/admin/story-chat/settings', {
        method: 'PUT',
        body: {
          expected_version: value.version,
          pictures_enabled: !value.pictures_enabled,
          reason: reason.trim(),
        },
      });
      setValue(next);
      setReason('');
      setMessage(t('storyChatAdmin.saved'));
    } catch (failure) {
      const code = failure.detail?.detail?.code;
      if (code === 'story_chat_settings_changed') {
        setValue(await api.request('/v1/admin/story-chat/settings').catch(() => value));
        setMessage(t('storyChatAdmin.changed'));
      } else setMessage(failure.message);
    } finally {
      setBusy(false);
    }
  };
  const on = value.pictures_enabled;
  return (
    <section className="card-box story-chat-admin" aria-label={t('storyChatAdmin.title')}>
      <div className="row tight">
        <ImagePlus size={16} aria-hidden="true" />
        <b>{t('storyChatAdmin.title')}</b>
        <span className={'pill ' + (on ? 'ok' : 'muted')}>
          {on ? t('storyChatAdmin.on') : t('storyChatAdmin.off')}
        </span>
      </div>
      <p className="muted">{t('storyChatAdmin.help')}</p>
      <label className="story-chat-admin-reason">
        {t('storyChatAdmin.reason')}
        <input
          value={reason}
          maxLength={500}
          disabled={busy}
          placeholder={on ? t('storyChatAdmin.reasonOff') : t('storyChatAdmin.reasonOn')}
          onChange={(event) => setReason(event.target.value)}
        />
      </label>
      <div className="row tight" style={{ justifyContent: 'flex-end' }}>
        {message && <small className="muted">{message}</small>}
        <button
          type="button"
          className={on ? 'ghost' : 'primary'}
          disabled={busy || !reason.trim()}
          onClick={save}
        >
          {on ? t('storyChatAdmin.turnOff') : t('storyChatAdmin.turnOn')}
        </button>
      </div>
    </section>
  );
}
