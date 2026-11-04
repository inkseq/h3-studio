// What to do after a finished take (h3-studio#1045), using the actions the shot already has. Anything
// that spends credits still goes through its own confirm step.
import React, { useState } from 'react';
import { useI18n } from './i18n/I18nProvider.jsx';
import { Button } from './ui/Button.jsx';
import { EnhanceTakeButton } from './EnhanceTake.jsx';
import { useStoryboardUI as useStore } from './storyboard-ui-context.jsx';

export function nextShotAfter(shots, id) {
  const at = shots.findIndex((shot) => shot.id === id);
  return at < 0 ? null : shots[at + 1] || null;
}

export function TakeNextStep({ shot, take, mediaRef, busy, onAnother, onMessage }) {
  const { t } = useI18n();
  const { state, dispatch } = useStore();
  const [carrying, setCarrying] = useState(false);
  const chosen = shot.chosen === take.id;
  // The frame on screen starts the next shot; a last shot gets a new one after it.
  const carry = async () => {
    const node = mediaRef.current?.querySelector('video');
    node?.pause();
    setCarrying(true);
    onMessage(t('takeNext.carrying'));
    try {
      const frame = await dispatch({
        type: 'saveFrame',
        shot: shot.id,
        version: take.id,
        artifactId: take.artifactId,
        t: Number((node?.currentTime || 0).toFixed(3)),
      });
      if (!frame || frame.ok === false) throw Error(frame?.error || 'frame');
      const existing = nextShotAfter(state.shots, shot.id);
      const added = existing
        ? null
        : await dispatch({ type: 'add', after: shot.id, title: t('storyboard.newShot') });
      const target = existing?.id || added?.id;
      if (!target) throw Error('shot');
      const patch = { start: frame.asset, continuePrev: false };
      const done = await dispatch({ type: 'update', id: target, patch });
      if (done?.ok === false) throw Error('update');
      onMessage(t('takeNext.carried'));
    } catch {
      onMessage(t('takeNext.carryFailed'));
    } finally {
      setCarrying(false);
    }
  };
  return (
    <div className="take-next" role="group" aria-label={t('takeNext.title')}>
      <small className="muted">{t('takeNext.title')}</small>
      {!chosen && (
        <Button
          size="sm"
          variant="primary"
          onClick={() => dispatch({ type: 'choose', shot: shot.id, version: take.id })}
        >
          {t('takeNext.use')}
        </Button>
      )}
      <Button size="sm" disabled={carrying} onClick={() => void carry()}>
        {t('takeNext.carry')}
      </Button>
      <EnhanceTakeButton take={take} onMessage={onMessage} />
      <Button size="sm" disabled={busy} onClick={onAnother}>
        {t('storyboard.anotherVersion')}
      </Button>
    </div>
  );
}
