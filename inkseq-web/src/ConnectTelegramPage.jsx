import React, { useState } from 'react';
import StudioHeader from './StudioHeader.jsx';
import AssistantConnections from './AssistantConnections.jsx';
import { useCredits } from './CreditsUI.jsx';
import { useI18n } from './i18n/I18nProvider.jsx';
import { Login } from './LoginDialog.jsx';
import { useCloud } from './use-cloud.js';
import { hostedSignInEnabled, hostedSignInUrl } from './account-access.js';
import { validTelegramNonce } from './TelegramLink.jsx';

// Bot-first linking (h3-studio#382): the bot's /link opens this page. The owner signs in,
// approves the Telegram connection, and is sent back to the bot with the one-time code.
export default function ConnectTelegramPage({ account, active = true }) {
  const { t } = useI18n();
  const credits = useCredits(account),
    c = useCloud(),
    [login, setLogin] = useState(false),
    [nonce] = useState(() => {
      const value = new URLSearchParams(window.location.search).get('r');
      return validTelegramNonce(value) ? value : null;
    });
  // Straight to the sign-in page and back here, link code kept; the dialog only where the
  // hosted page is off.
  const signIn = () =>
    hostedSignInEnabled(c.auth)
      ? window.location.assign(
          hostedSignInUrl(window.location.pathname + window.location.search, 'sign-in'),
        )
      : setLogin(true);
  return (
    <div className="home">
      <StudioHeader account={account} onSignIn={signIn} />
      <main className="connect-telegram-page">
        <h1 className="page-title">{t('telegram.pageTitle')}</h1>
        <AssistantConnections
          embedded
          telegram
          telegramNonce={nonce}
          account={account}
          creditMode={!!credits}
          active={active}
          onLogin={signIn}
        />
        {login && <Login onClose={() => setLogin(false)} onDone={() => setLogin(false)} />}
      </main>
    </div>
  );
}
