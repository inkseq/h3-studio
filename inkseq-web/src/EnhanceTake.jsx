// Enhance one finished take to 1080P/2K (h3-studio#795). Both tiers are planned when the dialog
// opens, so switching never waits or greys the start button. The sharper copy arrives as a new
// take of the same shot (queued → running → done in its takes); the original stays.
import React, { useEffect, useState } from 'react';
import { Sparkles } from 'lucide-react';
import { Button } from './ui/Button.jsx';
import { Dialog, DialogActions } from './ui/Dialog.jsx';
import { Segmented } from './ui/Segmented.jsx';
import { CreditUsageBar, creditShortfall, useCredits } from './CreditsUI.jsx';
import { useCloud } from './use-cloud.js';
import { cloudController as cloud } from './cloud-controller.js';
import { useI18n } from './i18n/I18nProvider.jsx';
import { batchEnhanceTakes, enhanceable } from './enhance-take.js';
import { enhanceAllPlan, howManyFit } from './shot-status.js';
import './enhance-take.css';

const TIERS = ['1080p', '2k'];

export function EnhanceTakeButton({ take, onMessage }) {
  const { t } = useI18n(),
    c = useCloud(),
    [open, setOpen] = useState(false);
  if (!enhanceable(take, c.capabilities)) return null;
  return (
    <>
      <Button size="sm" title={t('enhance.help')} onClick={() => setOpen(true)}>
        <Sparkles size={14} />
        {t('enhance.button')}
      </Button>
      {open && (
        <EnhanceDialog
          entityId={take.asset.entity.id}
          onClose={(started) => {
            setOpen(false);
            if (started) onMessage?.(t('enhance.started'));
          }}
        />
      )}
    </>
  );
}

// Selected shots (h3-studio#964): one sharper copy of each shot's current take. The queued
// copies show on their shots, so the selection simply clears once they start.
export function EnhanceSelectedButton({ shots, ids, chosen, onStarted }) {
  const { t } = useI18n(),
    c = useCloud(),
    [open, setOpen] = useState(false);
  if (c.capabilities?.enhance?.enabled !== true) return null;
  const takes = batchEnhanceTakes(shots, ids, c.capabilities, chosen);
  return (
    <>
      <Button disabled={!takes.length} title={t('enhance.batchHelp')} onClick={() => setOpen(true)}>
        <Sparkles size={14} />
        {t('enhance.batchButton', { count: takes.length })}
      </Button>
      {open && (
        <EnhanceSelectedDialog
          takes={takes}
          onClose={(count) => {
            setOpen(false);
            if (count) onStarted?.();
          }}
        />
      )}
    </>
  );
}

function EnhanceSelectedDialog({ takes, onClose }) {
  const { t } = useI18n(),
    c = useCloud(),
    balance = useCredits(c.account)?.available,
    [tier, setTier] = useState('1080p'),
    [plans, setPlans] = useState(null),
    [busy, setBusy] = useState(false),
    [started, setStarted] = useState(0),
    [failed, setFailed] = useState(false),
    [entityIds] = useState(() => takes.map((take) => take.asset.entity.id));
  useEffect(() => {
    let alive = true;
    Promise.all(
      TIERS.map((value) => Promise.all(entityIds.map((id) => cloud.planEnhance(id, value)))),
    )
      .then((values) => alive && setPlans(Object.fromEntries(TIERS.map((v, i) => [v, values[i]]))))
      .catch(() => alive && setFailed(true));
    return () => {
      alive = false;
    };
  }, [entityIds]);
  const ready = (plans?.[tier] || []).filter((plan) => plan.status === 'ready'),
    charged = ready.length > 0 && ready.every((plan) => plan.admission === 'credits'),
    credits = charged ? ready.reduce((sum, plan) => sum + plan.credits, 0) : null,
    short = creditShortfall(credits, balance);
  const start = async () => {
    setBusy(true);
    setFailed(false);
    let count = 0;
    try {
      // One at a time: each submit updates the shared job list.
      for (const plan of ready) {
        await cloud.submitEnhance(plan);
        count += 1;
      }
      onClose(count);
    } catch {
      setStarted(count);
      setFailed(true);
      setBusy(false);
    }
  };
  return (
    <Dialog title={t('enhance.batchTitle')} onClose={() => onClose(started)} busy={busy}>
      <p className="sb-confirm-note">{t('enhance.batchIntro', { count: entityIds.length })}</p>
      <Segmented
        label={t('enhance.batchTitle')}
        options={TIERS.map((value) => ({ value, label: t('enhance.tier.' + value) }))}
        value={tier}
        onChange={setTier}
      />
      <small className="muted">{t('enhance.tier.' + tier + 'Note')}</small>
      <CreditUsageBar credits={credits} balance={balance} />
      <p className="sb-confirm-note">
        {plans ? t('enhance.batchTiming') : failed ? '' : t('enhance.checking')}
      </p>
      {plans && ready.length < entityIds.length && (
        <p role="alert">{t('enhance.batchSkipped', { count: entityIds.length - ready.length })}</p>
      )}
      {failed && (
        <p role="alert">
          {started ? t('enhance.batchPartial', { count: started }) : t('enhance.failed')}
        </p>
      )}
      <DialogActions>
        <Button disabled={busy} onClick={() => onClose(started)}>
          {t('enhance.cancel')}
        </Button>
        <Button
          variant="primary"
          disabled={busy || !ready.length || short || started > 0}
          onClick={() => void start()}
        >
          {t('enhance.start')}
        </Button>
      </DialogActions>
    </Dialog>
  );
}

