// Inky (小墨, h3-studio#679): a quiet corner helper. It lists what Jev flagged; "去修改" opens the
// shot editor at the right field (guide link), then Inky flies there and says what to write.
// "帮我改" asks for a draft (#1065). Nothing changes until the person picks "用这个", and that is a
// normal edit, so undo works. Inky never starts a generation.
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { autoUpdate, flip, hide, offset, shift, useFloating } from '@floating-ui/react-dom';
import { useI18n } from '../i18n/I18nProvider.jsx';
import { navigateApp } from '../app-navigation.js';
import { Popover } from '../ui/Popover.jsx';
import { Button } from '../ui/Button.jsx';
import { InkyMark } from './InkyMark.jsx';
import {
  canRewrite,
  cornerState,
  isContinuity,
  itemField,
  itemHref,
  openItems,
  otherHref,
} from './inky-model.js';
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
function InkyPerch({ perch, onClose, onRewrite, onApply }) {
  const { t } = useI18n();
  const flyer = useRef(null),
    { node, from } = perch,
    [help, setHelp] = useState({ state: 'idle' });
  const ask = async () => {
    setHelp({ state: 'writing' });
    try {
      const { text } = await onRewrite(perch.id);
      setHelp({ state: 'draft', text });
    } catch (failure) {
      setHelp({ state: failure?.status === 429 ? 'limit' : 'failed' });
    }
  };
  const use = () => {
    onApply(perch.shotId, help.text);
    setHelp({ state: 'done' });
  };
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
        {help.state === 'draft' ? (
          <p className="inky-draft">{help.text}</p>
        ) : (
          <p>
            {help.state === 'idle'
              ? perch.note || t('inky.perch.' + perch.kind)
              : t('inky.help.' + help.state)}
          </p>
        )}
        <div className="inky-bubble-actions">
          {help.state === 'draft' && (
            <>
              <Button size="xs" variant="on-color" onClick={use}>
                {t('inky.help.use')}
              </Button>
              <Button size="xs" variant="on-color" onClick={onClose}>
                {t('inky.help.keep')}
              </Button>
            </>
          )}
          {help.state !== 'draft' && (
            <>
              {onRewrite &&
                canRewrite(perch.kind) &&
                ['idle', 'failed', 'writing'].includes(help.state) && (
                  <Button
                    size="xs"
                    variant="on-color"
                    disabled={help.state === 'writing'}
                    onClick={ask}
                  >
                    {t(help.state === 'failed' ? 'inky.help.again' : 'inky.help.ask')}
                  </Button>
                )}
              <Button size="xs" variant="on-color" onClick={onClose}>
                {t('inky.gotIt')}
              </Button>
            </>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}

// Typing to Inky (#1065). A reply says what changed; the latest change can be undone.
// The open tip for this shot, written as something to ask Inky; Tab or → fills it in.
const ASKS = [
  'vague_action',
  'needs_keyframe',
  'emotion_not_shown',
  'multiple_camera_moves',
  'action_too_long',
];
function askFor(tips, selectedShot, shotNumber, t) {
  const tip =
    selectedShot && tips.find((item) => item.shot_id === selectedShot && ASKS.includes(item.kind));
  return tip ? t('inky.ask.' + tip.kind, { number: shotNumber?.(selectedShot) ?? '' }) : '';
}

function InkyChat({ tips = [], shotNumber, selectedShot, onSend, onUndo }) {
  const { t } = useI18n();
  const [draft, setDraft] = useState(''),
    [talk, setTalk] = useState([]),
    [sending, setSending] = useState(false);
  const say = (entry) => setTalk((old) => [...old, entry]);
  const send = async () => {
    const text = draft.trim();
    if (!text || sending) return;
    setDraft('');
    say({ who: 'me', text });
    setSending(true);
    try {
      const turn = await onSend(text, selectedShot);
      const reply = turn.messages.find((message) => message.role === 'assistant')?.text || '';
      setTalk((old) => [
        ...old.map((entry) => ({ ...entry, undo: undefined })),
        { who: 'inky', text: reply, undo: turn.changes?.length ? turn.turn_id : undefined },
      ]);
    } catch (failure) {
      say({
        who: 'inky',
        text: t(failure?.status === 429 ? 'inky.chat.limit' : 'inky.chat.failed'),
      });
      setDraft(text);
    } finally {
      setSending(false);
    }
  };
  const undo = async (entry) => {
    setTalk((old) => old.map((item) => (item === entry ? { ...item, undo: undefined } : item)));
    try {
      await onUndo(entry.undo);
      say({ who: 'inky', text: t('inky.chat.undone') });
    } catch {
      say({ who: 'inky', text: t('inky.chat.undoFailed') });
    }
  };
  const ask = askFor(tips, selectedShot, shotNumber, t);
  return (
    <div className="inky-chat" data-keep-open>
      {talk.length > 0 && (
        <ul className="inky-thread">
          {talk.map((entry, index) => (
            <li key={index} className={'inky-say ' + entry.who}>
              <p>{entry.text}</p>
              {entry.undo && (
                <Button size="xs" variant="secondary" onClick={() => undo(entry)}>
                  {t('inky.chat.undo')}
                </Button>
              )}
            </li>
          ))}
          {sending && (
            <li className="inky-say inky">
              <p>{t('inky.chat.thinking')}</p>
            </li>
          )}
        </ul>
      )}
      <form
        className="inky-compose"
        onSubmit={(event) => {
          event.preventDefault();
          void send();
        }}
      >
        <textarea
          rows={2}
          value={draft}
          maxLength={2000}
          aria-label={t('inky.chat.label')}
          placeholder={
            ask
              ? t('inky.chat.askHint', { ask })
              : selectedShot
                ? t('inky.chat.shotHint', { number: shotNumber?.(selectedShot) ?? '' })
                : t('inky.chat.storyHint')
          }
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (
              ask &&
              !draft &&
              (event.key === 'Tab' || event.key === 'ArrowRight') &&
              !event.shiftKey &&
              !event.nativeEvent.isComposing
            ) {
              event.preventDefault();
              setDraft(ask);
              return;
            }
            if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault();
              void send();
            }
          }}
        />
        <Button size="sm" variant="primary" type="submit" disabled={!draft.trim() || sending}>
          {t('inky.chat.send')}
        </Button>
      </form>
    </div>
  );
}

