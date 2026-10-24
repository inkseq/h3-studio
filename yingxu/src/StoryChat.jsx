import React, { useEffect, useMemo, useReducer, useRef, useState } from 'react';
import { FloatingMessage } from './floating-notice.js';
import {
  ArrowUp,
  Check,
  Film,
  ImagePlus,
  MessageSquare,
  PanelLeftClose,
  PanelLeftOpen,
  PanelRightClose,
  PanelRightOpen,
  Paperclip,
  Plus,
  Sparkles,
  Undo2,
  X,
} from 'lucide-react';
import StudioHeader from './StudioHeader.jsx';
import ItemMenu from './ItemMenu.jsx';
import { StoryboardSplitter, useStoryboardPanelSize } from './StoryboardPanels.jsx';
import { RenameDialog } from './UniversalTrash.jsx';
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
import { createLiveChat } from './story-chat-client.js';
import { createCloudClient } from './cloud-client.js';
import { agentLinkSearch } from './agent-navigation.js';
import './story-chat.css';
import { track } from './analytics.js';

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
  if (quote.action?.kind === 'new_story')
    return (
      <div className={'sc-quote ' + quote.status}>
        <div className="sc-quote-head">
          <Plus size={15} aria-hidden="true" />
          <strong>{quote.label}</strong>
        </div>
        {quote.status === 'open' ? (
          <div className="sc-quote-actions">
            <small>{t('storyChat.newStoryHelp')}</small>
            <button className="sc-primary keep-label" onClick={() => onSettle(quote, true)}>
              {t('storyChat.newStoryStart')}
            </button>
            <button className="sc-ghost keep-label" onClick={() => onSettle(quote, false)}>
              {t('storyChat.newStoryStay')}
            </button>
          </div>
        ) : (
          <small className="sc-quote-done">
            {quote.status === 'confirmed' ? t('storyChat.newStoryOpened') : t('storyChat.declined')}
          </small>
        )}
      </div>
    );
  if (quote.live)
    return (
      <div className={'sc-quote ' + quote.status}>
        <div className="sc-quote-head">
          <Film size={15} aria-hidden="true" />
          <strong>{quote.label}</strong>
        </div>
        {quote.status === 'open' ? (
          <div className="sc-quote-actions">
            <small>{t('storyChat.liveQuoteHelp')}</small>
            <button className="sc-primary keep-label" onClick={() => onSettle(quote, true)}>
              {t('storyChat.makeInWorks')}
            </button>
            <button className="sc-ghost keep-label" onClick={() => onSettle(quote, false)}>
              {t('storyChat.notNow')}
            </button>
          </div>
        ) : (
          <small className="sc-quote-done">
            {quote.status === 'confirmed' ? t('storyChat.openedWorks') : t('storyChat.declined')}
          </small>
        )}
      </div>
    );
  return (
    <div className={'sc-quote ' + quote.status}>
      <div className="sc-quote-head">
        <Sparkles size={15} aria-hidden="true" />
        <strong>{quote.label}</strong>
        {Number.isFinite(quote.credits) && (
          <span className="sc-credits">
            {t('storyChat.quoteCredits', { credits: quote.credits })}
          </span>
        )}
      </div>
      {quote.status === 'open' ? (
        <div className="sc-quote-actions">
          {Number.isFinite(balance) && <small>{t('storyChat.quoteBalance', { balance })}</small>}
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

function CardDetail({
  card,
  profiles,
  busy,
  canDraw = true,
  onEdit,
  onPicture,
  onShot,
  onAsk,
  onClose,
}) {
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
        ) : canDraw ? (
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
        ) : null}
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
      <FloatingMessage tone="info" text={notice ? notice : null} />
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

// "14:05" today, otherwise a short date, in the viewer's language.
function whenText(seconds, locale) {
  const value = Number(seconds) * (Number(seconds) < 1e12 ? 1000 : 1);
  if (!Number.isFinite(value)) return '';
  const date = new Date(value);
  const today = date.toDateString() === new Date().toDateString();
  return new Intl.DateTimeFormat(locale === 'zh' ? 'zh-CN' : 'en', {
    ...(today ? { hour: 'numeric', minute: '2-digit' } : { month: 'short', day: 'numeric' }),
  }).format(date);
}

const openInWorks = (projectId, entityId) => {
  const search = agentLinkSearch({ projectId, entityId: entityId || null });
  if (search) navigateApp('/' + search);
};

// mode 'preview' uses sample replies; 'live' talks to /v1/story-chats (#422).
// A remembered on/off switch for a panel; private browsing or full storage just forgets.
function useRemembered(key, initial) {
  const [value, setValue] = useState(() => {
    try {
      const raw = localStorage.getItem(key);
      return raw === null ? initial : raw === '1';
    } catch {
      return initial;
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(key, value ? '1' : '0');
    } catch {}
  }, [key, value]);
  return [value, setValue];
}

export default function StoryChat({ account, mode = 'preview', projectId = null }) {
  const { t, locale } = useI18n();
  const live = mode === 'live';
  const api = useMemo(() => {
    const client = createCloudClient();
    client.setAccount(account);
    return client;
  }, [account]);
  const [unavailable, setUnavailable] = useState('');
  // Admin switch (#466): card pictures only when the server says they are on.
  const [picturesOn, setPicturesOn] = useState(!live);
  // The story this chat edits: the address's id at first, then whatever the client created.
  const story = useRef(projectId);
  const [session, setSession] = useState(0);
  const pending = useRef(null);
  const [stories, setStories] = useState(null);
  const refreshStories = () =>
    live &&
    api
      .request('/v1/story-chats')
      .then((value) => setStories(value.story_chats || []))
      .catch(() => setStories(null));
  // Open another story (or a new one, optionally starting with an idea) without a reload.
  const openStory = (ident, firstMessage = null) => {
    story.current = ident;
    pending.current = firstMessage;
    setOpenId(null);
    setTab('chat');
    try {
      window.history.pushState(window.history.state, '', '/quick-chat?story=' + (ident || 'new'));
    } catch {
      // A blocked history update only loses the back/reload shortcut.
    }
    setSession((value) => value + 1);
  };
  useEffect(() => {
    if (!live) return undefined;
    const back = () => {
      const ident = new URLSearchParams(window.location.search).get('story');
      if (window.location.pathname !== '/quick-chat' || !ident || ident === story.current) return;
      story.current = ident === 'new' ? null : ident;
      setSession((value) => value + 1);
    };
    window.addEventListener('popstate', back);
    return () => window.removeEventListener('popstate', back);
  }, [live]);
  const [state, dispatch] = useReducer(reducer, null, () => emptyChat());
  const [balance, setBalance] = useState(live ? Number.NaN : 0);
  const [text, setText] = useState('');
  const [files, setFiles] = useState([]);
  const [tab, setTab] = useState('chat');
  const [openId, setOpenId] = useState(null);
  // Desktop layout (owner, 2026-10-24): drag the story list and the card panel wider or
  // narrower, or fold either away; phones keep the tabs.
  const sideSize = useStoryboardPanelSize('story-chat-stories', 220, 160, 380),
    boardSize = useStoryboardPanelSize('story-chat-board', 620, 320, 1200);
  const [sideOpen, setSideOpen] = useRemembered('inkseq.storyChat.sideOpen', true),
    [boardOpen, setBoardOpen] = useRemembered('inkseq.storyChat.boardOpen', true);
  const [renaming, setRenaming] = useState(null);
  // A chat is one Works story; renaming renames the story, removing only takes the chat
  // out of this list (the story stays in Works and comes back when continued there).
  const updateChat = async (ident, body) => {
    await api.request(`/v1/story-chats/${encodeURIComponent(ident)}`, { method: 'PATCH', body });
    refreshStories();
    if (body.hidden && ident === story.current) openStory(null);
  };
  const [fresh, setFresh] = useState([]);
  const [notice, setNotice] = useState('');
  const chat = useRef(null);
  const thread = useRef(null);
  const input = useRef(null);
  const fileInput = useRef(null);

  // Preview: sample replies. Live: the story-chat server, once it says it is ready.
  useEffect(() => {
    dispatch({ type: 'reset' });
    setUnavailable('');
    if (!live) {
      const preview = createPreviewChat({ locale });
      chat.current = preview;
      setBalance(preview.balance);
      return () => preview.close();
    }
    let alive = true;
    const client = createLiveChat({
      api,
      language: locale === 'zh' ? 'zh' : 'en',
      projectId: story.current,
      labels: {
        generate: (count) => t('storyChat.liveQuote', { count }),
        failed: (failure) => {
          const code = failure.detail?.detail?.code;
          if (code === 'image_billing_unavailable') return t('storyChat.pictureUnavailable');
          if (code === 'image_not_returned') return t('storyChat.pictureRefused');
          return t('storyChat.failed', { reason: failure.message });
        },
        openWorks: openInWorks,
        picture: (name) => t('storyChat.livePicture', { name }),
        newStory: (idea) => t('storyChat.newStoryLabel', { idea }),
        startStory: (idea) => openStory(null, idea),
      },
      // Keep the address on this story, so a reload reopens it.
      onProject: (ident) => {
        story.current = ident;
        void refreshStories();
        try {
          window.history.replaceState(window.history.state, '', '/quick-chat?story=' + ident);
        } catch {
          // A blocked history update only loses the reload shortcut.
        }
      },
    });
    chat.current = client;
    api
      .request('/v1/story-chats/config')
      .then((config) => {
        if (!alive) return;
        if (Number.isSafeInteger(config.image_credits)) client.imageCredits = config.image_credits;
        setPicturesOn(config.pictures_enabled === true);
        // The confirm card shows the real balance, or none if it cannot be read.
        api
          .request('/v1/credits')
          .then((value) => alive && setBalance(Number(value.available)))
          .catch(() => alive && setBalance(Number.NaN));
        client.picturesEnabled = config.pictures_enabled === true;
        if (!config.enabled || !config.assistant_ready) setUnavailable(t('storyChat.notReady'));
        else {
          void refreshStories();
          if (story.current) return client.open(emit);
          if (pending.current) {
            const idea = pending.current;
            pending.current = null;
            return client.send({ text: idea }, emit);
          }
        }
      })
      .catch((failure) => alive && setUnavailable(failure.message));
    return () => {
      alive = false;
    };
  }, [live, locale, api, session]);

  const emit = useMemo(() => {
    let clear = null;
    return (event) => {
      dispatch({ type: 'event', event });
      if (event.type === 'chat.loaded' && event.fresh?.length) {
        setFresh(event.fresh);
        clearTimeout(clear);
        clear = setTimeout(() => setFresh([]), 1600);
      }
      if (event.type === 'project.changed') {
        setFresh((event.changes || []).map((change) => change.card?.id).filter(Boolean));
        clearTimeout(clear);
        clear = setTimeout(() => setFresh([]), 1600);
      }
    };
  }, []);

  // An in-app link to another story (?story=<id>) switches to it; the chat's own address
  // update after creating a story names the same story and changes nothing.
  useEffect(() => {
    if (live && projectId && projectId !== story.current) {
      story.current = projectId;
      setSession((value) => value + 1);
    }
  }, [live, projectId]);

  // A new title (the chat names the story) updates the list.
  useEffect(() => {
    if (live && state.title) void refreshStories();
  }, [live, state.title]);

  useEffect(() => {
    const node = thread.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [state.messages.length, state.busy]);

  const send = (value = text) => {
    const message = value.trim();
    if ((!message && !files.length) || state.busy || unavailable) return;
    chat.current.send({ text: message, attachments: files }, emit);
    track('story_chat_message', { live, attachments: files.length });
    setText('');
    setFiles([]);
    setTab('chat');
  };
  const settle = (quote, accepted) => {
    if (accepted) setBalance((value) => value - quote.credits);
    chat.current.settle(quote, accepted, state.cards, emit);
  };
  const edit = (id, patch) => {
    if (live) {
      void chat.current.edit(id, patch, emit);
      return;
    }
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

  const works = () =>
    live && chat.current?.projectId
      ? openInWorks(chat.current.projectId)
      : setNotice(t('storyChat.previewWorks'));
  // A confirm card already offers the next step; the model's chips would only repeat it.
  const proposalShown = state.messages.at(-1)?.role === 'quote';
  const lastAssistant = [...state.messages].reverse().find((item) => item.role === 'assistant');
  const open = state.cards.find((card) => card.id === openId);
  const profiles = state.cards.filter((card) => card.kind !== 'shot');
  const done = progress(state);
  const starters = ['storyChat.starter1', 'storyChat.starter2', 'storyChat.starter3'];

  return (
    <div className="home sc-shell">
      <StudioHeader account={account} active="quick" />
      {!live && (
        <p className="sc-preview-bar" role="status">
          {t('storyChat.preview')}
        </p>
      )}
      {unavailable && (
        <p className="sc-preview-bar" role="alert">
          {unavailable}
        </p>
      )}
      <div className="sc-tabs" role="tablist">
        {stories && (
          <button role="tab" aria-selected={tab === 'stories'} onClick={() => setTab('stories')}>
            {t('storyChat.tabStories')}
          </button>
        )}
        <button role="tab" aria-selected={tab === 'chat'} onClick={() => setTab('chat')}>
          {t('storyChat.tabChat')}
        </button>
        <button role="tab" aria-selected={tab === 'cards'} onClick={() => setTab('cards')}>
          {t('storyChat.tabCards', { count: state.cards.length })}
        </button>
      </div>
      <main
        className={
          'sc tab-' +
          tab +
          (stories ? ' with-stories' : '') +
          (sideOpen ? '' : ' side-closed') +
          (boardOpen ? '' : ' board-closed')
        }
        style={{ '--sc-side': sideSize.size + 'px', '--sc-board': boardSize.size + 'px' }}
      >
        {stories && (
          <nav className="sc-stories" aria-label={t('storyChat.myStories')}>
            <button className="sc-new-story keep-label" onClick={() => openStory(null)}>
              <Plus size={15} aria-hidden="true" />
              {t('storyChat.newStory')}
            </button>
            <h3>{t('storyChat.myStories')}</h3>
            {stories.length === 0 && <p className="sc-stories-empty">{t('storyChat.noStories')}</p>}
            <ul>
              {(stories || []).map((item) => (
                <li key={item.project_id}>
                  <button
                    aria-current={item.project_id === story.current ? 'page' : undefined}
                    onClick={() => item.project_id !== story.current && openStory(item.project_id)}
                  >
                    <span>{item.title}</span>
                    <small>{whenText(item.updated_at, locale)}</small>
                  </button>
                  <ItemMenu label={t('storyChat.chatMenu')}>
                    {(close) => (
                      <>
                        <button
                          role="menuitem"
                          onClick={() => {
                            close();
                            setRenaming(item);
                          }}
                        >
                          {t('storyChat.renameChat')}
                        </button>
                        <button
                          role="menuitem"
                          title={t('storyChat.removeChatHelp')}
                          onClick={() => {
                            close();
                            void updateChat(item.project_id, { hidden: true }).catch(() => {});
                          }}
                        >
                          {t('storyChat.removeChat')}
                        </button>
                      </>
                    )}
                  </ItemMenu>
                </li>
              ))}
            </ul>
            {renaming && (
              <RenameDialog
                title={renaming.title}
                onSave={(title) => updateChat(renaming.project_id, { title: title.trim() })}
                onClose={() => setRenaming(null)}
              />
            )}
          </nav>
        )}
        {stories && sideOpen && (
          <StoryboardSplitter
            panel={sideSize}
            className="sc-split"
            label={t('storyChat.resizeStories')}
            title={t('storyChat.resizeHelp')}
          />
        )}
        <section className="sc-chat" aria-label={t('storyChat.tabChat')}>
          <div className="sc-panel-toggles">
            {stories && (
              <button
                type="button"
                className="sc-icon"
                aria-label={t(sideOpen ? 'storyChat.hideStories' : 'storyChat.showStories')}
                title={t(sideOpen ? 'storyChat.hideStories' : 'storyChat.showStories')}
                onClick={() => setSideOpen((v) => !v)}
              >
                {sideOpen ? <PanelLeftClose size={16} /> : <PanelLeftOpen size={16} />}
              </button>
            )}
            <button
              type="button"
              className="sc-icon"
              aria-label={t(boardOpen ? 'storyChat.hideCards' : 'storyChat.showCards')}
              title={t(boardOpen ? 'storyChat.hideCards' : 'storyChat.showCards')}
              onClick={() => setBoardOpen((v) => !v)}
            >
              {boardOpen ? <PanelRightClose size={16} /> : <PanelRightOpen size={16} />}
            </button>
          </div>
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
                  {live &&
                    message === lastAssistant &&
                    !state.busy &&
                    state.undoTurnId &&
                    message.turnId === state.undoTurnId && (
                      <button
                        type="button"
                        className="sc-undo linkish"
                        onClick={() =>
                          void chat.current.undo(state.undoTurnId, emit).then((ok) => {
                            setNotice(t(ok ? 'storyChat.undone' : 'storyChat.undoStale'));
                            refreshStories();
                          })
                        }
                      >
                        <Undo2 size={13} aria-hidden="true" />
                        {t('storyChat.undo')}
                      </button>
                    )}
                  {message === lastAssistant &&
                    !state.busy &&
                    !proposalShown &&
                    message.chips.length > 0 && (
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
                <button className="sc-primary keep-label" onClick={works}>
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
                hidden={live}
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
        {boardOpen && (
          <StoryboardSplitter
            panel={boardSize}
            sign={-1}
            className="sc-split"
            label={t('storyChat.resizeCards')}
            title={t('storyChat.resizeHelp')}
          />
        )}
        <section
          className="sc-board"
          aria-label={t('storyChat.tabCards', { count: state.cards.length })}
        >
          <header className="sc-board-head">
            {live && chat.current?.projectId ? (
              <input
                key={state.title}
                className="sc-title-input"
                aria-label={t('storyChat.renameStory')}
                title={t('storyChat.renameStory')}
                defaultValue={state.title || t('storyChat.untitled')}
                maxLength={160}
                onKeyDown={(event) => event.key === 'Enter' && event.currentTarget.blur()}
                onBlur={(event) => {
                  void chat.current.rename(event.currentTarget.value, emit).then(refreshStories);
                }}
              />
            ) : (
              <h2>{state.title || t('storyChat.untitled')}</h2>
            )}
          </header>
          <div className="sc-board-body">
            <Board
              state={state}
              fresh={fresh}
              openId={openId}
              onOpen={setOpenId}
              onWorks={works}
              notice={notice}
            />
          </div>
          {open && (
            <CardDetail
              card={open}
              profiles={profiles}
              busy={state.busy}
              canDraw={picturesOn}
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
