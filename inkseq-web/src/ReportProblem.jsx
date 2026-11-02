import React, { useRef, useState, useSyncExternalStore } from 'react';
import { Bug } from 'lucide-react';
import { Button, IconButton } from './ui/Button.jsx';
import { Dialog } from './ui/Dialog.jsx';
import { useClient } from './AdminConsole.jsx';
import { useI18n } from './i18n/I18nProvider.jsx';
import { store } from './store.js';
import {
  REPORT_CATEGORIES,
  REPORT_MAX,
  reportBody,
  reportClient,
  reportPage,
  reportProblemKey,
  reportShortId,
} from './report-problem.js';
import './report-problem.css';

function ReportDialog({ account, onClose }) {
  const { t } = useI18n(),
    api = useClient(account),
    key = useRef(crypto.randomUUID());
  const state = useSyncExternalStore(store.subscribe, store.getState);
  const [category, setCategory] = useState('page'),
    [text, setText] = useState(''),
    [busy, setBusy] = useState(false),
    [problem, setProblem] = useState(''),
    [sent, setSent] = useState('');
  const page = reportPage();
  const inStory = state.workspace?.mode === 'cloud' && state.workspace?.account === account;
  const selected = inStory && state.project?.entities?.find((e) => e.id === state.selectedId);
  // A changed report is a new report; the same one sent again is not sent twice.
  const edit = (change) => (value) => {
    key.current = crypto.randomUUID();
    setProblem('');
    change(value);
  };
  const send = async () => {
    setBusy(true);
    setProblem('');
    try {
      const receipt = await api.request('/v1/feedback', {
        method: 'POST',
        key: key.current,
        body: reportBody({
          category,
          text,
          page,
          client: reportClient(),
          projectId: inStory ? state.project?.id : '',
          shotId: selected?.type === 'shot' ? selected.id : '',
        }),
      });
      setSent(receipt.report_id);
    } catch (error) {
      setProblem(reportProblemKey(error));
    } finally {
      setBusy(false);
    }
  };
  if (sent)
    return (
      <Dialog title={t('report.thanksTitle')} onClose={onClose} portal>
        <p role="status">{t('report.thanks', { id: reportShortId(sent) })}</p>
        <div className="report-actions">
          <Button variant="primary" onClick={onClose}>
            {t('report.done')}
          </Button>
        </div>
      </Dialog>
    );
  return (
    <Dialog
      title={t('report.title')}
      onClose={onClose}
      busy={busy}
      closeOnBackdrop={!text.trim()}
      portal
    >
      <label className="field">
        {t('report.category')}
        <select
          value={category}
          disabled={busy}
          onChange={(event) => edit(setCategory)(event.target.value)}
        >
          {REPORT_CATEGORIES.map((name) => (
            <option key={name} value={name}>
              {t('report.category.' + name)}
            </option>
          ))}
        </select>
      </label>
      <label className="field">
        {t('report.what')}
        <textarea
          value={text}
          rows={6}
          maxLength={REPORT_MAX}
          disabled={busy}
          placeholder={t('report.placeholder')}
          onChange={(event) => edit(setText)(event.target.value)}
        />
      </label>
      <small className="muted report-note">{t('report.steps')}</small>
      <small className="muted report-note">
        {t(inStory ? 'report.pageStory' : 'report.page', { page })}
      </small>
      <small className="muted report-note">{t('report.privacy')}</small>
      {problem && <p role="alert">{t(problem)}</p>}
      <div className="report-actions">
        <Button disabled={busy} onClick={onClose}>
          {t('report.cancel')}
        </Button>
        <Button variant="primary" disabled={busy || !text.trim()} onClick={send}>
          {t('report.send')}
        </Button>
      </div>
    </Dialog>
  );
}

// Signed-in only: a report is linked to the account so we can reply.
export default function ReportProblem({ account }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  if (!account) return null;
  return (
    <>
      <IconButton label={t('report.open')} title={t('report.open')} onClick={() => setOpen(true)}>
        <Bug size={15} />
      </IconButton>
      {open && <ReportDialog account={account} onClose={() => setOpen(false)} />}
    </>
  );
}
