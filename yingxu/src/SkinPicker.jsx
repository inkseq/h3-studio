// Settings → Account → Appearance: pick an interface skin (per browser).
import React, { useState } from 'react';
import { Palette } from 'lucide-react';
import { useI18n } from './i18n/I18nProvider.jsx';
import { SKINS, applySkin, readSkin } from './skins.js';

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
            <span className="skin-swatch" aria-hidden="true">
              {skin.swatch.map((color) => (
                <i key={color} style={{ background: color }} />
              ))}
            </span>
            <b>{t('skin.' + skin.id)}</b>
            <small className="muted">{t('skin.' + skin.id + 'Help')}</small>
          </button>
        ))}
      </div>
    </div>
  );
}
