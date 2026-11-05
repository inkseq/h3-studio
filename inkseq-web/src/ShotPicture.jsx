// Picture first (h3-studio#1039): make a shot's first frame and look at it before spending a clip.
// The server holds the credits, draws, and charges only once the picture is on the shot. Shown only
// where the server has story pictures on, and only in the place the first frame is chosen.
import React, { useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { ImagePlus } from 'lucide-react';
import { CreditUsageBar, creditShortfall, useCredits } from './CreditsUI.jsx';
import { useCloud } from './use-cloud.js';
import { useI18n } from './i18n/I18nProvider.jsx';
import { Button } from './ui/Button.jsx';
import { Dialog, DialogActions } from './ui/Dialog.jsx';
import { useStoryboardUI as useStore } from './storyboard-ui-context.jsx';

const freshId = () => 'shot-pic-' + crypto.randomUUID().replaceAll('-', '').slice(0, 24);

export function ShotPicture({ shot, hasPicture }) {
  const { t, locale } = useI18n();
  const { adapter, dispatch } = useStore();
  const c = useCloud();
  const balance = useCredits(c?.account)?.available;
  const read = adapter?.readPictureSettings,
    draw = adapter?.drawShotPicture;
  const { data } = useQuery({
    queryKey: ['picture-settings'],
    queryFn: () => read(),
    enabled: !!read,
    staleTime: 60000,
    retry: false,
  });
  const [open, setOpen] = useState(false),
    [busy, setBusy] = useState(false),
    [failed, setFailed] = useState(''),
    mode = shot.mode;
  // A retry after an unknown outcome reuses the id, so one click never draws or charges twice.
  const request = useRef(null);
  if (!draw || data?.pictures_enabled !== true) return null;
  const credits = Number.isSafeInteger(data.image_credits) ? data.image_credits : null;
  const close = () => {
    if (busy) return;
    setOpen(false);
    setFailed('');
  };
  const start = async () => {
    setBusy(true);
    setFailed('');
    request.current ||= freshId();
    try {
      // A shot saved the older way moves to the current settings first, the same way choosing its
      // mode does, keeping its people, place and references. The server refuses it otherwise.
      if (!shot.entity?.data?.storyboard) {
        await dispatch({ type: 'update', id: shot.id, patch: { mode } });
      }
      await draw(shot.id, request.current, locale === 'en' ? 'en' : 'zh');
      request.current = null;
      setOpen(false);
    } catch (error) {
      if (error?.detail?.detail?.nothing_charged === true) request.current = null;
      setFailed(
        error?.detail?.detail?.code === 'story_chat_shot_settings_invalid'
          ? 'shotPicture.settingsChanged'
          : 'shotPicture.failed',
      );
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="shot-picture">
      <Button size="sm" onClick={() => setOpen(true)}>
        <ImagePlus size={15} aria-hidden="true" />
        {t(hasPicture ? 'shotPicture.again' : 'shotPicture.make')}
      </Button>
      {open && (
        <Dialog title={t('shotPicture.title')} onClose={close} busy={busy}>
          <p className="sb-confirm-note">{t('shotPicture.note')}</p>
          <CreditUsageBar credits={credits} balance={balance} />
          {failed && <p role="alert">{t(failed)}</p>}
          <DialogActions>
            <Button disabled={busy} onClick={close}>
              {t('shotPicture.cancel')}
            </Button>
            <Button
              variant="primary"
              disabled={busy || creditShortfall(credits, balance)}
              onClick={() => void start()}
            >
              {t('shotPicture.confirm')}
            </Button>
          </DialogActions>
        </Dialog>
      )}
    </div>
  );
}
