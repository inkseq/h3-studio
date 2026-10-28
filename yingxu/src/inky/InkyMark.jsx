// Inky's body (h3-studio#679): the brand ink drop with the play-button eye, which never changes.
// The other eye and the pose follow `state` (see inky.css and the motion spec on the design canvas).
import React from 'react';
import { INK_DROP } from '../InkMark.jsx';

export const INKY_STATES = Object.freeze([
  'idle',
  'think',
  'notify',
  'fly',
  'land',
  'point',
  'talk',
  'done',
  'quiet',
  'drag',
]);

export function InkyMark({ state = 'idle', size = 44 }) {
  const happy = state === 'done',
    closed = state === 'quiet';
  return (
    <svg
      className={'inky-mark inky-' + (INKY_STATES.includes(state) ? state : 'idle')}
      width={size}
      height={size}
      viewBox="0 0 100 100"
      aria-hidden="true"
      focusable="false"
    >
      {state === 'notify' && <circle className="inky-ring" cx="50" cy="68" r="30" />}
      <g className="inky-body">
        <g transform="rotate(8 50 60)">
          <path d={INK_DROP} className="inky-drop" />
          <g className="inky-eyes">
            <path d="M39 58.5 L47.5 64 L39 69.5Z" className="inky-feat" />
            {happy ? (
              <path d="M53.5 66 Q58 59 62.5 66" className="inky-line" />
            ) : closed ? (
              <path d="M53.5 64.5 L62.5 64.5" className="inky-line" />
            ) : (
              <circle cx="58" cy="64" r="4.6" className="inky-feat" />
            )}
          </g>
        </g>
      </g>
      {state === 'think' && (
        <g className="inky-dots">
          <circle cx="66" cy="10" r="3" />
          <circle cx="76" cy="6" r="3" />
          <circle cx="86" cy="2" r="3" />
        </g>
      )}
    </svg>
  );
}
