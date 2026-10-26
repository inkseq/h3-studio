import './i18n/locale.js';
import { useI18n } from './i18n/I18nProvider.jsx';
import React from 'react';
import { executionStatus } from './service-model.js';
export function ServiceStatus({ capabilities, projectId }) {
  const { t: at, locale } = useI18n();
  const status = executionStatus(capabilities, locale),
    render = capabilities?.chapter_render;
  return (
    <details className={'service-status ' + status.kind}>
      <summary>
        {status.title}
        {at('adjunct.serviceStatus.copy106')}
      </summary>
      <p>{status.detail}</p>
      <ul>
        <li>{at('adjunct.serviceStatus.copy107')}</li>
        <li>
          {at('adjunct.serviceStatus.copy108')}
          {render?.implemented && render.enabled
            ? at('adjunct.serviceStatus.copy109')
            : render?.implemented
              ? at('adjunct.serviceStatus.copy110')
              : at('adjunct.serviceStatus.copy111')}
        </li>
        <li>{at('adjunct.serviceStatus.copy112')}</li>
      </ul>
      <details className="api-integration">
        <summary>{at('adjunct.serviceStatus.copy113')}</summary>
        <p>{at('adjunct.serviceStatus.copy114')}</p>
        {projectId && (
          <p>
            {at('adjunct.serviceStatus.copy115')}
            <code>{projectId}</code>
          </p>
        )}
        <ol>
          <li>
            {at('adjunct.serviceStatus.copy116')}
            <code>GET /v1/capabilities</code>
            {at('adjunct.serviceStatus.copy117')}
          </li>
          <li>
            {at('adjunct.serviceStatus.copy118')}
            <code>POST /v1/assets</code>
            {at('adjunct.serviceStatus.copy119')}
          </li>
          <li>
            {at('adjunct.serviceStatus.copy120')}
            <code>POST /v1/generation-plans</code>
            {at('adjunct.serviceStatus.copy121')}
          </li>
          <li>
            {at('adjunct.serviceStatus.copy122')}
            <code>POST /v1/jobs</code>
            {at('adjunct.serviceStatus.copy123')}
            <code>Idempotency-Key</code>
            {at('adjunct.serviceStatus.copy124')}
          </li>
          <li>{at('adjunct.serviceStatus.copy125')}</li>
        </ol>
        <p>{at('adjunct.serviceStatus.copy126')}</p>
      </details>
    </details>
  );
}
