// Arrow keys between cards (WAI-ARIA APG roving tabindex, as in its grid and toolbar patterns).
// A board of shot cards, the world list or the library grid is one Tab stop: the arrows move
// between cards by where they sit on screen, Home/End go to the first and last, and only the
// focused card's own buttons and fields are in the Tab order. Cards open on Enter or Space
// themselves; dragging starts from their handle only.
import { useLayoutEffect, useRef, useState } from 'react';

export const MOVE_KEYS = ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'];

/**
 * The card a key moves to. `rects` are the cards' boxes in reading order ({ left, top, width,
 * height }); ←/→ follow reading order, ↑/↓ go to the nearest card in the row above or below.
 * Returns `from` when there is nowhere to go.
 */
export function nextCard(rects, from, key) {
  const n = rects.length;
  if (!n) return -1;
  if (key === 'Home') return 0;
  if (key === 'End') return n - 1;
  if (key === 'ArrowRight') return Math.min(n - 1, from + 1);
  if (key === 'ArrowLeft') return Math.max(0, from - 1);
  if (key !== 'ArrowDown' && key !== 'ArrowUp') return from;
  const here = rects[from],
    down = key === 'ArrowDown',
    middle = (r) => r.left + r.width / 2;
  const beyond = rects
    .map((rect, index) => ({ rect, index }))
    .filter(({ rect }) =>
      down ? rect.top >= here.top + here.height / 2 : rect.top + rect.height / 2 <= here.top,
    );
  if (!beyond.length) return from;
  // A card straight above or below wins over a nearer row elsewhere: beside a group box the
  // group's members sit lower than the single cards, but ↓ from a single card means its column.
  const column = beyond.filter(
    ({ rect }) => rect.left < here.left + here.width && rect.left + rect.width > here.left,
  );
  const others = column.length ? column : beyond;
  const edge = down
    ? Math.min(...others.map(({ rect }) => rect.top))
    : Math.max(...others.map(({ rect }) => rect.top));
  const row = others.filter(({ rect }) => Math.abs(rect.top - edge) < 8);
  row.sort(
    (a, b) => Math.abs(middle(a.rect) - middle(here)) - Math.abs(middle(b.rect) - middle(here)),
  );
  return row[0].index;
}

const INNER = 'button, a[href], input, select, textarea, [tabindex]';
const OFF = 'data-grid-off';

// Only the active card and its own controls can be reached with Tab. Every other card carries
// an explicit tabindex="-1", so a click or a script can still focus it (and Enter then opens
// it): a div's tabIndex property already reads -1 without the attribute, so compare the
// attribute, not the property.
export function applyRovingStops(root, selector, active) {
  const cards = [...root.querySelectorAll(selector)];
  if (!cards.length) return;
  const current =
    cards.find((card) => card.dataset.gridCard === active.current) ||
    cards.find((card) => card.classList.contains('selected')) ||
    cards[0];
  for (const card of cards) {
    const on = card === current;
    if (card.getAttribute('tabindex') !== (on ? '0' : '-1'))
      card.setAttribute('tabindex', on ? '0' : '-1');
    for (const node of card.querySelectorAll(INNER)) {
      if (!on) {
        if (!node.hasAttribute(OFF)) node.setAttribute(OFF, node.getAttribute('tabindex') ?? '');
        if (node.getAttribute('tabindex') !== '-1') node.setAttribute('tabindex', '-1');
      } else if (node.hasAttribute(OFF)) {
        const was = node.getAttribute(OFF);
        node.removeAttribute(OFF);
        if (was === '') node.removeAttribute('tabindex');
        else node.setAttribute('tabindex', was);
      }
    }
  }
}

/**
 * Pass the returned callback ref to the element that holds the cards (it may come and go).
 * Cards carry `data-grid-card="<id>"`; `selector` may narrow which ones belong to this grid.
 */
export function useCardGrid(selector = '[data-grid-card]') {
  const [root, setRoot] = useState(null),
    active = useRef(null);
  useLayoutEffect(() => {
    if (root) applyRovingStops(root, selector, active);
  });
  useLayoutEffect(() => {
    if (!root) return undefined;
    const refresh = () => applyRovingStops(root, selector, active);
    const focus = (event) => {
      const card = event.target.closest?.(selector);
      if (!card || !root.contains(card) || card.dataset.gridCard === active.current) return;
      active.current = card.dataset.gridCard;
      refresh();
    };
    const keys = (event) => {
      if (!MOVE_KEYS.includes(event.key) || event.altKey || event.ctrlKey || event.metaKey) return;
      const card = event.target;
      if (!card.matches?.(selector) || !root.contains(card)) return;
      const cards = [...root.querySelectorAll(selector)],
        from = cards.indexOf(card),
        to = nextCard(
          cards.map((node) => node.getBoundingClientRect()),
          from,
          event.key,
        );
      event.preventDefault();
      if (to < 0 || to === from) return;
      active.current = cards[to].dataset.gridCard;
      refresh();
      cards[to].focus();
      cards[to].scrollIntoView?.({ block: 'nearest', inline: 'nearest' });
    };
    // Cards and their buttons come and go as the story changes.
    const observer = globalThis.MutationObserver ? new MutationObserver(refresh) : null;
    observer?.observe(root, { childList: true, subtree: true });
    root.addEventListener('focusin', focus);
    root.addEventListener('keydown', keys);
    return () => {
      observer?.disconnect();
      root.removeEventListener('focusin', focus);
      root.removeEventListener('keydown', keys);
    };
  }, [root, selector]);
  return setRoot;
}

/** Enter or Space on the card itself (not on one of its buttons) opens it. */
export function openKey(event, open) {
  if (event.target !== event.currentTarget) return;
  if (event.key !== 'Enter' && event.key !== ' ') return;
  if (event.repeat) return;
  event.preventDefault();
  open(event);
}
