import { useState } from 'react';
import { speechEdit, speechParts, speechShown, speechStored } from './speech-edit.js';

/**
 * A shot text box without speech tags (h3-studio#1113). `shown` goes in the box; `edit(after,
 * caret)` takes the box's new text and returns the text to save, tags put back. A new stored
 * value from elsewhere (another editor, undo, the server) starts over from it.
 */
export function useSpeechText(stored) {
  const value = String(stored ?? '');
  const [state, setState] = useState(() => ({ stored: value, parts: speechParts(value) }));
  let current = state;
  if (state.stored !== value) {
    current = { stored: value, parts: speechParts(value) };
    setState(current);
  }
  return {
    shown: speechShown(current.parts),
    edit(after, caret) {
      const parts = speechEdit(current.parts, after, caret);
      current = { stored: speechStored(parts), parts };
      setState(current);
      return current.stored;
    },
  };
}
