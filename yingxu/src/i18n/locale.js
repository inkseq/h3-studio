import { catalogs } from './catalogs.js';

export const DEFAULT_LOCALE = 'en';
export const LOCALE_STORAGE_KEY = 'inkseq.ui.locale.v1';
export const isLocale = (value) => Object.hasOwn(catalogs, value);
export const localeStorageKey = (account) =>
  account ? `${LOCALE_STORAGE_KEY}:account:${encodeURIComponent(account)}` : LOCALE_STORAGE_KEY;

export function readLocale(storage, account = '') {
  try {
    const value =
      storage?.getItem(localeStorageKey(account)) ?? storage?.getItem(LOCALE_STORAGE_KEY);
    return isLocale(value) ? value : DEFAULT_LOCALE;
  } catch {
    return DEFAULT_LOCALE;
  }
}

export function rememberLocale(storage, locale, account = '') {
  if (!isLocale(locale)) throw new TypeError('unsupported_locale');
  try {
    storage?.setItem(LOCALE_STORAGE_KEY, locale);
    if (account) storage?.setItem(localeStorageKey(account), locale);
  } catch {
    // A blocked browser store must not prevent changing the current UI language.
  }
}

export function readBrowserLocale(account = '') {
  try {
    return readLocale(globalThis.localStorage, account);
  } catch {
    return DEFAULT_LOCALE;
  }
}

export function translateForAccount(account, key, values = {}) {
  return translate(readBrowserLocale(account), key, values);
}

export function translate(locale, key, values = {}) {
  const template = catalogs[isLocale(locale) ? locale : DEFAULT_LOCALE][key] ?? catalogs.en[key];
  if (template === undefined) throw new Error(`missing_translation_key:${key}`);
  return template.replace(/\{([A-Za-z]\w*)\}/g, (match, name) =>
    Object.hasOwn(values, name) ? String(values[name]) : match,
  );
}
