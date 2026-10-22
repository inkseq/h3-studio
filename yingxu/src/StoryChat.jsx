import React, { useEffect, useMemo, useReducer, useRef, useState } from 'react';
import {
  ArrowUp,
  Check,
  Film,
  ImagePlus,
  MessageSquare,
  Paperclip,
  Sparkles,
  X,
} from 'lucide-react';
import StudioHeader from './StudioHeader.jsx';
import { useI18n } from './i18n/I18nProvider.jsx';
import { navigateApp } from './app-navigation.js';
import {
  CARD_GROUPS,
  applyEvent,
  cardsByKind,
  editCard,
  emptyChat,
  progress,
  quoteAffordable,
} from './story-chat-model.js';
import { createPreviewChat } from './story-chat-preview.js';
import './story-chat.css';

function reducer(state, action) {
  if (action.type === 'edit') return editCard(state, action.id, action.patch);
  if (action.type === 'reset') return emptyChat();
  return applyEvent(state, action.event);
}

const cardName = (card, t) =>
  card.kind === 'shot' ? t('storyChat.shotN', { n: card.order }) : card.name || '—';

function Thumb({ card, wide = false }) {
  return (
    <span
      className={'sc-thumb' + (wide ? ' wide' : '') + (card.status === 'generating' ? ' busy' : '')}
    >
      {card.image ? <img src={card.image} alt="" /> : <span>{(card.name || '').slice(0, 1)}</span>}
    </span>
  );
}

function QuoteCard({ quote, balance, onSettle }) {
  const { t } = useI18n();
  const affordable = quoteAffordable(quote, balance);
  return (
    <div className={'sc-quote ' + quote.status}>
      <div className="sc-quote-head">
        <Sparkles size={15} aria-hidden="true" />
        <strong>{quote.label}</strong>
        <span className="sc-credits">
          {t('storyChat.quoteCredits', { credits: quote.credits })}
        </span>
      </div>
      {quote.status === 'open' ? (
        <div className="sc-quote-actions">
          <small>{t('storyChat.quoteBalance', { balance })}</small>
          {affordable ? (
            <button className="sc-primary keep-label" onClick={() => onSettle(quote, true)}>
              {t('storyChat.confirm')}
            </button>
          ) : (
            <>
              <small className="sc-short">{t('storyChat.short')}</small>
              <button className="sc-primary keep-label" onClick={() => navigateApp('/pricing')}>
                {t('storyChat.buy')}
              </button>
            </>
          )}
          <button className="sc-ghost keep-label" onClick={() => onSettle(quote, false)}>
            {t('storyChat.notNow')}
          </button>
        </div>
      ) : (
        <small className="sc-quote-done">
          {quote.status === 'confirmed' ? (
            <>
              <Check size={13} aria-hidden="true" /> {t('storyChat.confirmed')}
            </>
          ) : (
            t('storyChat.declined')
          )}
        </small>
      )}
    </div>
  );
}

