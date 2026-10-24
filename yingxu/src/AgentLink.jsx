import { useI18n } from './i18n/I18nProvider.jsx';
import { FloatingMessage } from './floating-notice.js';
import React, { useEffect, useState } from 'react';
import { readAgentLink } from './agent-navigation.js';
import { cloudController as cloud } from './cloud-controller.js';
import { useCloud } from './CloudStudio.jsx';

export default function AgentLink({ state, onOpenCloud, onLocate, onActivity }) {
  const { t: at } = useI18n();
  const c = useCloud(),
    [search, setSearch] = useState(() => window.location.search),
    [hidden, setHidden] = useState(false),
    [error, setError] = useState('');
  useEffect(() => {
    const update = () => {
      setSearch(window.location.search);
      setHidden(false);
      setError('');
    };
    window.addEventListener('popstate', update);
    return () => window.removeEventListener('popstate', update);
  }, []);
  const target = readAgentLink(search);
  if (!target || hidden) return null;
  const here =
    state.workspace.mode === 'cloud' &&
    state.workspace.account === c.account &&
    state.project.id === target.projectId;
  async function follow() {
    setError('');
    if (!c.account) {
      onOpenCloud();
      return;
    }
    if (!here) {
      try {
        const done = await cloud.open(target.projectId);
        if (!done) return;
      } catch {
        setError(at('adjunct.agentLink.copy45'));
      }
      return;
    }
    if (target.activity) onActivity();
    else if (target.entityId) {
      if (!onLocate(target.entityId)) {
        setError(at('adjunct.agentLink.copy46'));
        return;
      }
    } else onActivity();
    setHidden(true);
  }
  return (
    <aside className="agent-link-banner" aria-label={at('adjunct.agentLink.copy47')}>
      <div>
        <b>{here ? at('adjunct.agentLink.copy48') : at('adjunct.agentLink.copy49')}</b>
        <p>
          {at('adjunct.agentLink.copy50')}
          {!here ? at('adjunct.agentLink.copy51') : ''}
        </p>
      </div>
      <div>
        <button className="primary" disabled={c.busy} onClick={follow}>
          {!c.account
            ? at('adjunct.agentLink.copy52')
            : here
              ? target.activity
                ? at('adjunct.agentLink.copy53')
                : target.entityId
                  ? at('adjunct.agentLink.copy54')
                  : at('adjunct.agentLink.copy55')
              : at('adjunct.agentLink.copy56')}
        </button>
        <button onClick={() => setHidden(true)}>{at('adjunct.agentLink.copy57')}</button>
      </div>
      {error && <p role="alert">{error}</p>}
    </aside>
  );
}
