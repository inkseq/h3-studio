import React, { useEffect, useState, useEffectEvent } from 'react';
import { Button } from './ui/Button.jsx';
import { ImagePlus } from 'lucide-react';
import { useI18n } from './i18n/I18nProvider.jsx';

// Admin on/off for story-chat card pictures (h3-studio#466). Hidden where the server has no
// such setting yet. Turning it off never touches pictures already drawn or being settled.
// One click toggles it (owner, 2026-10-23); the audit log gets a plain on/off note.
// The per-picture price publishes a new audited price revision with the current tier rates
// unchanged (#463); requests already quoted keep their frozen price.
export default function StoryChatAdminSwitch({ api, active }) {
  const { t } = useI18n();
  const [value, setValue] = useState(null);
  const [prices, setPrices] = useState(null);
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const loadPrices = () =>
    api
      .request('/v1/admin/credits/prices')
      .then((next) => {
        setPrices(next);
        setDraft(String(next.image_credits ?? next.recommended_image_credits ?? ''));
      })
      .catch(() => setPrices(null));
  const loadPricesEvent = useEffectEvent((...args) => loadPrices(...args));
  useEffect(() => {
    if (!active) return undefined;
    let live = true;
    api
      .request('/v1/admin/story-chat/settings')
      .then((next) => live && setValue(next))
      .catch(() => live && setValue(null));
    void loadPricesEvent();
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
          reason: value.pictures_enabled
            ? 'Pictures turned off in Admin'
            : 'Pictures turned on in Admin',
        },
      });
      setValue(next);
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
  const amount = Number(draft);
  const validPrice = /^[1-9]\d{0,5}$/.test(draft.trim());
  const current = prices?.image_credits ?? null;
  const savePrice = async () => {
    setBusy(true);
    setMessage('');
    try {
      await api.request('/v1/admin/credits/prices', {
        method: 'POST',
        key: 'image-price-' + Date.now(),
        body: {
          expected_revision: prices.revision,
          rates: prices.rates,
          image_credits: amount,
          reason: 'Picture price set to ' + amount + ' credits in Admin',
        },
      });
      await loadPrices();
      setMessage(t('storyChatAdmin.priceSaved'));
    } catch (failure) {
      const code = failure.detail?.detail?.code || failure.detail?.code;
      if (code === 'credit_prices_changed') {
        await loadPrices();
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
        <span className="spacer" />
        <Button className={on ? 'ghost' : 'primary'} disabled={busy} onClick={save}>
          {on ? t('storyChatAdmin.turnOff') : t('storyChatAdmin.turnOn')}
        </Button>
      </div>
      <p className="muted">{t('storyChatAdmin.help')}</p>
      {prices && (
        <div className="story-chat-admin-price">
          <label>
            <span>{t('storyChatAdmin.price')}</span>
            <input
              inputMode="numeric"
              value={draft}
              disabled={busy}
              onChange={(event) => setDraft(event.target.value)}
            />
            <span className="muted">{t('storyChatAdmin.perPicture')}</span>
          </label>
          <Button
            variant="ghost"
            disabled={busy || !validPrice || amount === current}
            onClick={() => void savePrice()}
          >
            {t('storyChatAdmin.savePrice')}
          </Button>
          <small className="muted">
            {current == null
              ? t('storyChatAdmin.priceUnset')
              : t('storyChatAdmin.priceCurrent', { count: current })}
          </small>
        </div>
      )}
      {message && (
        <small className="muted" role="status">
          {message}
        </small>
      )}
    </section>
  );
}
