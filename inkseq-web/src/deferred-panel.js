import { translate } from './i18n/locale.js';
import React from 'react';
import { store } from './store.js';

// Failed module imports may remain cached for this document. Reload only after
// the current local/owner-scoped draft has actually reached browser storage.
export function reloadAfterCheckpoint(target = store, reload = () => globalThis.location.reload()) {
  if (!target.checkpoint()) return false;
  reload();
  return true;
}

export function deferredPanel(load, label) {
  const Initial = React.lazy(load);
  class DeferredPanel extends React.Component {
    state = { failed: false, saveFailed: false, backupUrl: null };
    static getDerivedStateFromError() {
      return { failed: true };
    }
    reload = () => {
      if (!reloadAfterCheckpoint()) this.setState({ saveFailed: true });
    };
    backup = () => {
      if (this.state.backupUrl) URL.revokeObjectURL(this.state.backupUrl);
      const blob = new Blob([JSON.stringify(store.exportProject(), null, 2)], {
        type: 'application/json',
      });
      this.setState({ backupUrl: URL.createObjectURL(blob) });
    };
    componentWillUnmount() {
      if (this.state.backupUrl) URL.revokeObjectURL(this.state.backupUrl);
    }
    render() {
      const t = (key, values) => translate(this.props.locale || 'en', key, values),
        panelLabel =
          {
            角色图集: t('workspace.deferredCharacter'),
            交付与粗剪: t('legacy.panelDelivery'),
            角色造型: t('legacy.panelCharacter'),
            剧本拆分: t('legacy.panelScript'),
            声音编辑器: t('legacy.panelSound'),
            字幕编辑器: t('legacy.panelSubtitle'),
            制作队列: t('legacy.panelProduction'),
          }[label] || label;
      if (this.state.failed)
        return React.createElement(
          'section',
          { className: 'panel-load-error', role: 'alert' },
          React.createElement('b', null, t('workspace.deferred0', { label: panelLabel })),
          React.createElement('p', null, t('workspace.deferred2')),
          React.createElement(
            'button',
            { type: 'button', onClick: this.reload },
            t('workspace.deferred3'),
          ),
          this.state.saveFailed && React.createElement('p', null, t('workspace.deferred4')),
          React.createElement(
            'button',
            { type: 'button', onClick: this.backup },
            t('workspace.deferred5'),
          ),
          this.state.backupUrl &&
            React.createElement(
              'a',
              { href: this.state.backupUrl, download: 'Inkseq-项目结构备份.json' },
              t('workspace.deferred6'),
            ),
        );
      const fallback = React.createElement(
        'section',
        { className: 'panel-loading', role: 'status', 'aria-live': 'polite', 'aria-busy': true },
        React.createElement('b', null, t('workspace.deferred1', { label: panelLabel })),
        React.createElement('p', null, t('workspace.deferred7')),
      );
      return React.createElement(
        React.Suspense,
        { fallback },
        React.createElement(Initial, this.props),
      );
    }
  }
  DeferredPanel.displayName = 'Deferred' + label;
  return DeferredPanel;
}
