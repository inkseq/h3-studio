// Quick Create composer attachments (h3-studio#726): pending files with their target card,
// and the files shown on a sent message. Upload and attach happen in the chat client.
import React, { useState } from 'react';
import { FileText, Film, Music, X } from 'lucide-react';
import { IconButton } from './ui/Button.jsx';
import { useI18n } from './i18n/I18nProvider.jsx';
import { cardTargets, fileKind, joinText } from './story-chat-attach.js';

const ICONS = { video: Film, audio: Music, text: FileText };

function Preview({ file }) {
  if (file.kind === 'image' && file.url) return <img src={file.url} alt={file.name} />;
  const Icon = ICONS[file.kind] || FileText;
  return (
    <span className="sc-file" title={file.name}>
      <Icon size={18} aria-hidden="true" />
      <small>{file.name}</small>
    </span>
  );
}

export function AttachedFiles({ files }) {
  if (!files?.length) return null;
  return (
    <div className="sc-attached">
      {files.map((file) => (
        <Preview key={file.url || file.name} file={file} />
      ))}
    </div>
  );
}

// Files waiting to be sent: media to upload, each photo with the card it goes on.
export function useAttachments({ live, maxChars, setText, onNotice }) {
  const { t } = useI18n();
  const [files, setFiles] = useState([]);
  const add = async (list) => {
    const media = [];
    for (const file of [...list]) {
      const kind = fileKind(file);
      if (kind === 'text') {
        const body = await file.text();
        setText((current) => {
          const joined = joinText(current, body, maxChars);
          if (joined.cut) onNotice(t('storyChat.textCut', { max: maxChars }));
          return joined.text;
        });
      } else if (kind && (live || kind === 'image'))
        media.push({ file, kind, name: file.name, url: URL.createObjectURL(file), cardId: '' });
      else onNotice(t('storyChat.fileUnsupported', { name: file.name }));
    }
    if (media.length) setFiles((current) => [...current, ...media]);
  };
  const update = (target, patch) =>
    setFiles((current) => current.map((item) => (item === target ? { ...item, ...patch } : item)));
  const remove = (target) => setFiles((current) => current.filter((item) => item !== target));
  return { files, add, update, remove, clear: () => setFiles([]) };
}

export function PendingFiles({ attachments, cards }) {
  const { t } = useI18n();
  if (!attachments.files.length) return null;
  return (
    <div className="sc-pending">
      {attachments.files.map((file) => {
        const targets = cardTargets(cards, file.kind);
        return (
          <span key={file.url} className="sc-pending-item">
            <Preview file={file} />
            <IconButton
              variant="on-media"
              size="xs"
              label={t('storyChat.close')}
              onClick={() => attachments.remove(file)}
            >
              <X size={12} />
            </IconButton>
            {targets.length > 0 && (
              <select
                className="sc-pending-target"
                aria-label={t('storyChat.attachTo', { name: file.name })}
                value={file.cardId}
                onChange={(event) => attachments.update(file, { cardId: event.target.value })}
              >
                <option value="">{t('storyChat.attachLibrary')}</option>
                {targets.map((card) => (
                  <option key={card.id} value={card.id}>
                    {card.name}
                  </option>
                ))}
              </select>
            )}
          </span>
        );
      })}
    </div>
  );
}
