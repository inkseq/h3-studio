// Settings → Account → Appearance: pick an interface skin (per browser).
import React, { useState } from 'react';
import { Palette } from 'lucide-react';
import { useI18n } from './i18n/I18nProvider.jsx';
import { SKINS, applySkin, readSkin } from './skins.js';

// A tiny page mock in the skin's own colours: header with the active tab, then story cards.
function SkinPreview({ skin }) {
  const [bg, accent, third = accent] = skin.swatch;
  return (
    <span
      className={'skin-preview' + (skin.light ? ' light' : '')}
      style={{ '--p-bg': bg, '--p-accent': accent, '--p-third': third }}
      aria-hidden="true"
    >
      <span className="skin-preview-bar">
        <i />
        <b />
        <i />
        <i />
      </span>
      <span className="skin-preview-cards">
        <i />
        <i />
        <i />
      </span>
    </span>
  );
}

export default function SkinPicker() {
  const { t } = useI18n();
  const [current, setCurrent] = useState(readSkin);
  return (
    <div className="card-box">
      <h4>
        <Palette size={15} aria-hidden="true" /> {t('skin.title')}
      </h4>
      <div className="skin-picker" role="radiogroup" aria-label={t('skin.title')}>
        {SKINS.map((skin) => (
          <button
            key={skin.id}
            type="button"
            role="radio"
            aria-checked={current === skin.id}
            data-ui-surface="skin"
            className={'skin-option keep-label' + (current === skin.id ? ' on' : '')}
            onClick={() => setCurrent(applySkin(skin.id))}
          >
            <SkinPreview skin={skin} />
            <b>
              {t('skin.' + skin.id)}
              {skin.light && <small className="skin-tag">{t('skin.light')}</small>}
            </b>
            <small className="muted">{t('skin.' + skin.id + 'Help')}</small>
          </button>
        ))}
      </div>
    </div>
  );
}
