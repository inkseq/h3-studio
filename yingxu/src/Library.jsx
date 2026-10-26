import React, { useEffect, useMemo, useRef, useState, useEffectEvent } from 'react';
import { Button } from './ui/Button.jsx';
import { Dialog } from './ui/Dialog.jsx';
import { useI18n } from './i18n/I18nProvider.jsx';
import { createCloudClient } from './cloud-client.js';
import { createLibraryController, libraryKey } from './library-commands.js';
import { useTrash } from './UniversalTrash.jsx';
import { store } from './store.js';
import { cloudController as cloud } from './cloud-controller.js';
import { uploadFile } from './media.js';
import { uploadLibraryFiles } from './media-operations.js';
import { applyStoryboardAction } from './storyboard-actions.js';
import LibraryView from './v2/LibraryView.jsx';
import {
  FloatingPanel,
  InlineName,
  ReferencePicker,
  TagEditor,
  displayName,
  managedRow,
} from './v2/LibraryParts.jsx';
import { useThumbnailSource } from './use-thumbnail-source.js';
const copy = (value) => structuredClone(value),
  link = (project, entity) =>
    '/?' + new URLSearchParams({ project, ...(entity ? { entity } : {}) });
export function LibraryMedia({ row, api, detail = false }) {
  const { t } = useI18n(),
    [failed, setFailed] = useState(''),
    [retry, setRetry] = useState({ url: '', count: 0 });
  const id = row.artifact_id || row.asset_id,
    kind = row.artifact_id ? 'artifacts' : 'assets';
  const imageRead = row.type === 'image' || (!detail && row.type === 'video');
  const original = id ? `/v1/${kind}/${encodeURIComponent(id)}/content` : null;
  // Older uploads have no thumbnail rendition (rendition_status "missing"); an image can
  // still show its original, lazily loaded. Videos without a thumbnail keep the notice.
  const path = imageRead
    ? detail
      ? row.preview_url || (row.type === 'image' ? original : null)
      : row.thumbnail_url || (row.type === 'image' ? original : null)
    : detail
      ? original
      : null;
  let url = '';
  try {
    if (path) url = api.nativeContentURL(path);
  } catch {
    // No native URL for this path; the preview falls back to its placeholder.
  }
  const thumbnail = useThumbnailSource(api, url, { enabled: !detail && imageRead }),
    src = detail ? url : thumbnail.src;
  const retries = retry.url === url ? retry.count : 0;
  const onError = () => {
    thumbnail.finish();
    // A busy media lane answers 429 when many thumbnails load at once; try the
    // same thumbnail twice more after a pause before showing the notice.
    if (!detail && imageRead && retries < 2)
      setTimeout(() => setRetry({ url, count: retries + 1 }), 3000 * (retries + 1));
    else setFailed(url);
  };
  if (!imageRead && !detail) return null;
  if (!url || failed === url)
    return (
      <span
        ref={thumbnail.ref}
        className={detail ? 'library-preview-error detail' : 'library-preview-error'}
      >
        {t('library.previewUnavailable')}
      </span>
    );
  if (imageRead)
    return src ? (
      <img
        key={retries}
        ref={thumbnail.ref}
        className="library-media"
        src={src}
        alt={row.title || ''}
        loading="lazy"
        decoding="async"
        onLoad={thumbnail.finish}
        onError={onError}
      />
    ) : (
      <span ref={thumbnail.ref} className="library-preview-error" aria-hidden="true" />
    );
  if (row.type === 'audio')
    return <audio className="library-media" src={url} controls preload="none" onError={onError} />;
  return (
    <video
      className="library-media"
      src={url}
      controls
      playsInline
      preload="metadata"
      onError={onError}
    />
  );
}
export default function Library({ account, active = true }) {
  const { t } = useI18n(),
    trash = useTrash(),
    identity = useRef(account);
  identity.current = account;
  useEffect(() => {
    if (active) document.title = t('library.title') + ' · Inkseq';
  }, [active, t]);
  const api = useMemo(() => {
    const c = createCloudClient();
    c.setAccount(account);
    return c;
  }, [account]);
  const controller = useMemo(
    () =>
      createLibraryController({
        api,
        account,
        getAccount: () => identity.current,
        storage: localStorage,
      }),
    [api, account],
  );
  const [filters, setFilters] = useState({
      project_id: '',
      type: 'file',
      work: '',
      tag: '',
      q: '',
      unused: '',
    }),
    [rows, setRows] = useState([]),
    [counts, setCounts] = useState(null),
    [next, setNext] = useState(null),
    [projects, setProjects] = useState([]),
    [selected, setSelected] = useState([]),
    [busy, setBusy] = useState(false),
    [loading, setLoading] = useState(false),
    [error, setError] = useState(''),
    [message, setMessage] = useState(''),
    [imported, setImported] = useState(null),
    [dialog, setDialog] = useState(null),
    [revision, setRevision] = useState(0),
    [mobile, setMobile] = useState(
      () =>
        typeof window !== 'undefined' &&
        typeof window.matchMedia === 'function' &&
        window.matchMedia('(max-width:760px)').matches,
    );
  const epoch = useRef(0),
    dialogEpoch = useRef(0),
    // Latest saved version per story, so a second quick edit does not reuse a stale one.
    versions = useRef({});
  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return;
    const q = window.matchMedia('(max-width:760px)'),
      update = () => setMobile(q.matches);
    q.addEventListener('change', update);
    return () => q.removeEventListener('change', update);
  }, []);
  // Effects can reconnect while the cached page keeps its hook state. Restore
  // the owner fence on setup as well as render; cleanup must retire only its owner.
  useEffect(() => {
    identity.current = account;
    versions.current = {};
    const dialogEpochRef = dialogEpoch;
    const epochRef = epoch;
    return () => {
      if (identity.current === account) identity.current = null;
      epochRef.current++;
      dialogEpochRef.current++;
    };
  }, [account]);
  let pending;
  try {
    pending = controller.pending();
  } catch {
    pending = {};
  }
  const countsScope = JSON.stringify([
    account,
    ...Object.entries(filters).filter(([key]) => key !== 'type'),
  ]);
  // A rejected request (4xx, e.g. an image that is too small) explains itself; show that.
  const errorText = (e) =>
    e?.message?.startsWith('library.')
      ? t(e.message)
      : e?.status >= 400 && e.status < 500 && e.status !== 409 && e.message
        ? e.message
        : t('library.failed');
  async function load(append = false) {
    const n = ++epoch.current,
      owner = account;
    setLoading(true);
    setError('');
    try {
      const data = await controller.list(filters, append ? next : 0);
      if (n !== epoch.current || identity.current !== owner) return;
      setRows((old) =>
        append
          ? [
              ...old,
              ...data.items.filter(
                (v) => !old.some((x) => libraryKey(x.target) === libraryKey(v.target)),
              ),
            ]
          : data.items,
      );
      setNext(data.next_offset);
      setCounts({ scope: countsScope, value: data.counts || null });
      if (!append) setSelected([]);
    } catch (e) {
      if (n === epoch.current && identity.current === owner) setError(errorText(e));
    } finally {
      if (n === epoch.current) setLoading(false);
    }
  }
  const errorTextEvent = useEffectEvent((...args) => errorText(...args));
  useEffect(() => {
    setRows([]);
    setCounts(null);
    setProjects([]);
    setSelected([]);
    setDialog(null);
    setError('');
    const owner = account;
    let live = true;
    if (account)
      (async () => {
        const all = [];
        let offset = 0;
        while (live) {
          const result = await api.projects({ limit: 100, offset });
          if (!live || identity.current !== owner) return;
          const page = result.projects || [];
          all.push(...page);
          if (page.length < 100) break;
          offset += page.length;
        }
        if (live) setProjects([...new Map(all.map((p) => [p.id, p])).values()]);
      })().catch((e) => {
        if (live && identity.current === owner) setError(errorTextEvent(e));
      });
    return () => {
      live = false;
    };
  }, [account, api]);
  const loadEvent = useEffectEvent((...args) => load(...args));
  useEffect(() => {
    if (account && active) void loadEvent();
    else setLoading(false);
    const epochRef = epoch;
    return () => {
      epochRef.current++;
    };
  }, [account, controller, active, filters, revision]);
  useEffect(() => {
    const changed = () => setRevision((v) => v + 1);
    window.addEventListener('inkseq-trash-changed', changed);
    return () => window.removeEventListener('inkseq-trash-changed', changed);
  }, []);
  const currentRows = rows.filter((row) => selected.includes(libraryKey(row.target))),
    locked = busy || !!pending || trash?.busy || !!trash?.pending;
  function guardDraft(projectId) {
    const s = store.getState();
    if (s.workspace.mode === 'cloud' && s.project.id === projectId && s.workspace.dirty)
      throw Error('library.localDraft');
    return s;
  }
  // `keep` leaves the open card panel in place (quick edits); otherwise a finished
  // action closes whatever dialog started it.
  async function run(fn, { keep = false } = {}) {
    if (busy) return;
    const owner = account;
    setBusy(true);
    setError('');
    setMessage('');
    setImported(null);
    try {
      const result = await fn();
      if (identity.current !== owner) return;
      setRevision((v) => v + 1);
      if (result?.state === 'blocked') setMessage(t('library.blocked'));
      else if (result?.state === 'pending') setMessage(t('library.pending'));
      else {
        if (result?.state === 'completed')
          setImported({
            project: result.target_project_id,
            entity: result.entity_ids?.[result.source?.id],
          });
        setMessage(
          result?.message ?? t(result?.state === 'completed' ? 'library.copied' : 'library.saved'),
        );
        if (!keep) {
          setDialog(null);
          dialogEpoch.current++;
        }
      }
      return result;
    } catch (e) {
      if (identity.current === owner) setError(errorText(e));
    } finally {
      if (identity.current === owner) setBusy(false);
    }
  }
  async function actions(items, ops) {
    const pid = items[0]?.target.project_id;
    if (!pid || items.some((x) => x.target.project_id !== pid)) throw Error('library.changed');
    const before = guardDraft(pid),
      expected = Math.max(...items.map((x) => x.project_version), versions.current[pid] || 0),
      body = { expected_version: expected, actions: ops };
    const result = await controller.actions(pid, body);
    versions.current[pid] = result.version;
    const live = store.getState();
    if (
      identity.current === account &&
      before.workspace.mode === 'cloud' &&
      before.project.id === pid &&
      live.workspaceEpoch === before.workspaceEpoch &&
      !live.workspace.dirty
    )
      store.applyCloudCommand(result.project, {
        account,
        expectedVersion: before.workspace.serverVersion,
        version: result.version,
        expectedProject: before.project,
      });
    return result;
  }
  // Bulk edits may span stories: one versioned request per story.
  async function actionsPerStory(items, opsFor) {
    const groups = new Map();
    for (const item of items)
      groups.set(item.target.project_id, [...(groups.get(item.target.project_id) || []), item]);
    let last;
    for (const group of groups.values()) last = await actions(group, opsFor(group));
    return last;
  }
  const ref = (row) => ({ kind: row.target.kind, id: row.target.id });
  // Uploads and new characters are edits of a story document: open it here (no navigation),
  // change it, and save it straight away, so nothing waits on a separate "save" button.
  async function editStory(projectId, change) {
    let s = store.getState();
    if (s.workspace.mode === 'cloud' && s.workspace.dirty) {
      await cloud.save();
      s = store.getState();
    }
    if (s.workspace.dirty) throw Error('library.localDraft');
    if (!(s.workspace.mode === 'cloud' && s.project.id === projectId)) await cloud.open(projectId);
    const receipt = await change();
    const record = await cloud.save();
    if (record?.version) versions.current[projectId] = record.version;
    return receipt;
  }
  const uploadFiles = (files, projectId) =>
    run(async () => {
      const added = await editStory(projectId, () =>
        uploadLibraryFiles({ store, files, upload: uploadFile }),
      );
      return { state: 'saved', message: t('library.uploaded', { count: added.length }) };
    });
  const newProfile = (ptype, name) => {
    let receipt;
    const ok = store.editProject((p) => {
      receipt = applyStoryboardAction(p, { type: 'newProfile', ptype, name });
      return receipt !== false;
    });
    if (!ok || !receipt?.id) throw Error('library.failed');
    return receipt.id;
  };
  const createProfile = (ptype, name, projectId) =>
    run(async () => {
      await editStory(projectId, () => newProfile(ptype, name));
      if (filters.type !== ptype) setFilters((f) => ({ ...f, type: ptype }));
      return { state: 'saved', message: t('library.created', { name }) };
    });
  const fileInto = (items, profile) =>
    run(
      async () => {
        await actions(items, [
          { op: 'library.file', profile_id: profile.target.id, targets: items.map(ref) },
        ]);
        setSelected([]);
        return { state: 'saved', message: t('library.referenceDone', { name: profile.title }) };
      },
      { keep: true },
    );
  const createReference = (items, ptype, name) =>
    run(
      async () => {
        const pid = items[0].target.project_id;
        const id = await editStory(pid, () => newProfile(ptype, name));
        await actions(items, [{ op: 'library.file', profile_id: id, targets: items.map(ref) }]);
        setSelected([]);
        return { state: 'saved', message: t('library.referenceDone', { name }) };
      },
      { keep: true },
    );
  const rename = (row, title) =>
    run(
      async () => {
        await actions([row], [{ op: 'library.rename', target: ref(row), title }]);
        setDialog((d) =>
          d?.kind === 'detail' && libraryKey(d.row.target) === libraryKey(row.target)
            ? { ...d, row: { ...d.row, title } }
            : d,
        );
        return { state: 'saved', message: '' };
      },
      { keep: true },
    );
  const setTags = (items, tagsFor) =>
    run(
      async () => {
        await actionsPerStory(items, (group) =>
          group.map((row) => ({ op: 'library.tag', targets: [ref(row)], tags: tagsFor(row) })),
        );
        setDialog((d) => {
          const row =
            d?.kind === 'detail' &&
            items.find((x) => libraryKey(x.target) === libraryKey(d.row.target));
          return row ? { ...d, row: { ...d.row, tags: tagsFor(row) } } : d;
        });
        return { state: 'saved', message: '' };
      },
      { keep: true },
    );
  async function loadProfiles(projectId) {
    const profiles = [];
    let offset = 0;
    do {
      const value = await controller.list({ project_id: projectId, type: 'profile' }, offset);
      profiles.push(...value.items);
      offset = value.next_offset;
    } while (offset !== null);
    return profiles;
  }
  async function detail(row) {
    const n = ++dialogEpoch.current;
    setDialog({ kind: 'detail', row });
    try {
      const value = await controller.usage(row.target);
      if (n === dialogEpoch.current && identity.current === account)
        setDialog((d) => (d?.kind === 'detail' ? { kind: 'detail', row: value } : d));
    } catch (e) {
      if (n === dialogEpoch.current) setError(errorText(e));
    }
  }
  async function chooseDestination(id) {
    const n = ++dialogEpoch.current;
    setDialog((old) => ({ ...old, destination: id, target: null }));
    if (!id) return;
    try {
      const result = await api.request('/v1/projects/' + encodeURIComponent(id));
      if (n === dialogEpoch.current && identity.current === account)
        setDialog((old) =>
          old?.kind === 'import' && old.destination === id ? { ...old, target: result } : old,
        );
    } catch (e) {
      if (n === dialogEpoch.current) setError(errorText(e));
    }
  }
  function importDialog(row) {
    dialogEpoch.current++;
    setDialog({ kind: 'import', row: copy(row), destination: '', target: null });
  }
  const dismiss = useMemo(
    () => () => {
      dialogEpoch.current++;
      setDialog(null);
    },
    [],
  );
  const openKey = dialog?.kind === 'detail' ? libraryKey(dialog.row.target) : null;
  const anchor = useMemo(
    () => () =>
      openKey ? document.querySelector(`[data-lib-key="${CSS.escape(openKey)}"]`) : null,
    [openKey],
  );
  if (!account)
    return <LibraryView account={account} filters={filters} setFilters={setFilters} next={null} />;
  const feedback = (
    <>
      {error && (
        <p role="alert">
          {error}{' '}
          <Button disabled={loading || busy} onClick={() => load()}>
            {t('library.refresh')}
          </Button>
        </p>
      )}
      {busy && !error && (
        <p role="status" className="muted">
          {t('library.working')}
        </p>
      )}
      {message && <p role="status">{message}</p>}
      {imported && (
        <a href={link(imported.project, imported.entity)}>{t('library.openImported')}</a>
      )}
      {pending && (
        <section role="status">
          <p>{t('library.pending')}</p>
          <Button disabled={busy} onClick={() => run(() => controller.recover())}>
            {t('library.check')}
          </Button>
          <Button disabled={busy} onClick={() => run(() => controller.resume())}>
            {t('library.resume')}
          </Button>
        </section>
      )}
    </>
  );
  const allTags = [...new Set(rows.flatMap((r) => r.tags || []))];
  const detailBody = (row) => {
    const isFile = ['image', 'video', 'audio'].includes(row.type),
      managed = managedRow(row);
    return (
      <>
        <h3 className="lib-float-title">
          <InlineName
            value={isFile ? displayName(row, t) : row.title}
            disabled={locked || managed}
            onSave={(title) => rename(row, title)}
          />
        </h3>
        <LibraryMedia key={libraryKey(row.target)} row={row} api={api} detail />
        <small className="muted">
          {t('trash.kind.' + row.type)} · {row.source_project_title || row.project_title}
        </small>
        {managed && <p className="muted lib-managed">{t('library.managedHint')}</p>}
        <div className="field" hidden={managed}>
          <span>{t('library.tagHeading')}</span>
          <TagEditor
            tags={row.tags}
            suggestions={allTags}
            disabled={locked}
            onChange={(tags) => setTags([row], () => tags)}
          />
        </div>
        {isFile && !managed && (
          <ReferencePicker
            items={[row]}
            disabled={locked}
            loadProfiles={loadProfiles}
            onFile={fileInto}
            onCreate={createReference}
          />
        )}
        <div className="libp-uses">
          <b>{t('library.usage')}</b>
          {(row.usage || []).map((u) => (
            <a
              className="use-shot"
              key={u.project_id + ':' + u.shot_id}
              href={link(u.project_id, u.shot_id)}
            >
              {t('library.shot')}
            </a>
          ))}
          {!row.usage?.length && <small className="muted">{t('library.unused')}</small>}
        </div>
        <div className="libp-actions">
          {!managed && (
            <a
              className="small"
              href={link(
                row.target.project_id,
                row.target.kind === 'entity' ? row.target.id : null,
              )}
            >
              {t('library.edit')}
            </a>
          )}
          <Button
            className={'small ' + (managed ? 'primary' : 'ghost')}
            disabled={locked}
            onClick={() => importDialog(row)}
          >
            {t('library.import')}
          </Button>
          <Button
            variant="ghost-danger"
            size="sm"
            disabled={locked}
            onClick={() => {
              dismiss();
              trash.request('trash', [row.target]);
            }}
          >
            {t('trash.move')}
          </Button>
        </div>
      </>
    );
  };
  return (
    <LibraryView
      account={account}
      filters={filters}
      setFilters={setFilters}
      projects={projects}
      rows={rows}
      counts={counts?.scope === countsScope ? counts.value : null}
      selected={selected}
      setSelected={setSelected}
      libraryKey={libraryKey}
      locked={locked}
      loading={loading}
      next={next}
      onMore={() => load(true)}
      onRefresh={() => load()}
      onUploadFiles={uploadFiles}
      onCreateProfile={createProfile}
      onRename={rename}
      onAddTags={(items, add) => setTags(items, (row) => [...new Set([...row.tags, ...add])])}
      loadProfiles={loadProfiles}
      onFileInto={fileInto}
      onCreateReference={createReference}
      onDelete={() => {
        if (currentRows.length)
          trash.request(
            'trash',
            currentRows.map((r) => r.target),
          );
      }}
      onDetail={detail}
      openKey={openKey}
      renderMedia={(row) => <LibraryMedia row={row} api={api} />}
      feedback={(dialog?.kind !== 'import' || mobile) && feedback}
    >
      {dialog?.kind === 'detail' &&
        (mobile ? (
          <Dialog title={t('library.detail')} onClose={dismiss} className="library-detail-dialog">
            {detailBody(dialog.row)}
          </Dialog>
        ) : (
          <FloatingPanel anchor={anchor} label={t('library.detail')} onClose={dismiss}>
            {detailBody(dialog.row)}
          </FloatingPanel>
        ))}
      {dialog?.kind === 'import' && (
        <Dialog title={t('library.import')} onClose={() => busy || dismiss()}>
          {feedback}
          <p>{displayName(dialog.row, t)}</p>
          <label className="field">
            <span>{t('library.destination')}</span>
            <select
              disabled={locked}
              value={dialog.destination}
              onChange={(e) => chooseDestination(e.target.value)}
            >
              <option value="">{t('library.choose')}</option>
              {projects
                .filter((p) => p.id !== dialog.row.target.project_id)
                .map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.title}
                  </option>
                ))}
            </select>
          </label>
          <div className="dialog-actions">
            <Button disabled={busy} onClick={dismiss}>
              {t('library.cancel')}
            </Button>
            <Button
              variant="primary"
              disabled={locked || !dialog.target}
              onClick={() => {
                const d = copy(dialog);
                run(() => {
                  guardDraft(d.destination);
                  return controller.startImport({
                    source: { ...d.row.target, expected_version: d.row.project_version },
                    target_project_id: d.destination,
                    expected_project_version: d.target.version,
                  });
                });
              }}
            >
              {t('library.copy')}
            </Button>
          </div>
        </Dialog>
      )}
    </LibraryView>
  );
}
