// Inky (小墨, h3-studio#679): a quiet corner helper. It lists what Jev flagged; "去修改" opens the
// shot editor at the right field (guide link), then Inky flies there and says what to write.
// Inky never edits anything itself.
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { autoUpdate, flip, hide, offset, shift, useFloating } from '@floating-ui/react-dom';
import { useI18n } from '../i18n/I18nProvider.jsx';
import { navigateApp } from '../app-navigation.js';
import { Popover } from '../ui/Popover.jsx';
import { Button } from '../ui/Button.jsx';
import { InkyMark } from './InkyMark.jsx';
import { cornerState, itemField, itemHref, openItems } from './inky-model.js';
import './inky.css';

const reducedMotion = () => window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;

// Wait until the editor shows the field and its open animation has settled.
function whenFieldShows(field, done) {
  let tries = 0,
    timer = 0;
  const look = () => {
    const node = document.querySelector(`[data-guide-field="${field}"]`);
    if (node || ++tries > 40) {
      timer = setTimeout(() => done(node), node ? 450 : 0);
      return;
    }
    timer = setTimeout(look, 50);
  };
  look();
  return () => clearTimeout(timer);
}

function fly(flyer, from, to) {
  if (!flyer?.animate || reducedMotion()) return;
  const dx = to.x - from.x,
    dy = to.y - from.y;
  flyer.animate(
    [
      { transform: `translate(${-dx}px, ${-dy}px) scale(1, 1)` },
      {
        transform: `translate(${-dx * 0.5}px, ${-dy * 0.5 - 90}px) rotate(-20deg) scale(0.88, 1.16)`,
        offset: 0.5,
      },
      { transform: 'translate(0, 0) rotate(-14deg) scale(1.14, 0.86)', offset: 0.85 },
      { transform: 'translate(0, 0) rotate(-14deg) scale(1, 1)' },
    ],
    { duration: 900, easing: 'cubic-bezier(.45,0,.2,1)' },
  );
}

// Inky sits on the top edge of the field. It follows the field as anything moves, hides while the
// field is scrolled out of view, and leaves once the field is gone.
function InkyPerch({ perch, onClose }) {
  const { t } = useI18n();
  const flyer = useRef(null),
    { node, from } = perch;
  const { refs, floatingStyles, middlewareData, isPositioned } = useFloating({
    strategy: 'fixed',
    placement: 'top-end',
    elements: { reference: node },
    whileElementsMounted: (reference, floating, update) =>
      autoUpdate(reference, floating, update, { animationFrame: true }),
    middleware: [offset(-14), flip({ padding: 8 }), shift({ padding: 8 }), hide()],
  });
  useEffect(() => {
    const gone = new MutationObserver(() => node.isConnected || onClose());
    gone.observe(document.body, { childList: true, subtree: true });
    return () => gone.disconnect();
  }, [node, onClose]);
  useEffect(() => {
    if (!isPositioned || !from || !flyer.current) return;
    const box = flyer.current.getBoundingClientRect();
    fly(flyer.current, from, { x: box.left, y: box.top });
  }, [isPositioned, from]);
  // On the page itself: inside the toolbar it would stay under the shot panel.
  return createPortal(
    <div
      ref={refs.setFloating}
      className="inky-perch"
      style={{
        ...floatingStyles,
        visibility: middlewareData.hide?.referenceHidden ? 'hidden' : undefined,
      }}
      role="status"
    >
      <div ref={flyer} className="inky-flyer">
        <InkyMark state="point" size={44} />
      </div>
      <div className="inky-bubble">
        <p>{t('inky.perch.' + perch.kind)}</p>
        <Button size="xs" variant="on-color" onClick={onClose}>
          {t('inky.gotIt')}
        </Button>
      </div>
    </div>,
    document.body,
  );
}

export function Inky({ projectId, items, shotIds, shotNumber, selectedShot, onIgnore }) {
  const { t } = useI18n();
  const [ignored, setIgnored] = useState(() => new Set()),
    [seen, setSeen] = useState(0),
    [perch, setPerch] = useState(null),
    [typing, setTyping] = useState(false);
  const corner = useRef(null);
  const open = openItems(items, { ignored, shotIds });
  const state = perch ? 'point' : cornerState({ count: open.length, typing, seen });

  // Step aside while the person types in the field Inky pointed at.
  useEffect(() => {
    if (!perch) return;
    const typed = () => {
      setTyping(true);
      setPerch(null);
    };
    perch.node.addEventListener('input', typed, { once: true });
    return () => perch.node.removeEventListener('input', typed);
  }, [perch]);
  // The field now belongs to another shot: the advice no longer applies to it.
  const perchShot = perch?.shotId;
  useEffect(() => {
    if (perchShot && selectedShot !== perchShot) setPerch(null);
  }, [perchShot, selectedShot]);
  useEffect(() => {
    if (!typing) return;
    const timer = setTimeout(() => setTyping(false), 4000);
    return () => clearTimeout(timer);
  }, [typing]);

  const [pending, setPending] = useState(null);
  useEffect(() => {
    if (!pending) return;
    return whenFieldShows(pending.field, (node) => {
      setPending(null);
      if (!node) return;
      // The panel returns focus to Inky's button as it closes; put it back in the field.
      node.querySelector('textarea, input, select')?.focus({ preventScroll: true });
      const from = corner.current?.getBoundingClientRect();
      setPerch({ ...pending, node, from: from && { x: from.left, y: from.top } });
    });
  }, [pending]);

  const closePerch = useCallback(() => setPerch(null), []);
  const go = (item) => {
    const href = itemHref(item, projectId);
    if (!href) return;
    navigateApp(href, { restore: false });
    setPending({ id: item.id, kind: item.kind, shotId: item.shot_id, field: itemField(item) });
  };
  const ignore = (item) => {
    setIgnored((old) => new Set(old).add(item.id));
    onIgnore?.(item);
  };
  return (
    <>
      {perch && <InkyPerch perch={perch} onClose={closePerch} />}
      <div className="inky-corner" ref={corner} data-ui-surface="inky">
        <Popover
          align="end"
          className="inky-panel"
          onOpenChange={(next) => next && setSeen(open.length)}
          trigger={
            <Button
              variant="ghost"
              className="inky-button"
              aria-label={
                open.length ? t('inky.open', { count: open.length }) : t('inky.openEmpty')
              }
            >
              <InkyMark state={perch ? 'quiet' : state} size={30} />
              {open.length > seen && <span className="inky-badge">{open.length}</span>}
            </Button>
          }
        >
          <div className="inky-head">
            <InkyMark state="talk" size={28} />
            <strong>{t('inky.name')}</strong>
          </div>
          {open.length === 0 && <p className="inky-empty">{t('inky.empty')}</p>}
          <ul className="inky-items">
            {open.map((item) => (
              <li key={item.id}>
                <span className="inky-where">
                  {t('inky.shot', { number: shotNumber?.(item.shot_id) ?? '' })}
                </span>
                <p>{t('inky.tip.' + item.kind, { card: item.card_name || '' })}</p>
                {item.example && (
                  <p className="inky-example">{t('inky.example', { example: item.example })}</p>
                )}
                <div className="inky-actions">
                  <Button size="sm" variant="primary" onClick={() => go(item)}>
                    {t('inky.go')}
                  </Button>
                  <span data-keep-open>
                    <Button size="sm" variant="secondary" onClick={() => ignore(item)}>
                      {t('inky.ignore')}
                    </Button>
                  </span>
                </div>
              </li>
            ))}
          </ul>
        </Popover>
      </div>
    </>
  );
}
