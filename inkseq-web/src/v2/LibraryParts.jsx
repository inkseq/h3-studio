// Small in-place editors for the asset library (owner, 2026-10-22): rename, tag and file a
// picture where it is, instead of opening a dialog and coming back.
import React, { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { Button, IconButton } from '../ui/Button.jsx';
import { Popover, closePopover } from '../ui/Popover.jsx';
import { Check, Pencil, Plus, Upload, X } from 'lucide-react';
import { useI18n } from '../i18n/I18nProvider.jsx';

// Readable names for machine titles: "frame-5ee8…-time…" and bare "生成结果" say nothing.
export function displayName(row, t) {
  const story = row.source_project_title || row.project_title || '';
  const title = (row.title || '').replace(/\s*·\s*旧版 v\d+\s*/g, ' · ').replace(/\s*·\s*$/, '');
  if (/^frame-[0-9a-f]{6,}/i.test(title))
    return [t('library.frameName'), story].filter(Boolean).join(' · ');
  if (
    !title ||
    title === t('library.sourceGeneration') ||
    title === '生成结果' ||
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(title) ||
    /^[a-z]{1,8}-[0-9a-f]{8,}/i.test(title)
  )
    return [t('trash.kind.' + row.type), story].filter(Boolean).join(' · ');
  return title;
}

// Quick-create files live in a workspace the service manages; they are copied into a story
// before they can be renamed, tagged or used as a reference.
export const managedRow = (row) => row.target.project_id.startsWith('chat-workspace-');

/** Click the name to edit it; Enter or leaving the field saves, Escape cancels. */
export function InlineName({ value, onSave, disabled, className = '' }) {
  const { t } = useI18n();
  const [draft, setDraft] = useState(null);
  const commit = () => {
    const next = draft?.trim();
    setDraft(null);
    if (next && next !== value) onSave(next);
  };
  if (draft !== null)
    return (
      <input
        className={'lib-inline-name ' + className}
        autoFocus
        value={draft}
        maxLength={160}
        aria-label={t('library.name')}
        onChange={(e) => setDraft(e.target.value)}
        onFocus={(e) => e.target.select()}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === 'Enter') commit();
          if (e.key === 'Escape') {
            e.stopPropagation();
            setDraft(null);
          }
        }}
      />
    );
  return (
    <Button
      className={'lib-inline-label ' + className}
      title={t('library.renameHint')}
      disabled={disabled}
      onClick={(e) => {
        e.stopPropagation();
        setDraft(value);
      }}
    >
      <span className="clip">{value}</span>
      <Pencil size={12} aria-hidden="true" />
    </Button>
  );
}

/** Tags as chips: × removes one, typing and Enter adds; nothing replaces the whole set. */
export function TagEditor({ tags, suggestions = [], onChange, disabled }) {
  const { t } = useI18n();
  const [text, setText] = useState('');
  const add = () => {
    const extra = text
      .split(/[,，]/)
      .map((s) => s.trim())
      .filter(Boolean);
    setText('');
    if (extra.length) onChange([...new Set([...tags, ...extra])]);
  };
  const listId = useRef('lib-tags-' + Math.random().toString(36).slice(2)).current;
  return (
    <div className="lib-tag-editor">
      {tags.map((tag) => (
        <span key={tag} className="tagchip on">
          {tag}
          <IconButton
            size="xs"
            disabled={disabled}
            label={t('library.removeTag', { tag })}
            onClick={() => onChange(tags.filter((x) => x !== tag))}
          >
            <X size={11} />
          </IconButton>
        </span>
      ))}
      <input
        className="field-sm"
        list={listId}
        value={text}
        disabled={disabled}
        placeholder={t('library.addTag')}
        aria-label={t('library.addTag')}
        onChange={(e) => setText(e.target.value)}
        onBlur={add}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault();
            add();
          }
        }}
      />
      <datalist id={listId}>
        {suggestions
          .filter((s) => !tags.includes(s))
          .map((s) => (
            <option key={s} value={s} />
          ))}
      </datalist>
    </div>
  );
}