function EnhanceDialog({ entityId, onClose }) {
  const { t } = useI18n(),
    c = useCloud(),
    balance = useCredits(c.account)?.available,
    [tier, setTier] = useState('1080p'),
    [plans, setPlans] = useState(null),
    [busy, setBusy] = useState(false),
    [failed, setFailed] = useState(false);
  useEffect(() => {
    let alive = true;
    Promise.all(TIERS.map((value) => cloud.planEnhance(entityId, value)))
      .then((values) => alive && setPlans(Object.fromEntries(TIERS.map((v, i) => [v, values[i]]))))
      .catch(() => alive && setFailed(true));
    return () => {
      alive = false;
    };
  }, [entityId]);
  const plan = plans?.[tier],
    credits = plan?.admission === 'credits' ? plan.credits : null,
    short = creditShortfall(credits, balance),
    notReady = plan && plan.status !== 'ready';
  const start = async () => {
    setBusy(true);
    setFailed(false);
    try {
      await cloud.submitEnhance(plan);
      onClose(true);
    } catch {
      setFailed(true);
      setBusy(false);
    }
  };
  return (
    <Dialog title={t('enhance.title')} onClose={() => onClose(false)} busy={busy}>
      <p className="sb-confirm-note">{t('enhance.intro')}</p>
      <Segmented
        label={t('enhance.title')}
        options={TIERS.map((value) => ({ value, label: t('enhance.tier.' + value) }))}
        value={tier}
        onChange={setTier}
      />
      <small className="muted">{t('enhance.tier.' + tier + 'Note')}</small>
      <CreditUsageBar credits={credits} balance={balance} />
      <p className="sb-confirm-note">
        {plans ? t('enhance.timing') : failed ? '' : t('enhance.checking')}
      </p>
      {notReady && <p role="alert">{t('enhance.notReady')}</p>}
      {failed && <p role="alert">{t('enhance.failed')}</p>}
      <DialogActions>
        <Button disabled={busy} onClick={() => onClose(false)}>
          {t('enhance.cancel')}
        </Button>
        <Button
          variant="primary"
          disabled={busy || !plan || notReady || short}
          onClick={() => void start()}
        >
          {t('enhance.start')}
        </Button>
      </DialogActions>
    </Dialog>
  );
}

// Plans for many takes, a few at a time, so a long story does not flood the service.
async function planAll(entityIds, tier) {
  const out = new Array(entityIds.length);
  let next = 0;
  const worker = async () => {
    while (next < entityIds.length) {
      const at = next++;
      out[at] = await cloud.planEnhance(entityIds[at], tier);
    }
  };
  await Promise.all([1, 2, 3, 4].map(worker));
  return out;
}

// The whole story (h3-studio#893): every shot whose chosen take is an original with no copy at
// that size yet. Shots left out are listed with why. A shot already being enhanced is never sent
// again, and a second press finds its copy and leaves it out.
export function EnhanceAllButton({ shots }) {
  const { t } = useI18n(),
    c = useCloud(),
    [open, setOpen] = useState(false);
  const enabled = c.capabilities?.enhance?.enabled === true;
  if (!enabled) return null;
  const any = enhanceAllPlan(shots, '1080p', true).targets.length > 0;
  return (
    <>
      <Button disabled={!any} title={t('enhance.allHelp')} onClick={() => setOpen(true)}>
        <Sparkles size={14} />
        <span className="label">{t('enhance.allButton')}</span>
      </Button>
      {open && <EnhanceAllDialog shots={shots} onClose={() => setOpen(false)} />}
    </>
  );
}

