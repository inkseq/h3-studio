// Story settings → Default picture (h3-studio#891). Read by the settings form on save; "Apply to
// all shots" gives every existing shot the values shown here, at once, as one undo step.
import React, { useState } from 'react';
import { Button } from './ui/Button.jsx';
import { Field } from './ui/Field.jsx';
import { useI18n } from './i18n/I18nProvider.jsx';
import { store } from './store.js';
import {
  ASPECTS,
  MAX_COPIES,
  QUALITIES,
  applyPictureDefaults,
  pictureDefaults,
  setPictureDefaults,
} from './picture-defaults.js';

const ASPECT_LABEL = {
  '16:9': 'storyboard.aspectWide',
  '9:16': 'storyboard.aspectTall',
  '1:1': 'storyboard.aspectSquare',
};

/** The values in the form, as saved defaults. */
export function readPictureDefaults(form) {
  const f = new FormData(form);
  return {
    aspect: String(f.get('pictureAspect')),
    quality: String(f.get('pictureQuality')),
    keepAudio: f.get('pictureKeepAudio') === 'on',
    copies: Number(f.get('pictureCopies')),
  };
}

export function StoryPictureDefaults({ project }) {
  const { t } = useI18n(),
    defaults = pictureDefaults(project),
    shots = project.entities.filter((e) => e.type === 'shot'),
    [applied, setApplied] = useState('');
  const applyAll = (event) => {
    const value = readPictureDefaults(event.currentTarget.form);
    const ok = store.editProject((p) => {
      const saved = setPictureDefaults(p, value);
      for (const shot of p.entities.filter((e) => e.type === 'shot'))
        applyPictureDefaults(shot, { ...saved, set: true });
    });
    setApplied(ok === false ? store.getState().notice : t('pictureDefaults.applied'));
  };
  return (
    <fieldset className="story-picture-defaults">
      <legend>{t('pictureDefaults.title')}</legend>
      <small className="muted">{t('pictureDefaults.help')}</small>
      <Field label={t('storyboard.aspect')}>
        <select name="pictureAspect" defaultValue={defaults.aspect}>
          {ASPECTS.map((value) => (
            <option key={value} value={value}>
              {t(ASPECT_LABEL[value])}
            </option>
          ))}
        </select>
      </Field>
      <Field label={t('pictureDefaults.quality')}>
        <select name="pictureQuality" defaultValue={defaults.quality}>
          {QUALITIES.map((value) => (
            <option key={value} value={value}>
              {t('storyboard.labels.QUALITY_LABEL.' + value)}
            </option>
          ))}
        </select>
      </Field>
      <Field label={t('pictureDefaults.copies')}>
        <select name="pictureCopies" defaultValue={String(defaults.copies)}>
          {Array.from({ length: MAX_COPIES }, (_, i) => i + 1).map((value) => (
            <option key={value} value={value}>
              {value}
            </option>
          ))}
        </select>
      </Field>
      <label className="story-picture-check">
        <input type="checkbox" name="pictureKeepAudio" defaultChecked={defaults.keepAudio} />
        {t('pictureDefaults.keepAudio')}
      </label>
      <div className="story-picture-apply">
        <Button type="button" disabled={!shots.length} onClick={applyAll}>
          {t('pictureDefaults.applyAll', { count: shots.length })}
        </Button>
        <small className="muted" role="status">
          {applied || t('pictureDefaults.applyHelp')}
        </small>
      </div>
    </fieldset>
  );
}
