import React from 'react';
import {useI18n} from './i18n/I18nProvider.jsx';
import {ChatDialog} from './QuickChatControls.jsx';
import AssistantConnections from './AssistantConnections.jsx';
export default function QuickChatAgent({account,sessionId,onClose,onLogin}){const {t}=useI18n();return <ChatDialog title={t('assistants.title')} onClose={onClose} wide><AssistantConnections embedded account={account} sessionId={sessionId} onLogin={onLogin}/></ChatDialog>;}
