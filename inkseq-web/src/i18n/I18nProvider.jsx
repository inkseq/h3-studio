import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  useSyncExternalStore,
} from 'react';
import { Button } from '../ui/Button.jsx';
import { Globe } from 'lucide-react';
import {
  isLocale,
  LOCALE_STORAGE_KEY,
  readLocale,
  readWordsVersion,
  rememberLocale,
  subscribeWords,
  translate,
} from './locale.js';

const I18nContext = createContext(null);
const browserStorage = () => {
  try {
    return globalThis.localStorage;
  } catch {
    return null;
  }
};

export function I18nProvider({ account = '', children }) {
  const [locale, updateLocale] = useState(() => readLocale(browserStorage(), account));
  useEffect(() => {
    updateLocale(readLocale(browserStorage(), account));
  }, [account]);
  useEffect(() => {
    document.documentElement.lang = locale === 'zh' ? 'zh-CN' : 'en';
  }, [locale]);
  useEffect(() => {
    const onStorage = (event) => {
      if (event.key === null || event.key.startsWith(LOCALE_STORAGE_KEY))
        updateLocale(readLocale(browserStorage(), account));
    };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, [account]);
  const setLocale = useCallback(
    (value) => {
      if (!isLocale(value)) throw new TypeError('unsupported_locale');
      rememberLocale(browserStorage(), value, account);
      updateLocale(value);
    },
    [account],
  );
  // Late words arrived: a new value makes the pages that drew without them draw again.
  const wordsVersion = useSyncExternalStore(subscribeWords, readWordsVersion, readWordsVersion);
  const value = useMemo(
    () => ({ locale, setLocale, wordsVersion, t: (key, vars) => translate(locale, key, vars) }),
    [locale, setLocale, wordsVersion],
  );
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n() {
  const value = useContext(I18nContext);
  if (!value) throw new Error('i18n_provider_required');
  return value;
}

export function LanguageSwitcher() {
  const { locale, setLocale, t } = useI18n();
  const key = locale === 'en' ? 'locale.switchToChinese' : 'locale.switchToEnglish';
  return (
    // Globe + the CURRENT language (same control as inkseq.tv); one click switches.
    <Button
      className="language-switch keep-label"
      title={t(key)}
      aria-label={t(key)}
      onClick={() => setLocale(locale === 'en' ? 'zh' : 'en')}
    >
      <Globe size={14} aria-hidden="true" />
      <span>{locale === 'en' ? 'EN' : '中'}</span>
    </Button>
  );
}