function CardDetail({ card, profiles, busy, onEdit, onPicture, onShot, onAsk, onClose }) {
  const { t } = useI18n();
  const [name, setName] = useState(card.name || '');
  const [text, setText] = useState(card.text || '');
  useEffect(() => {
    setName(card.name || '');
    setText(card.text || '');
  }, [card.id, card.name, card.text]);
  const commit = () => {
    const patch = {};
    if (card.kind !== 'shot' && name.trim() && name !== card.name) patch.name = name.trim();
    if (text !== card.text) patch.text = text;
    if (Object.keys(patch).length) onEdit(card.id, patch);
  };
  const shot = card.kind === 'shot';
  const refs = card.refs || [];
  return (
    <aside className="sc-detail" aria-label={cardName(card, t)}>
      <header>
        <strong>{cardName(card, t)}</strong>
        {shot && <small>{t('storyChat.time', { start: card.start, end: card.end })}</small>}
        <button
          className="sc-icon"
          aria-label={t('storyChat.close')}
          onClick={() => {
            commit();
            onClose();
          }}
        >
          <X size={16} />
        </button>
      </header>
      <div className={'sc-detail-picture' + (shot ? ' wide' : '')}>
        {card.image ? (
          <img src={card.image} alt="" />
        ) : (
          <span>
            {card.status === 'generating'
              ? t('storyChat.status.generating')
              : t('storyChat.detailNoPicture')}
          </span>
        )}
      </div>
      {!shot && (
        <label>
          {t('storyChat.detailName')}
          <input value={name} onChange={(event) => setName(event.target.value)} onBlur={commit} />
        </label>
      )}
      <label>
        {shot ? t('storyChat.detailShot') : t('storyChat.detailText')}
        <textarea
          rows={4}
          value={text}
          onChange={(event) => setText(event.target.value)}
          onBlur={commit}
        />
      </label>
      {shot && (
        <fieldset>
          <legend>{t('storyChat.detailRefs')}</legend>
          <div className="sc-refs">
            {profiles.map((profile) => {
              const on = refs.includes(profile.id);
              return (
                <button
                  key={profile.id}
                  type="button"
                  aria-pressed={on}
                  onClick={() =>
                    onEdit(card.id, {
                      refs: on ? refs.filter((id) => id !== profile.id) : [...refs, profile.id],
                    })
                  }
                >
                  <Thumb card={profile} />
                  {profile.name}
                </button>
              );
            })}
          </div>
        </fieldset>
      )}
      <div className="sc-detail-actions">
        {shot ? (
          <button
            className="sc-primary keep-label"
            disabled={busy || card.status === 'generating'}
            onClick={() => {
              commit();
              onShot(card);
            }}
          >
            <Film size={15} aria-hidden="true" />
            {card.status === 'ready' ? t('storyChat.remakeShot') : t('storyChat.makeShot')}
          </button>
        ) : (
          <button
            className="sc-primary keep-label"
            disabled={busy}
            onClick={() => {
              commit();
              onPicture(card);
            }}
          >
            <ImagePlus size={15} aria-hidden="true" />
            {card.image ? t('storyChat.redrawPicture') : t('storyChat.makePicture')}
          </button>
        )}
        <button
          className="sc-ghost keep-label"
          onClick={() => {
            commit();
            onAsk(card);
          }}
        >
          <MessageSquare size={15} aria-hidden="true" />
          {t('storyChat.askAbout')}
        </button>
      </div>
    </aside>
  );
}

function Board({ state, fresh, openId, onOpen, onWorks, notice }) {
  const { t } = useI18n();
  const groups = cardsByKind(state);
  const done = progress(state);
  const profiles = Object.fromEntries(state.cards.map((card) => [card.id, card]));
  if (!state.cards.length)
    return (
      <div className="sc-board-empty">
        <Sparkles size={22} aria-hidden="true" />
        <p>{t('storyChat.boardEmpty')}</p>
      </div>
    );
  return (
    <>
      <div className="sc-progress">
        <span>{t('storyChat.progressCast', { ...done, sets: done.places + done.props })}</span>
        {done.shots > 0 && <span>{t('storyChat.progressShots', done)}</span>}
        {done.shots > 0 && (
          <span className="sc-meter" style={{ '--made': done.generated / done.shots }}>
            {t('storyChat.progressMade', done)}
          </span>
        )}
        <button
          className={'keep-label ' + (done.done ? 'sc-primary' : 'sc-ghost')}
          disabled={!done.shots}
          onClick={onWorks}
        >
          <Film size={15} aria-hidden="true" />
          {t('storyChat.openWorks')}
        </button>
      </div>
      {notice && (
        <p className="sc-notice" role="status">
          {notice}
        </p>
      )}
      {CARD_GROUPS.filter((group) => group.kind !== 'shot').map((group) =>
        groups[group.kind].length ? (
          <section key={group.kind} className="sc-group">
            <h3>{t('storyChat.' + group.key)}</h3>
            <div className="sc-tiles">
              {groups[group.kind].map((card) => (
                <button
                  key={card.id}
                  className={
                    'sc-tile' +
                    (fresh.includes(card.id) ? ' fresh' : '') +
                    (openId === card.id ? ' open' : '')
                  }
                  onClick={() => onOpen(card.id)}
                >
                  <Thumb card={card} />
                  <strong>{card.name}</strong>
                  <small>{card.text}</small>
                </button>
              ))}
            </div>
          </section>
        ) : null,
      )}
      {groups.shot.length > 0 && (
        <section className="sc-group">
          <h3>{t('storyChat.shots')}</h3>
          <ol className="sc-shots">
            {groups.shot.map((card) => (
              <li key={card.id}>
                <button
                  className={
                    'sc-shot' +
                    (fresh.includes(card.id) ? ' fresh' : '') +
                    (openId === card.id ? ' open' : '')
                  }
                  onClick={() => onOpen(card.id)}
                >
                  <Thumb card={card} wide />
                  <span className="sc-shot-body">
                    <span className="sc-shot-meta">
                      <strong>{t('storyChat.shotN', { n: card.order })}</strong>
                      <small>{t('storyChat.time', { start: card.start, end: card.end })}</small>
                      <small className={'sc-status ' + (card.status || 'draft')}>
                        {t('storyChat.status.' + (card.status || 'draft'))}
                      </small>
                    </span>
                    <span className="sc-shot-text">{card.text}</span>
                    <span className="sc-shot-refs">
                      {(card.refs || [])
                        .map((id) => profiles[id])
                        .filter(Boolean)
                        .map((profile) => (
                          <span key={profile.id}>{profile.name}</span>
                        ))}
                    </span>
                  </span>
                </button>
              </li>
            ))}
          </ol>
        </section>
      )}
    </>
  );
}

