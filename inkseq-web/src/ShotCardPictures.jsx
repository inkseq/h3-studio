// Pick which of a card's pictures this reference shot sends (h3-studio#504). Shown only when the
// card has two or more pictures; a tap changes the pick at once and the shot saves after.
import React from 'react';
import { Button } from './ui/Button.jsx';
import { useI18n } from './i18n/I18nProvider.jsx';
import { pickedPictures, togglePicture } from './shot-card-pictures.js';

export function ShotCardPictures({ shot, cardId, name, images, thumb, onChange }) {
  const { t: tr } = useI18n();
  // Shown in both modes so switching mode never moves the panel; only reference mode sends them.
  if (images.length < 2) return null;
  const picked = pickedPictures(shot, cardId, images);
  return (
    <div className="card-picks" role="group" aria-label={tr('storyboard.cardPictures', { name })}>
      {images.map((id) => {
        const on = picked.includes(id);
        return (
          <Button
            key={id}
            className={'card-pick' + (on ? ' on' : '')}
            aria-pressed={on}
            title={tr(on ? 'storyboard.cardPictureOn' : 'storyboard.cardPictureOff')}
            onClick={() => onChange({ cardImages: togglePicture(shot, cardId, images, id) })}
          >
            {thumb(id)}
          </Button>
        );
      })}
    </div>
  );
}
