import React from 'react';
import InkMark from './InkMark.jsx';
import { dailyInkMark } from './ink-mark-daily.js';
import './ink-loader.css';

// Loading state: today's ink-drop mark bobbing like a drop about to fall.
export default function InkLoader({ label, className = '' }) {
  return (
    <div className={'ink-loader ' + className} role="status">
      <InkMark variant={dailyInkMark()} size={44} />
      {label && <span>{label}</span>}
    </div>
  );
}
