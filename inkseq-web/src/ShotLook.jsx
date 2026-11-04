// A shot's look and camera move from pictures, both Auto by default (h3-studio#1043). The choice
// reaches the prompt on the server. A look can also become the whole story's style.
import React from 'react';
import { useI18n } from './i18n/I18nProvider.jsx';
import { Button } from './ui/Button.jsx';
import { Disclosure } from './ui/Disclosure.jsx';
import { useStoryboardUI as useStore } from './storyboard-ui-context.jsx';
import { CAMERAS, LOOKS, cameraArrow, lookPicture, styleOf } from './shot-look.js';

export function ShotLook({ shot, onChange }) {
  const { t } = useI18n();
  const { state, dispatch } = useStore();
  const look = shot.look || '',
    camera = shot.camera || '';
  const storyStyle = state.project.style || '';
  const lookStyle = look ? styleOf(t, look) : '';
  const summary = [
    t('shotLook.summaryLook', { name: t('shotLook.look.' + (look || 'auto')) }),
    t('shotLook.summaryCamera', { name: t('shotLook.camera.' + (camera || 'auto')) }),
  ].join(' · ');
  return (
    <Disclosure
      className="shot-look"
      summary={
        <>
          {t('shotLook.title')} <small className="muted">{summary}</small>
        </>
      }
    >
      <div className="shot-look-grid" role="group" aria-label={t('shotLook.lookLabel')}>
        {['', ...LOOKS].map((value) => (
          <Button
            key={value || 'auto'}
            variant="option"
            className="shot-look-pick"
            aria-pressed={look === value}
            onClick={() => onChange({ look: value })}
          >
            {value ? (
              <img src={lookPicture(value)} alt="" width="160" height="90" loading="lazy" />
            ) : (
              <span className="shot-look-auto" aria-hidden="true">
                A
              </span>
            )}
            <span>{t('shotLook.look.' + (value || 'auto'))}</span>
          </Button>
        ))}
      </div>
      {look && (
        <Button
          size="sm"
          disabled={storyStyle === lookStyle}
          onClick={() => dispatch({ type: 'project', patch: { style: lookStyle } })}
        >
          {t(storyStyle === lookStyle ? 'shotLook.isStory' : 'shotLook.useForStory')}
        </Button>
      )}
      <div className="shot-look-cameras" role="group" aria-label={t('shotLook.cameraLabel')}>
        {['', ...CAMERAS].map((value) => (
          <Button
            key={value || 'auto'}
            size="sm"
            variant={camera === value ? 'primary' : 'secondary'}
            aria-pressed={camera === value}
            onClick={() => onChange({ camera: value })}
          >
            {value && <span aria-hidden="true">{cameraArrow(value)}</span>}
            {t('shotLook.camera.' + (value || 'auto'))}
          </Button>
        ))}
      </div>
    </Disclosure>
  );
}