export function Inky({
  projectId,
  items,
  shotIds,
  shotNumber,
  selectedShot,
  onIgnore,
  onRewrite,
  onApply,
  onSend,
  onUndoSend,
}) {
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
    setPending({
      id: item.id,
      kind: item.kind,
      note: item.note,
      shotId: item.shot_id,
      field: itemField(item),
    });
  };
  const ignore = (item) => {
    setIgnored((old) => new Set(old).add(item.id));
    onIgnore?.(item);
  };
  return (
    <>
      {perch && (
        <InkyPerch perch={perch} onClose={closePerch} onRewrite={onRewrite} onApply={onApply} />
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
            {open.map((item) => {
              const other = item.other_shot_id ? shotNumber?.(item.other_shot_id) : 0;
              return (
                <li key={item.id}>
                  <span className="inky-where">
                    {t('inky.shot', { number: shotNumber?.(item.shot_id) ?? '' })}
                  </span>
                  <p>
                    {isContinuity(item.kind)
                      ? t('inky.tip.' + item.kind + (other ? '' : '_card'), { other })
                      : t('inky.tip.' + item.kind, { card: item.card_name || '' })}
                  </p>
                  {item.note && <p className="inky-example">{item.note}</p>}
                  {item.example && (
                    <p className="inky-example">{t('inky.example', { example: item.example })}</p>
                  )}
                  <div className="inky-actions">
                    <Button size="sm" variant="primary" onClick={() => go(item)}>
                      {t('inky.go')}
                    </Button>
                    {other > 0 && (
                      <Button
                        size="sm"
                        variant="secondary"
                        onClick={() => navigateApp(otherHref(item, projectId), { restore: false })}
                      >
                        {t('inky.seeOther', { number: other })}
                      </Button>
                    )}
                    <span data-keep-open>
                      <Button size="sm" variant="secondary" onClick={() => ignore(item)}>
                        {t('inky.ignore')}
                      </Button>
                    </span>
                  </div>
                </li>
              );
            })}
          </ul>
          {onSend && (
            <InkyChat
              tips={open}
              shotNumber={shotNumber}
              selectedShot={selectedShot}
              onSend={onSend}
              onUndo={onUndoSend}
            />
          )}
        </Popover>
      </div>
    </>
  );
}