export default function StoryChat({ account }) {
  const { t, locale } = useI18n();
  const [state, dispatch] = useReducer(reducer, null, () => emptyChat());
  const [balance, setBalance] = useState(0);
  const [text, setText] = useState('');
  const [files, setFiles] = useState([]);
  const [tab, setTab] = useState('chat');
  const [openId, setOpenId] = useState(null);
  const [fresh, setFresh] = useState([]);
  const [notice, setNotice] = useState('');
  const chat = useRef(null);
  const thread = useRef(null);
  const input = useRef(null);
  const fileInput = useRef(null);

  // The preview stands in for the story-chat server until it exists (#422).
  useEffect(() => {
    const preview = createPreviewChat({ locale });
    chat.current = preview;
    setBalance(preview.balance);
    dispatch({ type: 'reset' });
    return () => preview.close();
  }, [locale]);

  const emit = useMemo(() => {
    let clear = null;
    return (event) => {
      dispatch({ type: 'event', event });
      if (event.type === 'project.changed') {
        setFresh((event.changes || []).map((change) => change.card?.id).filter(Boolean));
        clearTimeout(clear);
        clear = setTimeout(() => setFresh([]), 1600);
      }
    };
  }, []);

  useEffect(() => {
    const node = thread.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [state.messages.length, state.busy]);

  const send = (value = text) => {
    const message = value.trim();
    if ((!message && !files.length) || state.busy) return;
    chat.current.send({ text: message, attachments: files }, emit);
    setText('');
    setFiles([]);
    setTab('chat');
  };
  const settle = (quote, accepted) => {
    if (accepted) setBalance((value) => value - quote.credits);
    chat.current.settle(quote, accepted, state.cards, emit);
  };
  const edit = (id, patch) => {
    chat.current.edited();
    dispatch({ type: 'edit', id, patch });
  };
  const ask = (card) => {
    setOpenId(null);
    setTab('chat');
    setText(t('storyChat.askPrefix', { name: cardName(card, t) }));
    setTimeout(() => input.current?.focus(), 0);
  };
  const addFiles = (list) =>
    setFiles((current) => [
      ...current,
      ...[...list]
        .filter((file) => file.type.startsWith('image/'))
        .map((file) => ({ name: file.name, url: URL.createObjectURL(file) })),
    ]);

  const lastAssistant = [...state.messages].reverse().find((item) => item.role === 'assistant');
  const open = state.cards.find((card) => card.id === openId);
  const profiles = state.cards.filter((card) => card.kind !== 'shot');
  const done = progress(state);
  const starters = ['storyChat.starter1', 'storyChat.starter2', 'storyChat.starter3'];

  return (
    <div className="home sc-shell">
      <StudioHeader account={account} active="quick" />
      <p className="sc-preview-bar" role="status">
        {t('storyChat.preview')}
      </p>
      <div className="sc-tabs" role="tablist">
        <button role="tab" aria-selected={tab === 'chat'} onClick={() => setTab('chat')}>
          {t('storyChat.tabChat')}
        </button>
        <button role="tab" aria-selected={tab === 'cards'} onClick={() => setTab('cards')}>
          {t('storyChat.tabCards', { count: state.cards.length })}
        </button>
      </div>
      <main className={'sc tab-' + tab}>
        <section className="sc-chat" aria-label={t('storyChat.tabChat')}>
          <div className="sc-thread" ref={thread}>
            {!state.messages.length && (
              <div className="sc-welcome">
                <h1>{t('storyChat.welcome')}</h1>
                <p>{t('storyChat.welcomeHelp')}</p>
                <div className="sc-starters">
                  {starters.map((key) => (
                    <button key={key} onClick={() => send(t(key))}>
                      {t(key)}
                    </button>
                  ))}
                </div>
              </div>
            )}
            {state.messages.map((message) => {
              if (message.role === 'user')
                return (
                  <div key={message.id} className="sc-msg user">
                    {message.attachments.length > 0 && (
                      <div className="sc-attached">
                        {message.attachments.map((file) => (
                          <img key={file.url} src={file.url} alt={file.name} />
                        ))}
                      </div>
                    )}
                    {message.text && <p>{message.text}</p>}
                  </div>
                );
              if (message.role === 'change')
                return (
                  <button
                    key={message.id}
                    className="sc-change keep-label"
                    onClick={() => setTab('cards')}
                  >
                    <Check size={13} aria-hidden="true" />
                    {t('storyChat.change.' + message.summary.kind, message.summary)}
                  </button>
                );
              if (message.role === 'quote')
                return (
                  <QuoteCard
                    key={message.id}
                    quote={state.quotes[message.quoteId]}
                    balance={balance}
                    onSettle={settle}
                  />
                );
              return (
                <div key={message.id} className="sc-msg assistant">
                  <p>{message.text}</p>
                  {message === lastAssistant && !state.busy && message.chips.length > 0 && (
                    <div className="sc-chips">
                      {message.chips.map((chip) => (
                        <button key={chip} onClick={() => send(chip)}>
                          {chip}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
            {state.busy && (
              <div className="sc-typing" aria-live="polite">
                <span />
                <span />
                <span />
                <small>{t('storyChat.thinking')}</small>
              </div>
            )}
            {done.done && (
              <div className="sc-handoff">
                <Film size={18} aria-hidden="true" />
                <span>
                  <strong>{t('storyChat.handoffTitle')}</strong>
                  <small>{t('storyChat.handoffHelp')}</small>
                </span>
                <button
                  className="sc-primary keep-label"
                  onClick={() => setNotice(t('storyChat.previewWorks'))}
                >
                  {t('storyChat.openWorks')}
                </button>
              </div>
            )}
          </div>
          <form
            className="sc-composer"
            onSubmit={(event) => {
              event.preventDefault();
              send();
            }}
            onDragOver={(event) => event.preventDefault()}
            onDrop={(event) => {
              event.preventDefault();
              addFiles(event.dataTransfer.files);
            }}
          >
            {files.length > 0 && (
              <div className="sc-pending">
                {files.map((file) => (
                  <span key={file.url}>
                    <img src={file.url} alt={file.name} />
                    <button
                      type="button"
                      aria-label={t('storyChat.close')}
                      onClick={() => setFiles((current) => current.filter((item) => item !== file))}
                    >
                      <X size={12} />
                    </button>
                  </span>
                ))}
              </div>
            )}
            <div className="sc-compose-row">
              <button
                type="button"
                className="sc-icon"
                aria-label={t('storyChat.attach')}
                title={t('storyChat.attach')}
                onClick={() => fileInput.current?.click()}
              >
                <Paperclip size={17} />
              </button>
              <input
                ref={fileInput}
                type="file"
                accept="image/*"
                multiple
                hidden
                onChange={(event) => {
                  addFiles(event.target.files);
                  event.target.value = '';
                }}
              />
              <textarea
                ref={input}
                rows={1}
                value={text}
                placeholder={t('storyChat.placeholder')}
                onChange={(event) => setText(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                    event.preventDefault();
                    send();
                  }
                }}
              />
              <button
                className="sc-send"
                aria-label={t('storyChat.send')}
                disabled={state.busy || (!text.trim() && !files.length)}
              >
                <ArrowUp size={17} />
              </button>
            </div>
          </form>
        </section>
        <section
          className="sc-board"
          aria-label={t('storyChat.tabCards', { count: state.cards.length })}
        >
          <header className="sc-board-head">
            <h2>{state.title || t('storyChat.untitled')}</h2>
          </header>
          <div className="sc-board-body">
            <Board
              state={state}
              fresh={fresh}
              openId={openId}
              onOpen={setOpenId}
              onWorks={() => setNotice(t('storyChat.previewWorks'))}
              notice={notice}
            />
          </div>
          {open && (
            <CardDetail
              card={open}
              profiles={profiles}
              busy={state.busy}
              onEdit={edit}
              onPicture={(card) => {
                setOpenId(null);
                setTab('chat');
                chat.current.propose({ kind: 'image', cards: [card.id] }, card, emit);
              }}
              onShot={(card) => {
                setOpenId(null);
                setTab('chat');
                chat.current.propose({ kind: 'generate', cards: [card.id] }, card, emit);
              }}
              onAsk={ask}
              onClose={() => setOpenId(null)}
            />
          )}
        </section>
      </main>
    </div>
  );
}
