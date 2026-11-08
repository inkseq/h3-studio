// The Script step (h3-studio#1062): the whole story's text in one place, to read from top to bottom
// and change in place. It only edits text, so nothing here can start a generation or spend credits.
import React, { memo, useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { ArrowUpRight, Plus } from 'lucide-react';
import { Button, IconButton } from './ui/Button.jsx';
import { useI18n } from './i18n/I18nProvider.jsx';
import { useStoryboardUI } from './storyboard-ui-context.jsx';
import { fmt, totalDuration } from './storyboard-ui-model.js';
import { DEFAULT_CHAPTER_NAMES, sceneHeading } from './story-headings.js';
import { store } from './store.js';
import { SceneBeats, SceneIntent } from './ScriptBeats.jsx';
import { Disclosure } from './ui/Disclosure.jsx';
import './story-script.css';

const SAVE_AFTER_MS = 500;
// What the saved story accepts (store.js).
const SHOT_TEXT_MAX = 24000;
const SCENE_TEXT_MAX = 120000;
// Only for browsers older than `field-sizing` (Safari before 26.2): they use these rows and scroll
// the rest. Newer browsers size the box to its text and ignore this.
const textRows = (text) =>
  text.split('\n').reduce((rows, line) => rows + Math.max(1, Math.ceil(line.length / 40)), 0);

// One piece of text. Typing stays in this box and reaches the story after a short pause, or on
// leaving the box: saving redraws the whole story page, far too much to do on every key. The text
// on screen is always what was typed, and a pause is one step of the top bar's undo.
const ScriptText = memo(function ScriptText({ id, value, label, placeholder, max, onSave }) {
  const [draft, setDraft] = useState(null),
    unsaved = useRef(null),
    timer = useRef(0),
    composing = useRef(false),
    saved = useRef(value),
    save = useRef(null);
  useLayoutEffect(() => {
    saved.current = value;
    save.current = () => {
      clearTimeout(timer.current);
      const text = unsaved.current;
      if (text === null) return;
      unsaved.current = null;
      const settle = () => setDraft((old) => (old === text ? null : old));
      // Unchanged text must not count as an edit: an edit marks a shot as changed.
      if (text === saved.current) return settle();
      onSave(id, text).then((result) => {
        if (result?.ok !== false) return settle();
        // Refused (the message shows above): keep the words on screen and try again on leaving.
        unsaved.current ??= text;
      });
    };
  });
  const later = () => {
    clearTimeout(timer.current);
    timer.current = setTimeout(() => save.current(), SAVE_AFTER_MS);
  };
  useEffect(() => {
    const now = () => save.current();
    window.addEventListener('pagehide', now);
    return () => {
      window.removeEventListener('pagehide', now);
      now();
    };
  }, []);
  return (
    <textarea
      className="script-text"
      rows={textRows(draft ?? value)}
      maxLength={max}
      value={draft ?? value}
      aria-label={label}
      placeholder={placeholder}
      onChange={(event) => {
        unsaved.current = event.target.value;
        setDraft(event.target.value);
        // Half-typed pinyin is not text yet: it waits until the word is chosen.
        if (!composing.current) later();
      }}
      onCompositionStart={() => {
        composing.current = true;
        clearTimeout(timer.current);
      }}
      onCompositionEnd={() => {
        composing.current = false;
        later();
      }}
      onBlur={() => save.current()}
    />
  );
});

// Every value here is plain text or a stable callback, so a saved edit redraws only its own row.
const ScriptShot = memo(function ScriptShot({ id, number, title, prompt, onSave, onOpen }) {
  const { t } = useI18n();
  const no = String(number).padStart(2, '0');
  return (
    <article className={'script-shot' + (title ? ' titled' : '')} data-script-shot={id}>
      <span className="script-shot-no">{no}</span>
      <div className="script-shot-body">
        {title && <h4 className="script-shot-title">{title}</h4>}
        <ScriptText
          id={id}
          value={prompt}
          label={t('shotCard.prompt')}
          placeholder={t('script.shotHint')}
          max={SHOT_TEXT_MAX}
          onSave={onSave}
        />
      </div>
      <IconButton
        size="sm"
        className="script-shot-open"
        label={t('script.openShot', { number: no })}
        onClick={() => onOpen(id)}
      >
        <ArrowUpRight size={14} />
      </IconButton>
    </article>
  );
});

function ScriptScene({ group, numbers, locked, onSaveShot, onSaveScene, onAddShot, onOpenShot }) {
  const { t } = useI18n();
  return (
    <section className="script-scene">
      <header className="script-scene-head">
        <h3>{sceneHeading(group.title, t)}</h3>
        <small className="muted">
          {t(group.shots.length === 1 ? 'storyboard.sceneOneShot' : 'storyboard.sceneShots', {
            count: group.shots.length,
          })}{' '}
          · {fmt(totalDuration(group.shots))}
        </small>
      </header>
      <SceneIntent group={group} Text={ScriptText} />
      {/* Once a scene is in beats, its old free text stays readable but out of the way. */}
      {group.id && !group.beats?.length && (
        <ScriptText
          id={group.id}
          value={group.script}
          label={t('script.sceneLabel')}
          placeholder={t('script.sceneHint')}
          max={SCENE_TEXT_MAX}
          onSave={onSaveScene}
        />
      )}
      {group.id && group.beats?.length > 0 && group.script.trim() && (
        <Disclosure className="script-source" summary={t('script.sourceText')}>
          <ScriptText
            id={group.id}
            value={group.script}
            label={t('script.sceneLabel')}
            placeholder={t('script.sceneHint')}
            max={SCENE_TEXT_MAX}
            onSave={onSaveScene}
          />
        </Disclosure>
      )}
      <SceneBeats
        group={{ ...group, beats: group.beats || [] }}
        numbers={numbers}
        Text={ScriptText}
      />
      {group.shots.map((shot) => (
        <ScriptShot
          key={shot.id}
          id={shot.id}
          number={numbers.get(shot.id)}
          title={shot.title}
          prompt={shot.prompt}
          onSave={onSaveShot}
          onOpen={onOpenShot}
        />
      ))}
      {!group.shots.length && <p className="muted script-empty">{t('script.emptyScene')}</p>}
      {group.id && (
        <Button
          variant="ghost"
          size="sm"
          className="script-add keep-label"
          disabled={locked}
          onClick={() => onAddShot(group.id)}
        >
          <Plus size={14} />
          {t('storyboard.addShot')}
        </Button>
      )}
    </section>
  );
}

// The same layout as the page, so nothing moves when the story arrives.
function ScriptWaiting({ title }) {
  return (
    <section className="script-page" aria-busy="true">
      <div className="script-sheet">
        <header className="script-head">
          <h1>{title || ' '}</h1>
          <small className="muted">{' '}</small>
        </header>
        <div className="script-chapter">
          <div className="script-scene">
            <div className="script-scene-head">
              <i className="ui-wait script-wait-head" />
            </div>
            <i className="ui-wait script-wait-scene" />
            {[0, 1, 2, 3, 4, 5].map((row) => (
              <div className="script-shot" key={row}>
                <span className="script-shot-no" />
                <i className="ui-wait script-wait-text" />
              </div>
            ))}
          </div>
        </div>
      </div>
    </section>
  );
}

export function StoryScript({ chapters, groups, shots, onNewScene, onOpenShot }) {
  const { t } = useI18n();
  const { state, dispatch } = useStoryboardUI();
  // The page's callbacks stay the same between saves, so the other rows see nothing new.
  const latest = useRef(null);
  useLayoutEffect(() => {
    latest.current = { dispatch, onOpenShot };
  });
  const saveShot = useCallback(
    (id, prompt) => latest.current.dispatch({ type: 'update', id, patch: { prompt } }),
    [],
  );
  const saveScene = useCallback(
    (id, script) => latest.current.dispatch({ type: 'sceneScript', id, script }),
    [],
  );
  const openShot = useCallback((id) => latest.current.onOpenShot(id), []);
  // A shot added from here is ready to type in.
  const added = useRef(false);
  useEffect(() => {
    if (!added.current || !state.lastAdded) return;
    added.current = false;
    document.querySelector(`[data-script-shot="${state.lastAdded}"] textarea`)?.focus();
  }, [state.lastAdded]);
  if (state.placeholder) return <ScriptWaiting title={state.project.title} />;
  const locked = state.busy || !!state.workbenchPending;
  const addShot = (sceneId) => {
    added.current = true;
    dispatch({
      type: 'add',
      ...(sceneId ? { sceneId } : {}),
      title: t('storyboard.newShot'),
      sceneTitle: t('storyboard.newScene'),
    });
  };
  // The same numbers as the cards on the storyboard.
  const numbers = new Map(state.shots.map((shot, index) => [shot.id, index + 1]));
  const scene = (group) => (
    <ScriptScene
      key={group.id || 'unparented'}
      group={group}
      numbers={numbers}
      locked={locked}
      onSaveShot={saveShot}
      onSaveScene={saveScene}
      onAddShot={addShot}
      onOpenShot={openShot}
    />
  );
  const several = chapters.length > 1;
  return (
    <section className="script-page" aria-label={t('stage.script')}>
      <div className="script-sheet">
        <header className="script-head">
          <h1>{state.project.title}</h1>
          <small className="muted">
            {t('chapter.meta', { count: shots.length, duration: fmt(totalDuration(shots)) })}
          </small>
        </header>
        {!groups.length && (
          <>
            <p className="muted script-empty">{t('script.emptyStory')}</p>
            <Button
              variant="ghost"
              size="sm"
              className="script-add keep-label"
              disabled={locked}
              onClick={() => addShot()}
            >
              <Plus size={14} />
              {t('storyboard.addShot')}
            </Button>
          </>
        )}
        {chapters.map((chapter, index) => (
          <div className="script-chapter" key={chapter.id}>
            {several && (
              <h2>
                {t('chapter.number', { n: index + 1 })}
                {!DEFAULT_CHAPTER_NAMES.has(chapter.title) && ' · ' + chapter.title}
              </h2>
            )}
            {groups.filter((group) => group.chapterId === chapter.id).map(scene)}
            {onNewScene && (
              <Button
                variant="ghost"
                size="sm"
                className="script-add keep-label"
                onClick={() => {
                  store.setScope(chapter.id);
                  onNewScene();
                }}
              >
                <Plus size={14} />
                {t('chapter.addScene')}
              </Button>
            )}
          </div>
        ))}
        {groups.filter((group) => !group.chapterId).map(scene)}
      </div>
    </section>
  );
}
