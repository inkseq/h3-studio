import React, { useLayoutEffect, useRef, useState } from 'react';
import { useI18n } from './i18n/I18nProvider.jsx';

// Presentation only: the complete immutable prompt stays in the DOM and recipe.
export default function QuickPrompt({ text }) {
  const { t } = useI18n(),
    ref = useRef(null),
    [expanded, setExpanded] = useState(false),
    [overflow, setOverflow] = useState(false);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el || expanded) return;
    const measure = () => setOverflow(el.scrollHeight > el.clientHeight + 1);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [text, expanded]);
  return (
    <div className="quick-prompt-preview">
      <p ref={ref} className={'quick-recipe-prompt' + (expanded ? ' expanded' : ' collapsed')}>
        {text}
      </p>
      {(overflow || expanded) && (
        <button
          type="button"
          className="ghost small quick-prompt-toggle"
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
        >
          {t(expanded ? 'quickView.showLess' : 'quickView.showMore')}
        </button>
      )}
    </div>
  );
}
