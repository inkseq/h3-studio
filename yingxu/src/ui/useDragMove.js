import { useState } from 'react';

/**
 * Lets a floating panel be moved by dragging its header (#232 #52). Returns the current
 * offset and the props for the header; presses on buttons and fields inside it still work.
 */
export function useDragMove() {
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  const onPointerDown = (event) => {
    if (event.button > 0 || event.target.closest('button, input, select, textarea, a')) return;
    event.preventDefault();
    const head = event.currentTarget,
      from = { x: event.clientX - offset.x, y: event.clientY - offset.y };
    head.setPointerCapture(event.pointerId);
    const move = (e) => setOffset({ x: e.clientX - from.x, y: e.clientY - from.y });
    const up = () => {
      head.removeEventListener('pointermove', move);
      head.removeEventListener('pointerup', up);
      head.removeEventListener('pointercancel', up);
    };
    head.addEventListener('pointermove', move);
    head.addEventListener('pointerup', up);
    head.addEventListener('pointercancel', up);
  };
  return [offset, { onPointerDown, 'data-drag-handle': '' }];
}
