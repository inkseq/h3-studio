import { useI18n } from './i18n/I18nProvider.jsx';
import { FloatingMessage } from './floating-notice.js';
import React, { useRef, useState } from 'react';
import { Upload, Image, Video, Music } from 'lucide-react';
import { store } from './store.js';
import { uploadFile } from './media.js';
import { uploadLibraryFiles } from './media-operations.js';
import { createUploadGate } from './workspace-context.js';
import './media-workspace.css';

export default function MediaLibraryUpload() {
  const { t: at } = useI18n();
  const types = [
    ['image', at('adjunct.upload0'), Image],
    ['video', at('adjunct.upload1'), Video],
    ['audio', at('adjunct.upload2'), Music],
  ];
  const input = useRef(),
    gate = useRef(createUploadGate()),
    [kind, setKind] = useState(''),
    [busy, setBusy] = useState(false),
    [dragging, setDragging] = useState(false),
    [error, setError] = useState(''),
    [message, setMessage] = useState('');
  async function accept(files) {
    if (!files.length) return;
    if (gate.current.busy) {
      setError(at('adjunct.mediaLibraryUpload.copy77'));
      return;
    }
    setError('');
    setMessage('');
    let completed = 0;
    await gate.current.run(async () => {
      setBusy(true);
      try {
        for (const file of files) {
          await uploadLibraryFiles({ store, files: [file], upload: uploadFile });
          completed++;
        }
        setMessage(at('adjunct.mediaLibraryUpload.copy78', { v0: completed }));
      } catch (error) {
        setError(
          `${completed ? at('adjunct.mediaLibraryUpload.copy79', { v0: completed }) : ''}${error.message}`,
        );
      } finally {
        setBusy(false);
        if (input.current) input.current.value = '';
      }
    });
  }
  const choose = (type) => {
    setKind(type);
    input.current.accept = type + '/*';
    input.current.click();
  };
  return (
    <section
      className={'media-library-upload ' + (dragging ? 'is-dragging' : '')}
      aria-label={at('adjunct.mediaLibraryUpload.copy80')}
      onDragOver={(event) => {
        event.preventDefault();
        if (!busy) setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={(event) => {
        event.preventDefault();
        setDragging(false);
        accept([...event.dataTransfer.files]);
      }}
    >
      <Upload size={25} />
      <div>
        <h3>
          {busy ? at('adjunct.mediaLibraryUpload.copy81') : at('adjunct.mediaLibraryUpload.copy82')}
        </h3>
        <p>{at('adjunct.mediaLibraryUpload.copy83')}</p>
        <div>
          {types.map(([type, label, Icon]) => (
            <button type="button" key={type} disabled={busy} onClick={() => choose(type)}>
              <Icon size={16} />
              {label}
            </button>
          ))}
        </div>
      </div>
      <input
        className="visually-hidden"
        type="file"
        ref={input}
        aria-label={
          kind
            ? types.find(([type]) => type === kind)?.[1]
            : at('adjunct.mediaLibraryUpload.copy84')
        }
        multiple
        onChange={(event) => accept([...event.target.files])}
      />
      <FloatingMessage tone="error" text={error ? error : null} />
      <FloatingMessage tone="info" text={message ? message : null} />
    </section>
  );
}
