import React, { useState } from 'react';
import StudioHeader from './StudioHeader.jsx';
import AssistantConnections from './AssistantConnections.jsx';
import { useCredits } from './CreditsUI.jsx';
import { useI18n } from './i18n/I18nProvider.jsx';
import { Login } from './LoginDialog.jsx';
import { validTelegramNonce } from './TelegramLink.jsx';

// Bot-first linking (h3-studio#382): the bot's /link opens this page. The owner signs in,
// approves the Telegram connection, and is sent back to the bot with the one-time code.
export default function ConnectTelegramPage({ account, active = true }) {
  const { t } = useI18n();
  const credits = useCredits(account),
    [login, setLogin] = useState(false),
    [nonce] = useState(() => {
      const value = new URLSearchParams(window.location.search).get('r');
      return validTelegramNonce(value) ? value : null;
    });
  return (
    <div className="home">
      <StudioHeader account={account} onSignIn={() => setLogin(true)} />
      <main className="connect-telegram-page">
        <h1 className="page-title">{t('telegram.pageTitle')}</h1>
        <AssistantConnections
          embedded
          telegram
          telegramNonce={nonce}
          account={account}
          creditMode={!!credits}
          active={active}
          onLogin={() => setLogin(true)}
        />
        {login && <Login onClose={() => setLogin(false)} onDone={() => setLogin(false)} />}
      </main>
    </div>
  );
}
