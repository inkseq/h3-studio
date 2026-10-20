import React from 'react';
import { useI18n } from './i18n/I18nProvider.jsx';
import AssistantConnections from './AssistantConnections.jsx';
export default function AgentConnect({
  account,
  workspace = {},
  projectId,
  onOpenCloud,
  onSaveCloud,
  busy,
}) {
  const { t } = useI18n(),
    ready =
      workspace.mode === 'cloud' &&
      workspace.account === account &&
      Number.isInteger(workspace.serverVersion) &&
      !workspace.dirty;
  return (
    <>
      <AssistantConnections
        embedded
        account={account}
        projectId={ready ? projectId : undefined}
        onLogin={onOpenCloud}
      />
      {/* Signed out, the assistants panel already offers sign-in; this note is for signed-in local work. */}
      {account && !ready && (
        <p>
          {t('assistants.scopeUnavailable')}{' '}
          <button disabled={busy} onClick={workspace.mode === 'cloud' ? onSaveCloud : onOpenCloud}>
            {t(workspace.mode === 'cloud' ? 'account.copy64' : 'assistants.login')}
          </button>
        </p>
      )}
    </>
  );
}
