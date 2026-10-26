import React from 'react';
import InkMark from './InkMark.jsx';
import './ink-loader.css';

// Loading state: the brand ink-drop mark bobbing like a drop about to fall. It also shows
// before sign-in, so it keeps the default face; the daily faces live in the studio header.
export default function InkLoader({ label, className = '' }) {
  return (
    <div className={'ink-loader ' + className} role="status">
      <InkMark size={44} />
      {label && <span>{label}</span>}
    </div>
  );
}
