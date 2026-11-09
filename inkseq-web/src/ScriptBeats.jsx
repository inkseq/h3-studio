// One scene as beats on the Script step: what the people want here, then action and line beats,
// each claimed by one shot (script-beats.js). Text boxes keep typing local and save after a
// pause, like the rest of the Script step.
//
// Keyboard (after Trelby's "Enter makes the next element" and Beat's outline moves; Fountain
// keeps a line's delivery beside its speaker): Alt+↑/↓ moves the beat, Ctrl+Enter adds a beat
// of the same kind below. After adding, splitting, removing or moving, focus lands on the new
// or neighbouring beat, never on the page.
import React, { useLayoutEffect, useRef, useState } from 'react';
import { ArrowDown, ArrowUp, ListPlus, Plus, X } from 'lucide-react';
import { Button, IconButton } from './ui/Button.jsx';
import { Segmented } from './ui/Segmented.jsx';
import { Note } from './ui/Note.jsx';
import { useI18n } from './i18n/I18nProvider.jsx';
import { useStoryboardUI } from './storyboard-ui-context.jsx';
import {
  BEAT_TEXT_MAX,
  beatClaims,
  beatSeconds,
  beatsFromShots,
  beatsSeconds,
  insertBeat,
  moveBeat,
  parseScript,
  splitBlocked,
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

// The control a beat's focus returns to, by its data-part.
const partOf = (node) => node?.closest?.('[data-part]')?.dataset.part || 'text';

// One beat: its kind, who says it and how, its text, the shot that films it and its tools.
// Every control's name starts with the beat's number ("第 3 拍：…").
function BeatRow({
  beat,
  n,
  first,
  last,
  claimedBy,
  stash,
  characters,
  shots,
  numbers,
  Text,
  act,
}) {
  const { t } = useI18n();
  const named = (part) => t('script.beatPart', { n, part });
  return (
    <li
      data-beat={beat.id}
      className={
        'script-beat ' +
        beat.kind +
        (claimedBy.length ? '' : ' unclaimed') +
        (claimedBy.length > 1 ? ' shared' : '')
      }
      onKeyDown={act.keys(beat)}
    >
      <Segmented
        className="script-beat-kind"
        label={named(t('script.beatKind'))}
        value={beat.kind}
        onChange={(kind) => act.setKind(beat, kind)}
        options={[
          { value: 'action', label: t('script.kind.action') },
          { value: 'line', label: t('script.kind.line') },
        ]}
      />
      <div className="script-beat-body">
        {beat.kind === 'line' && (
          <div className="script-beat-who">
            <select
              data-part="speaker"
              aria-label={named(t('script.speaker'))}
              value={beat.speaker}
              onChange={(e) => act.patch(beat.id, { speaker: e.target.value })}
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
              part="delivery"
              value={beat.delivery}
              label={named(t('script.delivery'))}
              placeholder={t('script.deliveryHint')}
              max={600}
              onSave={(_, delivery) => act.patch(beat.id, { delivery })}
            />
          </div>
        )}
        {stash && (
          <small className="muted script-beat-kept" role="status">
            {t('script.keptLine')}
          </small>
        )}
        <Text
          id={beat.id}
          part="text"
          value={beat.text}
          label={named(t(beat.kind === 'line' ? 'script.lineText' : 'script.actionText'))}
          placeholder={t(beat.kind === 'line' ? 'script.lineHint' : 'script.actionHint')}
          max={BEAT_TEXT_MAX}
          onSave={(_, text) => act.patch(beat.id, { text })}
        />
      </div>
      <div className="script-beat-side">
        <small className="muted">{t('script.seconds', { n: beatSeconds(beat) })}</small>
        <select
          className="script-beat-shot"
          data-part="shot"
          aria-label={named(t('script.claimedBy'))}
          value={claimedBy[0] || ''}
          onChange={(e) => act.claim(beat.id, e.target.value || null)}
        >
          <option value="">{t('script.noShot')}</option>
          {shots.map((shot) => (
            <option key={shot.id} value={shot.id}>
              {t('script.shotNo', { n: String(numbers.get(shot.id)).padStart(2, '0') })}
            </option>
          ))}
        </select>
        <span className="script-beat-tools">
          <IconButton
            size="sm"
            data-part="up"
            label={named(t('script.moveUp'))}
            disabled={first}
            onClick={() => act.move(beat.id, -1, 'up')}
          >
            <ArrowUp size={13} />
          </IconButton>
          <IconButton
            size="sm"
            data-part="down"
            label={named(t('script.moveDown'))}
            disabled={last}
            onClick={() => act.move(beat.id, 1, 'down')}
          >
            <ArrowDown size={13} />
          </IconButton>
          <IconButton
            size="sm"
            data-part="insert"
            label={named(t(beat.kind === 'line' ? 'script.insertLine' : 'script.insertAction'))}
            onClick={() => act.add(beat.kind, beat.id)}
          >
            <Plus size={13} />
          </IconButton>
          <IconButton
            size="sm"
            data-part="remove"
            label={named(t('script.removeBeat'))}
            onClick={() => act.remove(beat.id)}
          >
            <X size={13} />
          </IconButton>
        </span>
      </div>
    </li>
  );
}

// A scene with no beats yet: build them from its shots, split its own text, or start one.
// When splitting cannot run, the line under the buttons says why.
function BeatsStart({ group, boxRef, working, onFromShots, onSplit, onAdd }) {
  const { t } = useI18n();
  const blocked = splitBlocked(group);
  const inShots = group.shots.some((shot) => (shot.prompt || '').trim());
  const why = 'script-split-why-' + group.id;
  return (
    <div className="script-beats-start" ref={boxRef}>
      <div className="script-beats-actions">
        {inShots && (
          <Button
            size="sm"
            variant={blocked ? 'secondary' : 'ghost'}
            className="keep-label"
            disabled={working}
            aria-describedby={why}
            onClick={onFromShots}
          >
            <ListPlus size={13} aria-hidden="true" />
            {t('script.beatsFromShots')}
          </Button>
        )}
        <Button
          size="sm"
          variant="ghost"
          className="keep-label"
          disabled={!!blocked}
          aria-describedby={why}
          onClick={onSplit}
        >
          {t('script.splitBeats')}
        </Button>
        <Button size="sm" variant="ghost" className="keep-label" onClick={() => onAdd('action')}>
          <Plus size={13} aria-hidden="true" />
          {t('script.addAction')}
        </Button>
        <Button size="sm" variant="ghost" className="keep-label" onClick={() => onAdd('line')}>
          <Plus size={13} aria-hidden="true" />
          {t('script.addLine')}
        </Button>
      </div>
      {/* Always one line, so nothing moves when the scene text is typed. */}
      <small className="muted script-beats-why" id={why}>
        {t(
          blocked === 'inShots'
            ? 'script.splitWhy.inShots'
            : blocked
              ? 'script.splitWhy.empty'
              : 'script.splitWhy.ready',
        )}
      </small>
    </div>
  );
}

export function SceneBeats({ group, numbers, Text }) {
  const { t } = useI18n();
  const { state, dispatch } = useStoryboardUI();
  const beatsRef = useRef(group.beats),
    box = useRef(null),
    // { id, part }: where focus goes once the beats on screen include that beat.
    land = useRef(null),
    // Speaker and delivery of a line turned into an action, kept until it turns back.
    kept = useRef(new Map());
  const [working, setWorking] = useState(false);
  useLayoutEffect(() => {
    beatsRef.current = group.beats;
    const want = land.current;
    if (!want || !box.current) return;
    if (want.id === '') {
      const first = box.current.querySelector('.script-beats-start button:not(:disabled)');
      if (first) {
        land.current = null;
        first.focus();
      }
      return;
    }
    const row = box.current.querySelector(`[data-beat="${want.id}"]`);
    if (!row) return;
    land.current = null;
    const usable = (part) => {
      const node = row.querySelector(`[data-part="${part}"]`);
      return node && !node.disabled ? node : null;
    };
    (usable(want.part) || usable(want.part === 'up' ? 'down' : 'up') || usable('text'))?.focus();
  });
  const characters = state.profiles.filter((p) => p.type === 'character');
  const save = (beats, focus) => {
    if (focus) land.current = focus;
    return dispatch({ type: 'sceneBeats', id: group.id, beats });
  };
  const patch = (id, change) =>
    save(beatsRef.current.map((b) => (b.id === id ? { ...b, ...change } : b)));
  const add = (kind, after = null) => {
    const made = insertBeat(beatsRef.current, after, kind);
    if (made) save(made.beats, { id: made.beat.id, part: 'text' });
  };
  const move = (id, dir, part) => {
    const beats = moveBeat(beatsRef.current, id, dir);
    if (beats) save(beats, { id, part });
  };
  const remove = (id) => {
    const beats = beatsRef.current,
      at = beats.findIndex((b) => b.id === id);
    const near = beats[at + 1] || beats[at - 1];
    kept.current.delete(id);
    save(
      beats.filter((b) => b.id !== id),
      near ? { id: near.id, part: 'text' } : { id: '' },
    );
  };
  const setKind = (beat, kind) => {
    if (kind === beat.kind) return;
    if (kind === 'action') {
      if (beat.speaker || beat.delivery)
        kept.current.set(beat.id, { speaker: beat.speaker, delivery: beat.delivery });
      return patch(beat.id, { kind, speaker: '', delivery: '' });
    }
    const back = kept.current.get(beat.id);
    kept.current.delete(beat.id);
    patch(beat.id, { kind, ...(back || {}) });
  };
  // The scene's script is in its shots (stories written in chat): one beat list from them, in
  // shot order, and every beat claimed by the shot it came from.
  const fromShots = async () => {
    const { beats, claims } = beatsFromShots(
      group.shots.map((shot) => ({ id: shot.id, prompt: shot.prompt })),
      characters,
    );
    if (!beats.length) return;
    setWorking(true);
    try {
      const done = await save(beats, { id: beats[0].id, part: 'text' });
      if (done?.ok === false) return;
      for (const [beat, shot] of claims) {
        const claimed = await dispatch({ type: 'claimBeat', scene: group.id, beat, shot });
        if (claimed?.ok === false) break;
      }
    } finally {
      setWorking(false);
    }
  };
  const keys = (beat) => (event) => {
    if (event.defaultPrevented || event.nativeEvent?.isComposing) return;
    const select = event.target.tagName === 'SELECT';
    if (event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey && !select) {
      if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') return;
      event.preventDefault();
      move(beat.id, event.key === 'ArrowUp' ? -1 : 1, partOf(event.target));
    } else if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key === 'Enter') {
      event.preventDefault();
      add(beat.kind, beat.id);
    }
  };

  const claim = (beat, shot) => dispatch({ type: 'claimBeat', scene: group.id, beat, shot });
  const act = { keys, setKind, patch, move, add, remove, claim };

  if (!group.id) return null;
  if (!group.beats.length) {
    return (
      <BeatsStart
        group={group}
        boxRef={box}
        working={working}
        onFromShots={fromShots}
        onSplit={() => {
          const beats = parseScript(group.script, characters);
          if (beats.length) save(beats, { id: beats[0].id, part: 'text' });
        }}
        onAdd={add}
      />
    );
  }

  const claims = beatClaims(group.shots, group.beats);
  const unclaimed = group.beats.filter((b) => !claims.get(b.id).length).length;
  const shared = group.beats.filter((b) => claims.get(b.id).length > 1).length;
  return (
    <div className="script-beats" ref={box}>
      <div className="script-beats-sum muted">
        {t('script.beatsSum', {
          count: group.beats.length,
          seconds: beatsSeconds(group.beats),
          shots: Math.round(totalDuration(group.shots)),
        })}
        <span className="script-beats-keys">{t('script.beatKeys')}</span>
      </div>
      <ol className="script-beat-list">
        {group.beats.map((beat, index) => (
          <BeatRow
            key={beat.id}
            beat={beat}
            n={index + 1}
            first={index === 0}
            last={index === group.beats.length - 1}
            claimedBy={claims.get(beat.id)}
            stash={beat.kind === 'action' && kept.current.has(beat.id)}
            characters={characters}
            shots={group.shots}
            numbers={numbers}
            Text={Text}
            act={act}
          />
        ))}
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
