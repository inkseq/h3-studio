// Reviewed terms (h3-studio#374, CR-74): a signed-in person accepts the current Terms version
// once before paid generation or purchase. The server refuses with terms_acceptance_required
// until then; this asks at sign-in and again whenever that refusal comes back.
import React, { useEffect, useState } from 'react';
import { Dialog, DialogActions } from './ui/Dialog.jsx';
import { Button } from './ui/Button.jsx';
import { useI18n } from './i18n/I18nProvider.jsx';
import { useClient } from './AdminConsole.jsx';
import { TERMS_REQUIRED } from './terms-gate.js';
import { LEGAL } from './legal-content.js';

export function TermsGate({ account }) {
  const { t, locale } = useI18n();
  const api = useClient(account);
  const [terms, setTerms] = useState(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState('');
  useEffect(() => {
    if (!account) return undefined;
    let live = true;
    const read = () =>
      api
        .request('/v1/account')
        .then((body) => {
          const value = body?.terms;
          if (live && value?.status === 'reviewed' && value.acceptance_required) setTerms(value);
        })
        .catch(() => {});
    read();
    window.addEventListener(TERMS_REQUIRED, read);
    return () => {
      live = false;
      window.removeEventListener(TERMS_REQUIRED, read);
    };
  }, [api, account]);
  if (!terms) return null;
  const accept = async () => {
    setBusy(true);
    setError('');
    try {
      await api.request('/v1/account/terms', { method: 'POST', body: { version: terms.version } });
      setTerms(null);
    } catch (failure) {
      setError(failure.message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog title={t('terms.gateTitle')} onClose={() => setTerms(null)} busy={busy} portal>
      {/* Same one-line consent as sign-in; no checkbox or version shown. */}
      <p className="terms-gate-consent">
        {t('signIn.consentBefore')}
        {['terms', 'privacy', 'content'].map((doc, index) => (
          <React.Fragment key={doc}>
            {index === 1 ? t('signIn.comma') : index === 2 ? t('signIn.and') : null}
            <a href={'/' + doc} target="_blank" rel="noreferrer">
              {(LEGAL[doc][locale] || LEGAL[doc].en).title}
            </a>
          </React.Fragment>
        ))}
        {t('signIn.consentAfter')}
      </p>
      {error && <p role="alert">{error}</p>}
      <DialogActions>
        <Button variant="primary" disabled={busy} onClick={() => void accept()}>
          {t('terms.gateAccept')}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
