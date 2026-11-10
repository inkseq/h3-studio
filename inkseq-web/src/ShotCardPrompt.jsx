// A shot's prompt on its own card (h3-studio#1040), saved through the same edit as the side editor.
// `@` offers the story's characters, places and props and adds the chosen one to the shot.
// Spoken lines show and are edited as 「台词」 without the video model's tags, which saving keeps
// (h3-studio#1113, speech-edit.js).
import React, { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useI18n } from './i18n/I18nProvider.jsx';
import { Button } from './ui/Button.jsx';
import { useStoryboardUI as useStore } from './storyboard-ui-context.jsx';
import { mentionAt, mentionOptions, withMention } from './shot-mention.js';
import { useSpeechText } from './use-speech-text.js';

const stop = (event) => event.stopPropagation();

export function ShotCardPrompt({ shot }) {
  const { t } = useI18n();
  const { state, dispatch } = useStore();
  const box = useRef(null);
  const [mention, setMention] = useState(null),
    [active, setActive] = useState(0);
  const speech = useSpeechText(shot.prompt);
  const options = mention ? mentionOptions(state.profiles, mention.query) : [];
  const shut = () => setMention(null);
  useEffect(() => {
    if (!mention) return undefined;
    // The menu sits under the box, so the page moving closes it. The box scrolling as it wraps does not.
    const moved = (event) => {
      if (event.target !== box.current) shut();
    };
    window.addEventListener('scroll', moved, true);
    return () => window.removeEventListener('scroll', moved, true);
  }, [mention]);
  const read = (node) => {
    const found = mentionAt(node.value, node.selectionStart);
    if (!found) return shut();
    const rect = node.getBoundingClientRect();
    setActive(0);
    setMention({ ...found, left: rect.left, top: rect.bottom + 4, width: rect.width });
  };
  const pick = (profile) => {
    const next = withMention(speech.shown, mention, profile.name);
    dispatch({
      type: 'update',
      id: shot.id,
      patch: { prompt: speech.edit(next.text, next.caret) },
    });
    dispatch({ type: 'drop', profile: profile.id, shot: shot.id, slot: 'smart' });
    shut();
    requestAnimationFrame(() => {
      box.current?.focus();
      box.current?.setSelectionRange(next.caret, next.caret);
    });
  };
  const keys = (event) => {
    event.stopPropagation();
    if (!options.length) return;
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      const step = event.key === 'ArrowDown' ? 1 : options.length - 1;
      setActive((value) => (value + step) % options.length);
    } else if (event.key === 'Enter') {
      event.preventDefault();
      pick(options[active]);
    } else if (event.key === 'Escape') {
      shut();
    }
  };
  return (
    <div className="card-prompt" onClick={stop} onPointerDown={stop} onContextMenu={stop}>
      <textarea
        ref={box}
        className="card-prompt-input"
        rows={3}
        value={speech.shown}
        aria-label={t('shotCard.prompt')}
        placeholder={t('shotCard.promptHint')}
        onChange={(event) => {
          const prompt = speech.edit(event.target.value, event.target.selectionEnd);
          dispatch({ type: 'update', id: shot.id, patch: { prompt } });
          read(event.target);
        }}
        onKeyDown={keys}
        onBlur={() => setTimeout(shut, 120)}
      />
      {mention &&
        options.length > 0 &&
        createPortal(
          <ul
            className="card-mention"
            role="listbox"
            aria-label={t('shotCard.mention')}
            style={{ left: mention.left, top: mention.top, width: Math.max(mention.width, 180) }}
          >
            {options.map((profile, index) => (
              <li key={profile.id}>
                <Button
                  size="sm"
                  variant="ghost"
                  role="option"
                  aria-selected={index === active}
                  className={'card-mention-item' + (index === active ? ' active' : '')}
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => pick(profile)}
                >
                  {profile.name}
                </Button>
              </li>
            ))}
          </ul>,
          document.body,
        )}
    </div>
  );
}
