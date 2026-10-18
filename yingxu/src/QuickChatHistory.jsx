import React, { useEffect, useRef } from 'react';
import { useI18n } from './i18n/I18nProvider.jsx';
import { LoaderCircle } from 'lucide-react';
import { observeEarlierCards } from './quick-thread-window.js';

export default function QuickChatHistory({
  state,
  account,
  readyAccount,
  sessionReady,
  onCards,
  onText,
  scrollRef,
  autoReady = false,
  active = true,
}) {
  const { t } = useI18n(),
    error = state.cardHistoryError;
  // Card paging is a scoped, single-flight GET. The rest of open()/refresh()
  // can still hydrate jobs after the initial page is already usable.
  const disabled =
    !sessionReady ||
    !account ||
    readyAccount !== account ||
    state.account !== account ||
    !state.session?.id ||
    !!state.pending ||
    (!state.cardHistoryLoaded && !error);
  const sentinel = useRef(null),
    live = useRef(null);
  live.current = {
    scope: JSON.stringify([account, state.session?.id]),
    cursor: state.cardHistoryCursor,
    enabled: active && autoReady && !disabled,
    hasMore: state.cardHistoryHasMore,
    loading: state.cardHistoryLoading,
    error,
    onCards,
  };
  useEffect(
    () =>
      observeEarlierCards(sentinel.current, scrollRef?.current, {
        snapshot: () => live.current,
        load: () => live.current.onCards(),
      }),
    [
      scrollRef,
      account,
      state.session?.id,
      state.cardHistoryCursor,
      state.cardHistoryHasMore,
      state.cardHistoryLoading,
      error,
      disabled,
      active,
      autoReady,
    ],
  );
  const automatic = active && autoReady && typeof globalThis.IntersectionObserver === 'function';
  return (
    <header className="quick-history" data-card-history aria-live="polite">
      <span ref={sentinel} className="quick-history-sentinel" aria-hidden="true" />
      {state.cardHistoryLoading ? (
        <p role="status">
          <LoaderCircle size={14} className="quick-history-spinner" aria-hidden="true" />
          {t('quickHistory.loading')}
        </p>
      ) : error ? (
        <>
          <p className="qc-error" role="alert">
            {t(error.key, { status: error.status })}
          </p>
          <button type="button" data-history-retry disabled={disabled} onClick={onCards}>
            {t(error.reset ? 'quickHistory.refresh' : 'quickHistory.retry')}
          </button>
        </>
      ) : state.cardHistoryHasMore ? (
        <button
          type="button"
          className={automatic ? 'quick-history-fallback' : undefined}
          data-history-load
          disabled={disabled}
          onClick={onCards}
        >
          {t('quickHistory.more')}
        </button>
      ) : state.cardHistoryLoaded ? (
        <p className="muted" data-history-start role="status">
          {t(state.historyCursor ? 'quickHistory.start' : 'quickHistory.beginning')}
        </p>
      ) : null}
      {state.cardHistoryLoaded &&
        !state.cardHistoryHasMore &&
        !error &&
        (state.historyCursor || state.textHistoryLoading) && (
          <div className="quick-history-text">
            <button type="button" disabled={disabled || state.textHistoryLoading} onClick={onText}>
              {t(state.textHistoryLoading ? 'quickHistory.textLoading' : 'quickHistory.textMore')}
            </button>
            {state.textHistoryError && (
              <p className="qc-error" role="alert">
                {t(state.textHistoryError.key, { status: state.textHistoryError.status })}
              </p>
            )}
          </div>
        )}
    </header>
  );
}
