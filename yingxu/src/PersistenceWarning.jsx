import { useI18n } from './i18n/I18nProvider.jsx';
import React, { useEffect, useState, useSyncExternalStore } from 'react';
import { store } from './store.js';
import './persistence-warning.css';

export default function PersistenceWarning() {
  const { t: lt } = useI18n();
  useSyncExternalStore(store.subscribe, store.getState, store.getState);
  const risk = store.getPersistenceRisk(),
    [url, setUrl] = useState('');
  useEffect(
    () => () => {
      if (url) URL.revokeObjectURL(url);
    },
    [url],
  );
  if (!risk.current && !risk.otherCloudDrafts) return null;
  const prepare = () =>
    setUrl(
      URL.createObjectURL(
        new Blob([JSON.stringify(store.exportProject(), null, 2)], { type: 'application/json' }),
      ),
    );
  return (
    <section className="persistence-warning" role="alert" aria-label={lt('legacy.copy18')}>
      <b>{lt('legacy.copy19')}</b>
      <p>
        {risk.current ? lt('legacy.draftCurrent') : lt('legacy.draftOther')} {lt('legacy.copy20')}
      </p>
      <div>
        <button
          onClick={() => {
            if (store.checkpoint()) store.notify(lt('legacy.draftSaved'));
          }}
        >
          {lt('legacy.copy21')}
        </button>
        {risk.current && <button onClick={prepare}>{lt('legacy.copy22')}</button>}
        {url && (
          <a href={url} download="映序-未落盘项目结构.json">
            {lt('legacy.copy23')}
          </a>
        )}
      </div>
      {url && <p>{lt('legacy.copy24')}</p>}
    </section>
  );
}
