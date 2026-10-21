import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

// TelegramLink.jsx exports a pure helper; read it without a JSX loader.
const source = readFileSync(new URL('./TelegramLink.jsx', import.meta.url), 'utf8');
const body = source.slice(
  source.indexOf('export const TELEGRAM_BOT'),
  source.indexOf('// After a Telegram'),
);
const { telegramDeepLink } = await import(
  'data:text/javascript,' +
    encodeURIComponent(body.replace(/export /g, '') + '\nexport { telegramDeepLink };')
);

test('one-time connect codes become a Telegram start link', () => {
  const code = 'sxc_' + 'A'.repeat(43);
  assert.equal(telegramDeepLink(code), 'https://t.me/InkseqBot?start=l_' + code);
});

test('refuses payloads Telegram would reject', () => {
  assert.equal(telegramDeepLink(''), null);
  assert.equal(telegramDeepLink('has space'), null);
  assert.equal(telegramDeepLink('x'.repeat(63)), null);
});
