// Inky (小墨, h3-studio#679): a quiet corner helper. It lists what Jev flagged; "去修改" opens the
// shot editor at the right field (guide link), then Inky flies there and says what to write.
// Inky never edits anything itself.
import React, { useEffect, useRef, useState } from 'react';
import { useI18n } from '../i18n/I18nProvider.jsx';
import { navigateApp } from '../app-navigation.js';
import { Popover } from '../ui/Popover.jsx';
import { Button } from '../ui/Button.jsx';
import { InkyMark } from './InkyMark.jsx';
import { cornerState, itemField, itemHref, openItems, perchPoint } from './inky-model.js';
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

export function Inky({ projectId, items, shotIds, shotNumber, onIgnore }) {
  const { t } = useI18n();
  const [ignored, setIgnored] = useState(() => new Set()),
    [seen, setSeen] = useState(0),
    [perch, setPerch] = useState(null),
    [typing, setTyping] = useState(false);
  const corner = useRef(null),
    flyer = useRef(null);
  const open = openItems(items, { ignored, shotIds });
  const state = perch ? 'point' : cornerState({ count: open.length, typing, seen });

  // Step aside while the person types in the field Inky pointed at.
  useEffect(() => {
    if (!perch) return;
    const node = document.querySelector(`[data-guide-field="${perch.field}"]`);
    const typed = () => {
      setTyping(true);
      setPerch(null);
    };
    node?.addEventListener('input', typed, { once: true });
    return () => node?.removeEventListener('input', typed);
  }, [perch]);
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
      const rect = node.getBoundingClientRect(),
        at = perchPoint(rect, { width: window.innerWidth, height: window.innerHeight });
      const from = corner.current?.getBoundingClientRect();
      setPerch({ ...pending, ...at, from: from && { x: from.left, y: from.top } });
    });
  }, [pending]);
  useEffect(() => {
    if (!perch?.from || !flyer.current) return;
    const box = flyer.current.getBoundingClientRect();
    fly(flyer.current, perch.from, { x: box.left, y: box.top });
  }, [perch]);

  const go = (item) => {
    const href = itemHref(item, projectId);
    if (!href) return;
    navigateApp(href, { restore: false });
    setPending({ id: item.id, kind: item.kind, field: itemField(item) });
  };
  const ignore = (item) => {
    setIgnored((old) => new Set(old).add(item.id));
    onIgnore?.(item);
  };
  if (!open.length && !perch) return null;
  return (
    <>
      {perch && (
        <div
          className={'inky-perch side-' + perch.side}
          style={
            perch.side === 'right'
              ? { left: perch.left, top: perch.top }
              : { right: perch.right, top: perch.top }
          }
          role="status"
        >
          <div ref={flyer} className="inky-flyer">
            <InkyMark state="point" size={56} />
          </div>
          <div className="inky-bubble">
            <p>{t('inky.perch.' + perch.kind)}</p>
            <Button size="xs" variant="on-color" onClick={() => setPerch(null)}>
              {t('inky.gotIt')}
            </Button>
          </div>
        </div>
      )}
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
