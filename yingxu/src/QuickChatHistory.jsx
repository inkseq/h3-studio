import React from 'react';
import {useI18n} from './i18n/I18nProvider.jsx';

export default function QuickChatHistory({state,disabled,onCards,onText}){
  const {t}=useI18n(),error=state.cardHistoryError;
  return <header className="quick-history" data-card-history aria-live="polite">
    {state.cardHistoryLoading?<p role="status">{t('quickHistory.loading')}</p>:error?<><p className="qc-error" role="alert">{t(error.key,{status:error.status})}</p><button type="button" data-history-retry disabled={disabled} onClick={onCards}>{t(error.reset?'quickHistory.refresh':'quickHistory.retry')}</button></>:state.cardHistoryHasMore?<button type="button" data-history-load disabled={disabled} onClick={onCards}>{t('quickHistory.more')}</button>:state.cardHistoryLoaded?<p className="muted" data-history-start role="status">{t('quickHistory.start')}</p>:null}
    {state.cardHistoryLoaded&&!state.cardHistoryHasMore&&!error&&(state.historyCursor||state.textHistoryLoading)&&<div className="quick-history-text"><button type="button" disabled={disabled||state.textHistoryLoading} onClick={onText}>{t(state.textHistoryLoading?'quickHistory.textLoading':'quickHistory.textMore')}</button>{state.textHistoryError&&<p className="qc-error" role="alert">{t(state.textHistoryError.key,{status:state.textHistoryError.status})}</p>}</div>}
  </header>;
}
