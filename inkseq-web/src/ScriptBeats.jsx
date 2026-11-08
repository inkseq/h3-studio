// One scene as beats on the Script step: what the people want here, then action and line beats,
// each claimed by one shot (script-beats.js). Text boxes keep typing local and save after a
// pause, like the rest of the Script step.
import React, { useLayoutEffect, useRef } from 'react';
import { Plus, X } from 'lucide-react';
import { Button, IconButton } from './ui/Button.jsx';
import { Segmented } from './ui/Segmented.jsx';
import { Note } from './ui/Note.jsx';
import { useI18n } from './i18n/I18nProvider.jsx';
import { useStoryboardUI } from './storyboard-ui-context.jsx';
import {
  BEAT_TEXT_MAX,
  beatClaims,
  beatSeconds,
  beatsSeconds,
  newBeatId,
  parseScript,
} from './script-beats.js';
import { totalDuration } from './storyboard-ui-model.js';

export function SceneIntent({ group, Text }) {
  const { t } = useI18n();
  const { dispatch } = useStoryboardUI();
  if (!group.id) return null;
  return (
    <div className="script-intent">
      <span className="script-intent-label">{t('script.intent')}</span>
      <Text
        id={group.id}
        value={group.intent}
        label={t('script.intent')}
        placeholder={t('script.intentHint')}
        max={600}
        onSave={(id, intent) => dispatch({ type: 'sceneIntent', id, intent })}
      />
    </div>
  );
}

export function SceneBeats({ group, numbers, Text }) {
  const { t } = useI18n();
  const { state, dispatch } = useStoryboardUI();
  const beatsRef = useRef(group.beats);
  useLayoutEffect(() => {
    beatsRef.current = group.beats;
  });
  const characters = state.profiles.filter((p) => p.type === 'character');
  const save = (beats) => dispatch({ type: 'sceneBeats', id: group.id, beats });
  const patch = (id, change) =>
    save(beatsRef.current.map((b) => (b.id === id ? { ...b, ...change } : b)));
  const add = (kind) =>
    save([...beatsRef.current, { id: newBeatId(), kind, text: '', speaker: '', delivery: '' }]);

  if (!group.id) return null;
  if (!group.beats.length)
    return (
      <div className="script-beats-start">
        <Button
          size="sm"
          variant="ghost"
          className="keep-label"
          disabled={!group.script.trim()}
          onClick={() => save(parseScript(group.script, characters))}
        >
          {t('script.splitBeats')}
        </Button>
        <Button size="sm" variant="ghost" className="keep-label" onClick={() => add('action')}>
          <Plus size={13} aria-hidden="true" />
          {t('script.addAction')}
        </Button>
        <Button size="sm" variant="ghost" className="keep-label" onClick={() => add('line')}>
          <Plus size={13} aria-hidden="true" />
          {t('script.addLine')}
        </Button>
      </div>
    );

  const claims = beatClaims(group.shots, group.beats);
  const unclaimed = group.beats.filter((b) => !claims.get(b.id).length).length;
  const shared = group.beats.filter((b) => claims.get(b.id).length > 1).length;
  return (
    <div className="script-beats">
      <div className="script-beats-sum muted">
        {t('script.beatsSum', {
          count: group.beats.length,
          seconds: beatsSeconds(group.beats),
          shots: Math.round(totalDuration(group.shots)),
        })}
      </div>
      <ol className="script-beat-list">
        {group.beats.map((beat) => {
          const claimedBy = claims.get(beat.id);
          return (
            <li
              key={beat.id}
              className={
                'script-beat ' +
                beat.kind +
                (claimedBy.length ? '' : ' unclaimed') +
                (claimedBy.length > 1 ? ' shared' : '')
              }
            >
              <Segmented
                className="script-beat-kind"
                label={t('script.beatKind')}
                value={beat.kind}
                onChange={(kind) =>
                  patch(beat.id, kind === 'action' ? { kind, speaker: '', delivery: '' } : { kind })
                }
                options={[
                  { value: 'action', label: t('script.kind.action') },
                  { value: 'line', label: t('script.kind.line') },
                ]}
              />
              <div className="script-beat-body">
                {beat.kind === 'line' && (
                  <div className="script-beat-who">
                    <select
                      aria-label={t('script.speaker')}
                      value={beat.speaker}
                      onChange={(e) => patch(beat.id, { speaker: e.target.value })}
                    >
                      <option value="">{t('script.pickSpeaker')}</option>
                      {characters.map((c) => (
                        <option key={c.id} value={c.id}>
                          {c.name}
                          {c.bible?.speaker ? ' · ' + c.bible.speaker : ''}
                        </option>
                      ))}
                    </select>
                    <Text
                      id={beat.id + ':delivery'}
                      value={beat.delivery}
                      label={t('script.delivery')}
                      placeholder={t('script.deliveryHint')}
                      max={600}
                      onSave={(_, delivery) => patch(beat.id, { delivery })}
                    />
                  </div>
                )}
                <Text
                  id={beat.id}
                  value={beat.text}
                  label={t(beat.kind === 'line' ? 'script.lineText' : 'script.actionText')}
                  placeholder={t(beat.kind === 'line' ? 'script.lineHint' : 'script.actionHint')}
                  max={BEAT_TEXT_MAX}
                  onSave={(_, text) => patch(beat.id, { text })}
                />
              </div>
              <div className="script-beat-side">
                <small className="muted">{t('script.seconds', { n: beatSeconds(beat) })}</small>
                <select
                  className="script-beat-shot"
                  aria-label={t('script.claimedBy')}
                  value={claimedBy[0] || ''}
                  onChange={(e) =>
                    dispatch({
                      type: 'claimBeat',
                      scene: group.id,
                      beat: beat.id,
                      shot: e.target.value || null,
                    })
                  }
                >
                  <option value="">{t('script.noShot')}</option>
                  {group.shots.map((shot) => (
                    <option key={shot.id} value={shot.id}>
                      {t('script.shotNo', { n: String(numbers.get(shot.id)).padStart(2, '0') })}
                    </option>
                  ))}
                </select>
                <IconButton
                  size="sm"
                  label={t('script.removeBeat')}
                  onClick={() => save(beatsRef.current.filter((b) => b.id !== beat.id))}
                >
                  <X size={13} />
                </IconButton>
              </div>
            </li>
          );
        })}
      </ol>
      {(unclaimed > 0 || shared > 0) && (
        <Note tone="warn">
          {[
            unclaimed ? t('script.unclaimed', { count: unclaimed }) : '',
            shared ? t('script.shared', { count: shared }) : '',
          ]
            .filter(Boolean)
            .join(' ')}
        </Note>
      )}
      <div className="script-beats-add">
        <Button size="sm" variant="ghost" className="keep-label" onClick={() => add('action')}>
          <Plus size={13} aria-hidden="true" />
          {t('script.addAction')}
        </Button>
        <Button size="sm" variant="ghost" className="keep-label" onClick={() => add('line')}>
          <Plus size={13} aria-hidden="true" />
          {t('script.addLine')}
        </Button>
      </div>
    </div>
  );
}
