import React,{useEffect,useState} from 'react';
import {Trash2,CheckSquare,RotateCcw} from 'lucide-react';
import {ChatDialog} from './QuickChatControls.jsx';
import {HISTORY_ACTIONS,historySelectable,historyReason,purgeCountdown,historyBytes} from './creation-history-model.js';
const safely=promise=>promise.catch(()=>{});
const date=value=>Number.isFinite(value)?new Date(value*1000).toLocaleString():'';
function Totals({title,value,content=false}){return <section className="qc-history-totals"><h3>{title}</h3><p>{content&&`${value.sessions} 条创作 · ${value.turns} 轮对话 · ${value.cards} 张任务卡 · `}{value.assets} 份素材 · {value.artifacts} 份结果</p><small>{value.objects} 个存储对象 · {historyBytes(value.bytes)}</small></section>;}
function Preview({value,state,controller}){
  const [now,setNow]=useState(()=>Date.now()/1000);
  useEffect(()=>{const timer=setInterval(()=>setNow(Date.now()/1000),1000);return()=>clearInterval(timer);},[value.preview_id]);
  const expired=now>=value.expires_at,blocked=value.items.filter(item=>!item.eligible),permanent=['purge','empty_trash'].includes(value.action);
  return <div className="qc-history-preview">
    <p>{permanent?'永久删除不可恢复。请先核对将删除和保留的内容。':value.action==='trash'?'移入后从创作记录隐藏，可以在回收站恢复。自动清理以每条记录的授权和保留状态为准。':'恢复云端保存的创作内容，不启动任务或重新生成。'}</p>
    {permanent?<><Totals title="将永久删除" value={value.removed} content/><Totals title="引用及共享内容保留" value={value.kept}/></>:<p className="qc-history-totals">{value.items.length} 条创作将{value.action==='trash'?'移入回收站':'恢复'}，对话、任务卡、素材和结果均保留。</p>}
    <p className="qc-muted">任务、计费与操作收据始终保留。未发送文字仍留在当前浏览器的原草稿中，不属于这次云端清理范围。</p>
    {value.review_hold_release_required&&<p className="qc-warning">含人工复核保留的记录。确认永久删除会解除这些记录的保留；未确认前不会自动清理。</p>}
    {!!value.warnings.length&&<section className="qc-history-warnings"><h3>保留提醒</h3><ul>{value.warnings.map((warning,index)=><li key={index}>{warning.label||'相关素材或结果'}：{warning.project_id||warning.shot_id?'已被故事或镜头引用，原件保留':'共享或关联内容按服务端检查保留'}</li>)}</ul></section>}
    {!!blocked.length&&<p className="qc-error" role="alert">{blocked.length} 条记录不可操作：{[...new Set(blocked.map(item=>historyReason(item.blocked_reason)))].join('；')}。请返回列表重新选择。</p>}
    {expired&&<p className="qc-warning">预览已过期，请返回列表重新预览。</p>}
    <div className="qc-dialog-footer"><button type="button" disabled={state.busy} onClick={()=>controller.dismissHistoryPreview()}>返回列表</button><button type="button" className={permanent?'qc-history-danger':'qc-primary'} disabled={state.busy||!!state.pending||expired||!!blocked.length||!value.items.length} onClick={()=>safely(controller.confirmHistory())}>{HISTORY_ACTIONS[value.action]} · 确认 {value.items.length} 条</button></div>
  </div>;
}
function Receipt({state,controller}){const receipt=state.historyReceipt,pending=state.pending?.method==='actionHistory',blocked=receipt?.items.filter(item=>item.state==='blocked').length||0;return <>{receipt&&<p className={receipt.state==='pending'||blocked?'qc-warning':'qc-history-receipt'} role="status">{HISTORY_ACTIONS[receipt.action]}：{receipt.state==='pending'?'处理中，尚未确认永久删除':blocked?`原操作已核对，${blocked} 条受阻，未删除`:'已完成'}。共享引用与账本保留。</p>}{pending&&<div className="qc-warning"><p>{state.pending.actionId?'原清理操作处理中，核对同一操作的状态。':'原确认结果待核对，保留原标识和原请求，不建立第二次清理。'}</p><button type="button" disabled={state.busy} onClick={()=>safely(controller.recover())}>{state.pending.actionId?'核对原清理状态':'核对原确认结果'}</button></div>}</>;}
export default function CreationHistory({state,controller,busy,onSelect}){
  const [selecting,setSelecting]=useState(false),[archive,setArchive]=useState(null),[archiveError,setArchiveError]=useState(''),trash=state.historyView==='trash',preview=state.historyPreview,selected=state.historySelection,locked=busy||!!state.pending,rows=trash?state.trashSessions:state.sessions;
  useEffect(()=>{setSelecting(false);setArchive(null);setArchiveError('');},[state.account,state.session?.id]);
  function toggle(id){try{controller.selectHistory(selected.includes(id)?selected.filter(value=>value!==id):[...selected,id]);}catch{}}
  function selectAll(){try{controller.selectHistory(rows.filter(row=>historySelectable(row,state.historyView)).slice(0,100).map(row=>row.id));}catch{}}
  function leave(){if(!busy)safely(controller.historyView('active'));}
  async function archiveTest(){const live=controller.getState();if(archive.account!==live.account||archive.id!==live.session?.id||archive.version!==live.session.version){setArchiveError('账户或创作已更新，请重新核对。');return;}try{await controller.patch({expected_version:archive.version,is_test:true,test_accepted:true});setArchive(null);}catch{}}
  const error=state.error&&<p className="qc-error" role="alert">{state.error}</p>;
  const controls=<div className="qc-history-selection"><button type="button" disabled={locked} onClick={selectAll}>选择已加载可操作记录</button><button type="button" disabled={locked||!selected.length} onClick={()=>controller.selectHistory([])}>清除选择</button><span>{selected.length}/100 条</span></div>;
  return <>
    <div className="qc-history-heading"><span className="qc-eyebrow">创作记录</span><button type="button" disabled={busy||!state.account} aria-pressed={selecting} onClick={()=>{setSelecting(!selecting);controller.selectHistory([]);}}><CheckSquare size={13}/>{selecting?'完成':'批量选择'}</button></div>
    <div className="qc-history-tools"><label><input type="checkbox" checked={state.hideTests} disabled={busy||!state.account} onChange={event=>safely(controller.hideTests(event.target.checked))}/>隐藏测试记录</label><button type="button" disabled={busy||!state.account} onClick={()=>safely(controller.historyView('trash'))}><Trash2 size={13}/>回收站</button></div>
    {state.session?.id&&!trash&&<div className="qc-history-tags"><button disabled={locked||state.session.archive_pending===true} onClick={()=>safely(controller.patch({is_test:!state.session.is_test}))}>{state.session.is_test?'取消当前测试标记':'将当前创作标为测试'}</button>{state.session.is_test&&<button disabled={locked||state.session.archive_pending===true} onClick={()=>{setArchiveError('');setArchive({account:state.account,id:state.session.id,version:state.session.version});}}>{state.session.archive_pending?'验收完成，等待安全归档':'验收完成并归档…'}</button>}</div>}
    {selecting&&!trash&&<>{controls}<button className="qc-history-trash" disabled={locked||!selected.length} onClick={()=>safely(controller.previewHistory('trash'))}><Trash2 size={13}/>移到回收站（{selected.length}）</button></>}
    <nav className="qc-history-list">{state.sessions.map(session=><div className="qc-history-row" key={session.id}>{selecting&&<input aria-label={'选择创作 '+(session.title||'新的创作')} type="checkbox" checked={!trash&&selected.includes(session.id)} disabled={locked||trash||!historySelectable(session,'active')} onChange={()=>toggle(session.id)}/>}<button title={(session.title||'新的创作')+' · '+date(session.updated_at)+(session.blocked_reason?' · '+historyReason(session.blocked_reason):'')} className={session.id===state.session?.id?'active':''} disabled={busy||trash} onClick={()=>onSelect(session.id)}><span>{session.title||'新的创作'}</span>{session.is_test&&<small>测试</small>}{session.archive_pending&&<small>待归档</small>}{session.review_hold&&<small>复核保留</small>}</button></div>)}{!state.sessions.length&&<p className="qc-muted">{state.hideTests?'没有可见记录，测试记录已隐藏。':'还没有云端创作记录。'}</p>}{state.sessionsCursor&&<button disabled={busy} onClick={()=>safely(controller.list({more:true}))}>加载更早创作</button>}</nav>
    {!trash&&preview&&<ChatDialog title={HISTORY_ACTIONS[preview.action]+'前核对'} onClose={()=>{if(!busy)controller.dismissHistoryPreview();}}><Preview key={preview.preview_id} value={preview} state={state} controller={controller}/>{error}</ChatDialog>}
    {archive&&<ChatDialog title="验收完成并归档测试创作" onClose={()=>{if(!busy)setArchive(null);}}><p>标记验收完成。任务未结束时保持原创作，待服务端确认安全后才移到回收站。未发送文字留在浏览器原草稿；共享引用和任务账本保留。</p>{archiveError&&<p className="qc-error" role="alert">{archiveError}</p>}{error}<div className="qc-dialog-footer"><button disabled={busy} onClick={()=>setArchive(null)}>先不归档</button><button className="qc-primary" disabled={locked} onClick={archiveTest}>确认验收完成并归档</button></div></ChatDialog>}
    {trash&&<ChatDialog title="创作回收站" wide onClose={leave} className="qc-history-dialog">{preview?<Preview key={preview.preview_id} value={preview} state={state} controller={controller}/>:<>
      <p className="qc-muted">通常保留 5 天。人工复核保留或缺少清理授权的记录不自动删除。恢复不会重新运行任务。{state.trashAsOf&&<small>清理倒计时截至 {date(state.trashAsOf)}。</small>}</p>
      <Receipt state={state} controller={controller}/>{controls}
      <div className="qc-history-trash-list">{state.trashSessions.map(row=><label className="qc-history-trash-row" key={row.id}><input type="checkbox" aria-label={'选择回收站创作 '+(row.title||'新的创作')} checked={selected.includes(row.id)} disabled={locked||!historySelectable(row,'trash')} onChange={()=>toggle(row.id)}/><span><b>{row.title||'新的创作'}</b><small>{purgeCountdown(row,state.trashAsOf)}{row.is_test?' · 测试记录':''}</small>{row.blocked_reason&&<small>{historyReason(row.blocked_reason)}</small>}</span><small>{date(row.trashed_at)}</small></label>)}{!state.trashSessions.length&&<p className="qc-history-empty">回收站暂无记录。</p>}</div>
      {state.trashCursor&&<button disabled={busy} onClick={()=>safely(controller.list({view:'trash',more:true}))}>加载更多回收站记录</button>}
      <div className="qc-history-actions"><button disabled={locked||!selected.length} onClick={()=>safely(controller.previewHistory('restore'))}><RotateCcw size={14}/>恢复所选</button><button disabled={locked||!selected.length} onClick={()=>safely(controller.previewHistory('purge'))}>永久删除所选</button><button className="qc-history-danger" disabled={locked||!state.trashSessions.length} onClick={()=>safely(controller.previewHistory('empty_trash'))}>预览清空整个回收站</button></div>
    </>}{error}</ChatDialog>}
  </>;
}
