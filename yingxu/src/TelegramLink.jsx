import React, { useEffect, useMemo } from 'react';
import { Button } from './ui/Button.jsx';
import qrcode from 'qrcode-generator';
import { useI18n } from './i18n/I18nProvider.jsx';

export const TELEGRAM_BOT = 'InkseqBot';
// Telegram's start parameter allows 64 characters of [A-Za-z0-9_-]; the one-time code fits.
// The bot's one-time nonce from /connect/telegram?r=… (43–128 URL-safe characters).
export const validTelegramNonce = (value) =>
  typeof value === 'string' && /^[A-Za-z0-9_-]{43,128}$/.test(value);

// Telegram usernames can change, so the id is only the last fallback for display.
export function telegramUserLabel(user) {
  if (!user) return '';
  if (typeof user.username === 'string' && user.username) return '@' + user.username;
  if (typeof user.display_name === 'string' && user.display_name) return user.display_name;
  return String(user.id || '');
}

export function telegramDeepLink(code) {
  const payload = 'l_' + String(code || '');
  if (!/^[A-Za-z0-9_-]{3,64}$/.test(payload)) return null;
  return `https://t.me/${TELEGRAM_BOT}?start=${payload}`;
}

// After a Telegram connection is issued: open the bot on this device, or scan the QR on a phone.
// The QR and link carry only the one-time code, never a key (h3-studio#382).
export default function TelegramLink({ code, expiresAt, autoOpen = false, onHide }) {
  const { t } = useI18n();
  const link = telegramDeepLink(code);
  const svg = useMemo(() => {
    if (!link) return '';
    const qr = qrcode(0, 'M');
    qr.addData(link);
    qr.make();
    return qr.createSvgTag({ cellSize: 4, margin: 2, scalable: true });
  }, [link]);
  useEffect(() => {
    if (autoOpen && link) window.location.assign(link);
  }, [autoOpen, link]);
  if (!link) return <p className="qc-error">{t('telegram.badCode')}</p>;
  return (
    <article className="telegram-link">
      <h3>{t('telegram.ready')}</h3>
      <div className="telegram-link-body">
        <div
          className="telegram-qr"
          role="img"
          aria-label={t('telegram.qr')}
          dangerouslySetInnerHTML={{ __html: svg }}
        />
        <div className="telegram-steps">
          <p>{t('telegram.scan')}</p>
          <a className="button primary" href={link} target="_blank" rel="noreferrer">
            {t('telegram.open')}
          </a>
          <small className="muted">
            {t('telegram.expires', { date: new Date(expiresAt * 1000).toLocaleString() })}
          </small>
          <small className="muted">{t('telegram.private')}</small>
        </div>
      </div>
      {onHide && (
        <Button variant="ghost" size="sm" onClick={onHide}>
          {t('assistants.hide')}
        </Button>
      )}
    </article>
  );
}
