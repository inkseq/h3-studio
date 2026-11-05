import { catalogs, catalogsPending, loadAllCatalogs, loadCatalogs } from './catalogs.js';

export const DEFAULT_LOCALE = 'en';
export const LOCALE_STORAGE_KEY = 'inkseq.ui.locale.v1';
export const isLocale = (value) => Object.hasOwn(catalogs, value);
export const localeStorageKey = (account) =>
  account ? `${LOCALE_STORAGE_KEY}:account:${encodeURIComponent(account)}` : LOCALE_STORAGE_KEY;

// First visit: English, unless the browser's first language is Chinese. A choice made with
// the header switch always wins.
export const firstVisitLocale = (languages = globalThis.navigator?.languages) =>
  /^zh(-|$)/i.test(String(languages?.[0] || '')) ? 'zh' : DEFAULT_LOCALE;

export function readLocale(storage, account = '') {
  try {
    const value =
      storage?.getItem(localeStorageKey(account)) ?? storage?.getItem(LOCALE_STORAGE_KEY);
    return isLocale(value) ? value : firstVisitLocale();
  } catch {
    return firstVisitLocale();
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

let wordsVersion = 0;
let wordsMissed = false;
const wordsListeners = new Set();
export const readWordsVersion = () => wordsVersion;
export function subscribeWords(listener) {
  wordsListeners.add(listener);
  return () => wordsListeners.delete(listener);
}
function redrawIfMissed() {
  if (!wordsMissed) return;
  wordsMissed = false;
  wordsVersion += 1;
  for (const listener of wordsListeners) listener();
}
// The first script does not carry this word: fetch the rest and draw again when it arrives.
function fetchMissedWords() {
  wordsMissed = true;
  void loadAllCatalogs()
    .then(redrawIfMissed)
    .catch(() => {});
}
/** Words a page needs before it draws: 'app' for signed-in pages, 'ops' for operator pages. */
export const loadWords = (tier = 'app') => loadCatalogs(tier).then(redrawIfMissed);

export function translate(locale, key, values = {}) {
  const template = catalogs[isLocale(locale) ? locale : DEFAULT_LOCALE][key] ?? catalogs.en[key];
  if (template === undefined) {
    if (!catalogsPending()) throw new Error(`missing_translation_key:${key}`);
    fetchMissedWords();
    return '';
  }
  return template.replace(/\{([A-Za-z]\w*)\}/g, (match, name) =>
    Object.hasOwn(values, name) ? String(values[name]) : match,
  );
}
