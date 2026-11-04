// A reversible choice changes on the press and the server follows (#1032). Never use this for
// credits, payment, generation or anything that cannot be undone.
import { useEffect, useOptimistic, useRef, useState, useTransition } from 'react';

const NONE = Symbol('none');

// One write at a time, and only the newest choice made while one was out. A failed write
// does not stop the next; whoever passes `commit` reports the failure.
export function latestOnly(commit) {
  let wanted = NONE,
    running = null;
  return (value) => {
    wanted = value;
    running ||= (async () => {
      try {
        while (wanted !== NONE) {
          const next = wanted;
          wanted = NONE;
          await Promise.resolve(commit(next)).catch(() => {});
        }
      } finally {
        running = null;
      }
    })();
    return running;
  };
}

// Shows `actual` until a press, then the pressed value until the write and its reload finish.
// A refused write simply falls back to `actual`.
export function useOptimisticChoice(actual, commit) {
  const [shown, show] = useOptimistic(actual, (_, next) => next),
    [, start] = useTransition(),
    latest = useRef(commit);
  useEffect(() => {
    latest.current = commit;
  });
  const [write] = useState(() => latestOnly((value) => latest.current(value)));
  const choose = (value) =>
    start(async () => {
      show(value);
      await write(value);
    });
  return [shown, choose];
}