function EnhanceAllDialog({ shots, onClose }) {
  const { t } = useI18n(),
    c = useCloud(),
    balance = useCredits(c.account)?.available,
    [tier, setTier] = useState('1080p'),
    // The shots as they were when the dialog opened: what is listed is what is sent.
    [frozen] = useState(() =>
      Object.fromEntries(TIERS.map((value) => [value, enhanceAllPlan(shots, value, true)])),
    ),
    [plans, setPlans] = useState({}),
    [busy, setBusy] = useState(false),
    [started, setStarted] = useState(0),
    [failed, setFailed] = useState(false);
  const plan = frozen[tier],
    number = (shot) => shots.findIndex((s) => s.id === shot.id) + 1;
  useEffect(() => {
    let alive = true;
    for (const value of TIERS)
      planAll(
        frozen[value].targets.map((target) => target.take.asset.entity.id),
        value,
      )
        .then((list) => alive && setPlans((old) => ({ ...old, [value]: list })))
        .catch(() => alive && setFailed(true));
    return () => {
      alive = false;
    };
  }, [frozen]);
  const list = plans[tier],
    ready = list
      ? plan.targets
          .map((target, i) => ({ target, plan: list[i] }))
          .filter((row) => row.plan?.status === 'ready')
      : [],
    charged = ready.length > 0 && ready.every((row) => row.plan.admission === 'credits'),
    costs = ready.map((row) => row.plan.credits),
    credits = charged ? costs.reduce((sum, value) => sum + value, 0) : null,
    short = creditShortfall(credits, balance),
    fit = short ? howManyFit(costs, balance) : null,
    // Short of credits: only an explicit "the first N" is ever sent, never a silent part.
    sending = short ? ready.slice(0, fit || 0) : ready;
  const start = async () => {
    setBusy(true);
    setFailed(false);
    let count = 0;
    try {
      for (const row of sending) {
        await cloud.submitEnhance(row.plan);
        count += 1;
      }
      onClose(count);
    } catch {
      setStarted(count);
      setFailed(true);
      setBusy(false);
    }
  };
  const skipped = [
    ...plan.skipped,
    ...(list
      ? plan.targets
          .filter((target, i) => list[i]?.status !== 'ready')
          .map((target) => ({ shot: target.shot, reason: 'unavailable' }))
      : []),
  ];
  const total = list ? ready.length : plan.targets.length;
  return (
    <Dialog title={t('enhance.allTitle')} onClose={() => onClose(started)} busy={busy}>
      <Segmented
        label={t('enhance.allTitle')}
        options={TIERS.map((value) => ({ value, label: t('enhance.tier.' + value) }))}
        value={tier}
        onChange={setTier}
      />
      <small className="muted">{t('enhance.tier.' + tier + 'Note')}</small>
      <p className="sb-confirm-note">
        {t('enhance.allCount' + (total === 1 ? '.one' : ''), {
          count: total,
          skipped: skipped.length,
        })}
      </p>
      {skipped.length > 0 && (
        <ul className="enhance-skipped">
          {skipped.map(({ shot, reason }) => (
            <li key={shot.id}>
              <b>{t('enhance.allShot', { n: number(shot) })}</b>
              {shot.entity?.title ? ' ' + shot.entity.title : ''} · {t('enhance.skip.' + reason)}
            </li>
          ))}
        </ul>
      )}
      <CreditUsageBar credits={credits} balance={balance} />
      {short && (
        <p role="alert">
          {fit ? t('enhance.allFit', { fit, count: ready.length }) : t('enhance.allNone')}
        </p>
      )}
      <p className="sb-confirm-note">{t('enhance.allUseCopy')}</p>
      <p className="sb-confirm-note">
        {list ? t('enhance.batchTiming') : failed ? '' : t('enhance.checking')}
      </p>
      {failed && (
        <p role="alert">
          {started ? t('enhance.allPartial', { count: started }) : t('enhance.failed')}
        </p>
      )}
      <DialogActions>
        <Button disabled={busy} onClick={() => onClose(started)}>
          {t('enhance.cancel')}
        </Button>
        <Button
          variant="primary"
          disabled={busy || !sending.length || started > 0}
          onClick={() => void start()}
        >
          {short && fit
            ? t('enhance.allStartFirst', { count: fit })
            : t('enhance.allStart', { count: list ? sending.length : plan.targets.length })}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