/** A floating panel opened from a button (ui/Popover). `look` is the trigger's look. */
export function PopMenu({
  label,
  className = '',
  look,
  align = 'start',
  disabled,
  title,
  onOpen,
  children,
}) {
  return (
    <Popover
      align={align}
      className={('lib-pop-panel ' + className).trim()}
      onOpenChange={(open) => open && onOpen?.()}
      trigger={
        <Button
          variant={look === 'primary' ? 'primary' : undefined}
          size={look === 'small' ? 'sm' : undefined}
          disabled={disabled}
          title={title}
        >
          {label}
        </Button>
      }
    >
      {children}
    </Popover>
  );
}

/** Pick a character/place to use this file as its reference, or create one on the spot. */
export function ReferencePicker({ items, loadProfiles, onFile, onCreate, disabled, label }) {
  const { t } = useI18n();
  const [profiles, setProfiles] = useState(null),
    [making, setMaking] = useState(''),
    [name, setName] = useState('');
  const projectIds = new Set(items.map((r) => r.target.project_id));
  const mixed = projectIds.size !== 1,
    audio = items.some((r) => r.type === 'audio');
  const types = audio ? ['character', 'location'] : ['character', 'location', 'prop'];
  const load = async () => {
    setProfiles(null);
    setMaking('');
    try {
      setProfiles((await loadProfiles([...projectIds][0])).filter((p) => types.includes(p.type)));
    } catch {
      setProfiles([]);
    }
  };
  return (
    <PopMenu
      label={label || t('library.setReference')}
      look="small"
      disabled={disabled || mixed || !items.length}
      title={
        mixed
          ? t('library.referenceOneStory')
          : items.some(managedRow)
            ? t('library.managedHint')
            : undefined
      }
      onOpen={load}
    >
      {profiles === null ? (
        <p className="muted">{t('library.loading')}</p>
      ) : (
        <>
          {profiles.length ? (
            <div className="lib-pop-list">
              {profiles.map((p) => (
                <Button
                  variant="ghost"
                  className="lib-pop-item"
                  key={p.target.id}
                  onClick={(e) => {
                    closePopover(e.currentTarget);
                    onFile(items, p);
                  }}
                >
                  <small className="muted">{t('trash.kind.' + p.type)}</small>
                  <span className="clip">{p.title}</span>
                </Button>
              ))}
            </div>
          ) : (
            <p className="muted">{t('library.referenceNone')}</p>
          )}
          <div className="lib-pop-new">
            {making ? (
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  if (!name.trim()) return;
                  closePopover(e.currentTarget);
                  onCreate(items, making, name.trim());
                  setName('');
                }}
              >
                <input
                  autoFocus
                  value={name}
                  maxLength={160}
                  placeholder={t('library.newName')}
                  aria-label={t('library.newName')}
                  onChange={(e) => setName(e.target.value)}
                />
                <Button variant="primary" size="sm" type="submit" disabled={!name.trim()}>
                  {t('library.referenceNew')}
                </Button>
              </form>
            ) : (
              types.map((type) => (
                <Button
                  variant="ghost"
                  size="sm"
                  key={type}
                  data-keep-open
                  onClick={() => setMaking(type)}
                >
                  <Plus size={13} />
                  {t('library.new.' + type)}
                </Button>
              ))
            )}
          </div>
        </>
      )}
    </PopMenu>
  );
}

/** "Upload" opens a small menu under the button: which story, then pick or drop files. */
export function UploadMenu({ projects, defaultStory, onFiles, disabled }) {
  const { t } = useI18n();
  const [story, setStory] = useState(defaultStory || ''),
    input = useRef(null);
  useEffect(() => {
    if (defaultStory) setStory(defaultStory);
  }, [defaultStory]);
  const target = story || projects[0]?.id || '';
  const send = (files, node) => {
    if (!files.length || !target) return;
    closePopover(node);
    onFiles(files, target);
  };
  return (
    <PopMenu
      className="lib-upload"
      align="end"
      look="primary"
      disabled={disabled}
      label={
        <>
          <Upload size={15} />
          {t('v2.upload')}
        </>
      }
    >
      {!projects.length ? (
        <p className="muted">{t('library.noStory')}</p>
      ) : (
        <>
          <label className="field">
            <span>{t('library.uploadTo')}</span>
            <select value={target} onChange={(e) => setStory(e.target.value)}>
              {projects.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.title}
                </option>
              ))}
            </select>
          </label>
          <div
            className="lib-drop"
            onDragOver={(e) => e.preventDefault()}
            onDrop={(e) => {
              e.preventDefault();
              e.stopPropagation();
              send([...e.dataTransfer.files], e.currentTarget);
            }}
          >
            <Upload size={20} aria-hidden="true" />
            <span>
              {t('library.uploadDrop')}{' '}
              <Button variant="link" data-keep-open onClick={() => input.current?.click()}>
                {t('library.uploadPick')}
              </Button>
            </span>
            <small className="muted">{t('library.uploadNote')}</small>
          </div>
          <input
            ref={input}
            className="visually-hidden"
            type="file"
            multiple
            accept="image/*,video/*,audio/*"
            onChange={(e) => {
              send([...e.target.files], e.target);
              e.target.value = '';
            }}
          />
        </>
      )}
    </PopMenu>
  );
}

