import React from 'react';
import { Button } from './ui/Button.jsx';
import { navigateApp } from './app-navigation.js';
import { isChunkError, reloadOnce } from './chunk-reload.js';
import { store } from './store.js';
import { useI18n } from './i18n/I18nProvider.jsx';

// "Saved" only when the open story is on the server with nothing newer here (#1023).
function savedKnown() {
  const { workspace } = store.getState();
  return workspace.mode === 'cloud' && !workspace.dirty;
}

function Fallback({ onRetry }) {
  const { t } = useI18n();
  return (
    <div className="page-error" role="alert">
      <h2>{t('pageError.title')}</h2>
      <p>{t(savedKnown() ? 'pageError.body' : 'pageError.bodyUnknown')}</p>
      <div className="page-error-actions">
        <Button variant="primary" onClick={() => window.location.reload()}>
          {t('pageError.reload')}
        </Button>
        <Button
          onClick={() => {
            onRetry();
            navigateApp('/');
          }}
        >
          {t('pageError.works')}
        </Button>
      </div>
    </div>
  );
}

// A crash in one page shows a way out instead of a blank black screen (owner, 2026-10-24).
// React still reports the error (onCaughtError → Sentry on the deployed sites).
export default class PageErrorBoundary extends React.Component {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  // A code file removed by a newer release: fetch the new version once instead of stopping here.
  componentDidCatch(error) {
    if (isChunkError(error)) reloadOnce();
  }
  // Moving to another address tries again; the route cache itself is never remounted.
  componentDidUpdate(previous) {
    if (this.state.failed && previous.resetKey !== this.props.resetKey)
      this.setState({ failed: false });
  }
  render() {
    return this.state.failed ? (
      <Fallback onRetry={() => this.setState({ failed: false })} />
    ) : (
      this.props.children
    );
  }
}
