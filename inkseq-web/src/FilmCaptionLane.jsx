// The caption lane under the clips in the film editor, and the selected caption's editor.
// Captions start from the script lines each shot films (caption-edit.js), then are fixed by
// hand; burn-in still asks for its own confirmation (caption-model.js).
import React, { useState } from 'react';
import { Plus, Trash2, Wand2 } from 'lucide-react';
import { Button, IconButton } from './ui/Button.jsx';
import { useI18n } from './i18n/I18nProvider.jsx';
import { MAX_CUE_TEXT } from './caption-edit.js';

export function CaptionLane({ cues, x, width, selected, onSelect }) {
  const { t } = useI18n();
  return (
    <div
      className="film-caption-lane"
      style={{ width }}
      aria-label={t('editor.captionLane')}
      onPointerDown={(event) => event.target === event.currentTarget && onSelect(null)}
    >
      {!cues.length && <span className="film-audio-empty muted">{t('editor.noCaptions')}</span>}
      {cues.map((cue) => {
        const left = x(cue.start);
        return (
          <Button
            key={cue.id}
            className={'film-caption-cue' + (selected === cue.id ? ' on' : '')}
            style={{ left, width: Math.max(14, x(cue.end) - left) }}
            title={cue.text}
            onClick={() => onSelect(cue.id)}
          >
            {cue.text || '…'}
          </Button>
        );
      })}
    </div>
  );
}

export function CaptionControls({
  cues,
  selected,
  busy,
  hasLines,
  onAdd,
  onFill,
  onSet,
  onRemove,
}) {
  const { t } = useI18n();
  const [armed, setArmed] = useState(false);
  const cue = cues.find((item) => item.id === selected);
  const time = (key) => (
    <input
      type="number"
      step="0.1"
      min={0}
      className="field-sm film-caption-time"
      aria-label={t(key === 'start' ? 'editor.cueStart' : 'editor.cueEnd')}
      value={cue[key]}
      disabled={busy}
      onChange={(event) => onSet(cue.id, { [key]: Number(event.target.value) })}
    />
  );
  return (
    <div className="film-caption-bar">
      <Button size="sm" disabled={busy} onClick={onAdd}>
        <Plus size={13} aria-hidden="true" />
        {t('editor.addCaption')}
      </Button>
      {hasLines && (
        <Button
          size="sm"
          variant={armed ? 'primary' : 'ghost'}
          disabled={busy}
          title={t('editor.captionsFromLinesHint')}
          onClick={() => {
            // Replacing existing captions takes a second click on the same button.
            if (cues.length && !armed) return setArmed(true);
            setArmed(false);
            onFill();
          }}
          onBlur={() => setArmed(false)}
        >
          <Wand2 size={13} aria-hidden="true" />
          {t(armed ? 'editor.captionsReplace' : 'editor.captionsFromLines')}
        </Button>
      )}
      {cue && (
        <div className="film-caption-edit">
          {time('start')}
          <span className="muted">–</span>
          {time('end')}
          <input
            className="film-caption-text"
            value={cue.text}
            maxLength={MAX_CUE_TEXT}
            placeholder={t('editor.captionText')}
            aria-label={t('editor.captionText')}
            disabled={busy}
            onChange={(event) => onSet(cue.id, { text: event.target.value })}
          />
          <IconButton
            size="sm"
            label={t('editor.removeCaption')}
            disabled={busy}
            onClick={() => onRemove(cue.id)}
          >
            <Trash2 size={13} />
          </IconButton>
        </div>
      )}
    </div>
  );
}