/** Characters, places and props are created (a name), not uploaded. */
export function CreateProfileMenu({ type, projects, defaultStory, onCreate, disabled }) {
  const { t } = useI18n();
  const [name, setName] = useState(''),
    [story, setStory] = useState(defaultStory || '');
  const target = story || defaultStory || projects[0]?.id || '';
  return (
    <PopMenu
      className="lib-upload"
      align="end"
      look="primary"
      disabled={disabled}
      label={
        <>
          <Plus size={15} />
          {t('library.new.' + type)}
        </>
      }
    >
      {!projects.length ? (
        <p className="muted">{t('library.noStory')}</p>
      ) : (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (!name.trim() || !target) return;
            closePopover(e.currentTarget);
            onCreate(type, name.trim(), target);
            setName('');
          }}
        >
          <label className="field">
            <span>{t('library.newName')}</span>
            <input
              autoFocus
              value={name}
              maxLength={160}
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <label className="field">
            <span>{t('library.newIn')}</span>
            <select value={target} onChange={(e) => setStory(e.target.value)}>
              {projects.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.title}
                </option>
              ))}
            </select>
          </label>
          <Button variant="primary" type="submit" disabled={!name.trim()}>
            <Check size={14} />
            {t('library.create')}
          </Button>
        </form>
      )}
    </PopMenu>
  );
}

/** A panel that opens beside the card that was clicked and follows it while scrolling. */
export function FloatingPanel({ anchor, label, onClose, children }) {
  const { t } = useI18n();
  const ref = useRef(null),
    [pos, setPos] = useState(null);
  useLayoutEffect(() => {
    const place = () => {
      const el = ref.current;
      if (!el) return;
      const a = anchor?.()?.getBoundingClientRect(),
        w = el.offsetWidth,
        h = el.offsetHeight,
        vw = window.innerWidth,
        vh = window.innerHeight;
      if (!a) return setPos({ left: vw - w - 16, top: 72 });
      let left = a.right + 12;
      if (left + w > vw - 16) left = a.left - w - 12;
      if (left < 16) left = Math.max(16, Math.min(vw - w - 16, a.left));
      const top = Math.max(64, Math.min(a.top, vh - h - 16));
      setPos({ left, top });
    };
    place();
    window.addEventListener('resize', place);
    window.addEventListener('scroll', place, true);
    const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(place) : null;
    observer?.observe(ref.current);
    return () => {
      window.removeEventListener('resize', place);
      window.removeEventListener('scroll', place, true);
      observer?.disconnect();
    };
  }, [anchor]);
  useEffect(() => {
    const outside = (e) => {
      if (ref.current?.contains(e.target) || anchor?.()?.contains(e.target)) return;
      // Menus opened from the panel float outside it; let them close first.
      if (e.target.closest?.('.lib-pop, dialog, [role="dialog"]')) return;
      onClose();
    };
    const escape = (e) => {
      if (e.key === 'Escape' && !document.querySelector('details.lib-pop[open]')) onClose();
    };
    document.addEventListener('pointerdown', outside, true);
    window.addEventListener('keydown', escape);
    return () => {
      document.removeEventListener('pointerdown', outside, true);
      window.removeEventListener('keydown', escape);
    };
  }, [anchor, onClose]);
  return (
    <aside
      ref={ref}
      className="lib-float"
      aria-label={label}
      style={pos ? { left: pos.left, top: pos.top } : { visibility: 'hidden' }}
    >
      <Button className="lib-float-close" aria-label={t('trash.close')} onClick={onClose}>
        <X size={16} />
      </Button>
      {children}
    </aside>
  );
}
