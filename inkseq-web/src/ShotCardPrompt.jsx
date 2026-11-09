// A shot's prompt on its own card (h3-studio#1040), saved through the same edit as the side editor.
// `@` offers the story's characters, places and props and adds the chosen one to the shot.
// While the box is not being edited, the video model's line markup reads as "名字：台词"
// (prompt-display.js); editing shows and saves the stored text.
import React, { useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useI18n } from './i18n/I18nProvider.jsx';
import { Button } from './ui/Button.jsx';
import { useStoryboardUI as useStore } from './storyboard-ui-context.jsx';
import { mentionAt, mentionOptions, withMention } from './shot-mention.js';
import { hasModelLines, readablePrompt, speakerNames } from './prompt-display.js';

const stop = (event) => event.stopPropagation();

export function ShotCardPrompt({ shot }) {
  const { t } = useI18n();
  const { state, dispatch } = useStore();
  const box = useRef(null);
  const [mention, setMention] = useState(null),
    [active, setActive] = useState(0),
    [editing, setEditing] = useState(false);
  const profiles = state.profiles;
  const names = useMemo(() => speakerNames(profiles), [profiles]);
  const shown =
    editing || !hasModelLines(shot.prompt) ? shot.prompt : readablePrompt(shot.prompt, names);
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
    const next = withMention(shot.prompt, mention, profile.name);
    dispatch({ type: 'update', id: shot.id, patch: { prompt: next.text } });
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
        value={shown}
        aria-label={t('shotCard.prompt')}
        placeholder={t('shotCard.promptHint')}
        onChange={(event) => {
          dispatch({ type: 'update', id: shot.id, patch: { prompt: event.target.value } });
          read(event.target);
        }}
        onKeyDown={keys}
        onFocus={() => setEditing(true)}
        onBlur={() => {
          setEditing(false);
          setTimeout(shut, 120);
        }}
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
