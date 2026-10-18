import {createQuickChatClient} from './quick-chat-client.js';
import {stableJSON,digest} from './cloud-model.js';
import {bindingPayload} from './quick-chat-model.js';
import {requireModeAvailable} from './generation-availability.js';
import {rememberDraftOrigin,clearAcceptedDraft} from './quick-chat-draft.js';
import {HISTORY_ACTIONS,historySelection,checkedHistoryPreview,checkedHistoryResult} from './creation-history-model.js';
import {CARD_HISTORY_PAGE_SIZE,emptyCardHistory,checkedCardPage} from './quick-chat-history.js';
import {currentConversationRow} from './quick-chat-sidebar.js';
import {readLocale,translate} from './i18n/locale.js';
const clone=value=>structuredClone(value),uuid=()=>crypto.randomUUID();
const record=(value,field)=>value?.[field]||value;
const merge=(older,newer)=>[...new Map([...older,...newer].map(value=>[value.id||value.binding_id,value])).values()];
const initialHistory=()=>({hideTests:false,historyView:'active',historySelection:[],historyPreview:null,historyReceipt:null,trashSessions:[],trashCursor:null,trashAsOf:null,sessionsAsOf:null});
export async function saveMaterialSelection(controller,{original,next,proposed}){
  const account=original.account,sid=original.session?.id;
  let expectedVersion=original.session?.version;
  function checked(version=true){const live=controller.getState();if(!sid||live.account!==account||live.session?.id!==sid||version&&live.session.version!==expectedVersion)throw Error('创作已被更新或目标已改变。请核对最新材料后再调整，原素材保留。');return live;}
  const live=checked();
  // Profile and controls may change without changing FL/REF mode.
  if(stableJSON(next)!==stableJSON(live.session.next_settings)){
    const patched=await controller.patch({expected_version:expectedVersion,next_settings:next});
    expectedVersion=record(patched,'session').version;
  }
  const latest=checked(),merged=proposed.map(binding=>{const known=latest.materials.find(item=>item.asset_id===binding.asset_id);return {...bindingPayload(binding),binding_id:known?.binding_id||binding.binding_id,version:known?.version??binding.version??0};});
  await controller.saveMaterials(merged);
  checked(false);return true;
}
export function createQuickChatController({client=createQuickChatClient(),storage=globalThis.localStorage}={}){
  let epoch=0,sessionEpoch=0,readGeneration=0,refreshing=false,availabilitySequence=0,availabilityRequest=null,historyEpoch=0,listSequence={active:0,trash:0};
  let cardPageSequence=0,cardPageRequest=null,textPageSequence=0;
  let state={account:null,schema:null,capabilities:null,availability:null,availabilityChecking:false,operatorAccess:false,sessions:[],sessionsCursor:null,session:null,materials:[],timeline:[],historyCursor:null,forwardCursor:null,cards:{},revisions:{},preflights:{},submissions:{},imports:{},busy:false,error:'',notice:'',pending:null,lastUpdated:null,draftReset:null,...initialHistory(),...emptyCardHistory()};
  const listeners=new Set();const emit=patch=>{state={...state,...patch};for(const fn of listeners)fn();};
  const context=()=>({epoch,sessionEpoch,account:state.account,sessionId:state.session?.id});
  function guard(ctx,{session=true}={}){if(ctx.epoch!==epoch||ctx.account!==state.account||ctx.readGeneration!==undefined&&ctx.readGeneration!==readGeneration||session&&(ctx.sessionEpoch!==sessionEpoch||ctx.sessionId!==state.session?.id))throw Error('工作账户、会话或内容已经改变；旧响应没有加入当前创作。');}
  async function refreshAvailability({fresh=false}={}){
    if(!state.account)return null;
    if(availabilityRequest&&!fresh)return availabilityRequest;
    const ctx=context(),sequence=++availabilitySequence;emit({availabilityChecking:true});
    const request=(async()=>{try{const value=await Promise.resolve().then(()=>client.generationAvailability());guard(ctx,{session:false});if(sequence!==availabilitySequence)throw Error('执行状态已被更新，请重新核对。');emit({availability:value});return value;}catch(error){if(ctx.epoch===epoch&&sequence===availabilitySequence)emit({availability:null});throw error;}finally{if(ctx.epoch===epoch&&sequence===availabilitySequence){availabilityRequest=null;emit({availabilityChecking:false});}}})();
    availabilityRequest=request;return request;
  }
  async function checkGeneration(revision){
    const ctx=context();if(state.pending)throw Error('有原操作结果待核对，请先恢复原操作，不要重复提交。');
    const snapshot=await refreshAvailability({fresh:true});guard(ctx);
    return requireModeAvailable(snapshot,state.capabilities,revision);
  }
  async function readOperatorAccess(){const ctx=context();try{const me=await client.me();guard(ctx,{session:false});emit({operatorAccess:(me.username||me.account)===state.account&&me.operator_capacity?.view===true});}catch{}}
  const pendingName=account=>`yingxu:quick-chat:operation:${account}`;
  function readPending(account){try{return account?JSON.parse(storage?.getItem(pendingName(account))||'null'):null;}catch{return {corrupt:true};}}
  function writePending(value){if(!state.account||!storage)throw Error('无法保存原操作标识，未提交。请启用浏览器存储后重试。');if(value)storage.setItem(pendingName(state.account),JSON.stringify(value));else storage.removeItem(pendingName(state.account));emit({pending:value});}
  function invalidateHistory(patch={}){historyEpoch++;emit({historyPreview:null,...patch});}
  async function list({more=false,view='active'}={}){
    if(!['active','trash'].includes(view))throw Error('创作列表类型不正确。');
    invalidateHistory();const ctx=context(),sequence=++listSequence[view],hideTests=view==='active'&&state.hideTests;
    const data=await client.sessions({cursor:more?(view==='active'?state.sessionsCursor:state.trashCursor):null,view,hideTests});guard(ctx,{session:false});
    if(sequence!==listSequence[view]||hideTests!==(view==='active'&&state.hideTests))throw Error('筛选已改变，旧列表未加入。');
    const rows=more?merge(view==='active'?state.sessions:state.trashSessions,data.sessions||[]):data.sessions||[];
    emit(view==='active'?{sessions:rows,sessionsCursor:data.next_cursor||null,sessionsAsOf:data.as_of??null}:{trashSessions:rows,trashCursor:data.next_cursor||null,trashAsOf:data.as_of??null});return data;
  }
  function closeArchivedSession(items){if(!items.some(item=>item.id===state.session?.id&&['trashed','purging','purged'].includes(item.trash_state)))return;sessionEpoch++;readGeneration++;client.reset();emit({session:null,materials:[],timeline:[],cards:{},revisions:{},preflights:{},submissions:{},imports:{},historyCursor:null,forwardCursor:null,draftReset:null,...resetCardHistory()});}
  async function finishHistory(value,operation,ctx){
    guard(ctx,{session:false});const receipt=checkedHistoryResult(value,operation);
    if(receipt.state==='pending')writePending({...operation,actionId:receipt.action_id});else writePending(null);
    guard(ctx,{session:false});closeArchivedSession(receipt.items);guard(ctx,{session:false});invalidateHistory({historyReceipt:receipt,historySelection:[],notice:receipt.state==='pending'?'永久删除已受理，仍在处理中；请核对原清理状态。':receipt.items.some(item=>item.state==='blocked')?'原清理操作已核对，受阻记录仍保留。':HISTORY_ACTIONS[receipt.action]+'已完成，任务和计费账本保留。'});guard(ctx,{session:false});
    await list();guard(ctx,{session:false});if(state.historyView==='trash')await list({view:'trash'});return receipt;
  }
  async function historyFailure(error,operation,ctx){
    guard(ctx,{session:false});const proof=error.detail?.detail||error.detail;
    if(proof?.resource==='quick_chat_history'&&proof.action==='confirm'&&proof.no_mutation_committed===true&&['history_preview_expired','history_preview_changed','history_action_blocked'].includes(proof.code)&&proof.request_hash===await digest(operation.body)){
      guard(ctx,{session:false});if(stableJSON(state.pending)!==stableJSON(operation))throw Error('原操作已变化，请核对原记录。');writePending(null);invalidateHistory();
    }
    throw error;
  }
  async function recoverHistory(operation){
    const ctx=context();if(operation.account!==ctx.account||operation.method!=='actionHistory'||!operation.body?.preview_id||operation.body.confirmed!==true||!Array.isArray(operation.items))throw Error('原清理操作无法核对，请返回原账户。');
    // A known pending action is observed, never posted again. An unknown
    // response reuses precisely the persisted confirmation and key.
    let value;try{value=operation.actionId?await client.historyAction(operation.actionId):await client.actionHistory(operation.body,operation.key);}catch(error){return historyFailure(error,operation,ctx);}
    guard(ctx,{session:false});return finishHistory(value,operation,ctx);
  }
  const isHistoryMetadata=body=>['is_test','test_accepted','review_hold'].some(key=>Object.hasOwn(body||{},key));
  function checkMetadata(value,id,body){const session=record(value,'session');if(session?.id!==id||!Number.isSafeInteger(session.version)||session.version<=body.expected_version||Object.hasOwn(body,'title')&&session.title!==body.title||Object.hasOwn(body,'is_test')&&session.is_test!==body.is_test||body.test_accepted===true&&(session.is_test!==true||session.archive_pending!==true&&session.trash_state!=='trashed')||body.review_hold===true&&session.review_hold!==true)throw Error('归档或测试标记收据不完整，保留原操作供核对。');return session;}
  async function recoverMetadata(operation){
    const ctx=context(),target=operation.metadataTarget,id=target?.id||operation.sessionId;
    if(operation.args?.length!==1||operation.args[0]!==id||!id||target&&(target.account!==ctx.account||target.version!==operation.body.expected_version||Object.keys(operation.body).some(key=>!['expected_version','title','is_test'].includes(key))))throw Error('原标记目标不完整，请核对原操作。');
    const value=await client.patchSession(...operation.args,operation.body,operation.key);guard(ctx,{session:false});const session=checkMetadata(value,id,operation.body);writePending(null);guard(ctx,{session:false});
    // An idempotent acknowledgement settles the original command. It is not
    // a fresh project read: another client may have advanced this session.
    const newerLoaded=state.session?.id===session.id&&Number.isSafeInteger(state.session.version)&&state.session.version>session.version;
    if(!newerLoaded&&state.session?.id===session.id)emit({session});guard(ctx,{session:false});
    if(!newerLoaded)closeArchivedSession([session]);guard(ctx,{session:false});await list();return value;
  }
  function resetCardHistory(){cardPageSequence++;textPageSequence++;cardPageRequest=null;return emptyCardHistory();}
  async function readHistoryCard(summary,ctx,bundle){
    const response=await client.card(ctx.sessionId,summary.id);guard(ctx);const card=record(response,'card');
    if(card?.id!==summary.id||card.session_id&&card.session_id!==ctx.sessionId)throw Error('quickHistory.invalid');
    bundle.cards[card.id]={...card,created_at:summary.created_at??card.created_at};
    if(!card.current_revision_id&&!card.current_revision?.id)throw Error('quickHistory.invalid');
    const ids=new Set([card.current_revision_id,card.current_revision?.id,...(response.revisions||card.revisions||[]).map(item=>item.id)].filter(Boolean));
    for(const id of ids){const revision=state.revisions[id]||record(await client.revision(ctx.sessionId,id),'revision');guard(ctx);if(revision?.id!==id||revision.card_id!==card.id)throw Error('quickHistory.invalid');bundle.revisions[id]=revision;}
    for(const id of summary.submission_ids||[]){const submission=state.submissions[id]||record(await client.submission(ctx.sessionId,id),'submission');guard(ctx);if(submission?.id!==id||!submission.revision_id)throw Error('quickHistory.invalid');const revision=bundle.revisions[submission.revision_id]||state.revisions[submission.revision_id]||record(await client.revision(ctx.sessionId,submission.revision_id),'revision');guard(ctx);if(revision?.id!==submission.revision_id||revision.card_id!==card.id)throw Error('quickHistory.invalid');bundle.revisions[revision.id]=revision;bundle.submissions[id]=submission;}
    if(card.turn_id){const turn=record(await client.getTurn(ctx.sessionId,card.turn_id),'turn');guard(ctx);if(turn?.id!==card.turn_id||typeof turn.text!=='string')throw Error('quickHistory.invalid');bundle.turns[turn.id]=turn;}
  }
  async function loadCardHistory({reset=false,onBeforeCommit}={}){
    if(cardPageRequest)return cardPageRequest;
    if(!state.account||!state.session?.id)throw Error('quickHistory.scope');
    if(state.cardHistoryLoaded&&!state.cardHistoryHasMore&&!reset)return {added:0,start_reached:true};
    const ctx={...context(),readGeneration},sequence=++cardPageSequence,known=new Set(Object.keys(state.cards));
    let cursor=reset?null:state.cardHistoryCursor;
    emit({cardHistoryLoading:true,cardHistoryError:null});
    const request=(async()=>{await Promise.resolve();try{
      const bundle={cards:{},revisions:{},submissions:{},turns:{}},newIds=new Set(),seen=new Set();let page;
      // Deep links and forward events may already have loaded an older card.
      // Skip overlaps by card keyset, never by consuming raw event pages.
      for(let reads=0;reads<64;reads++){
        guard(ctx);if(sequence!==cardPageSequence)throw Error('quickHistory.scope');
        page=checkedCardPage(await client.cards(ctx.sessionId,{limit:CARD_HISTORY_PAGE_SIZE-newIds.size,...(cursor?{before_cursor:cursor}:{})}),ctx.sessionId,cursor,CARD_HISTORY_PAGE_SIZE-newIds.size);guard(ctx);
        await Promise.all(page.cards.map(summary=>readHistoryCard(summary,ctx,bundle)));guard(ctx);
        for(const card of page.cards)if(!known.has(card.id))newIds.add(card.id);
        cursor=page.next_cursor||null;
        if(newIds.size>=CARD_HISTORY_PAGE_SIZE||!page.has_more)break;
        if(seen.has(cursor))throw Error('quickHistory.invalid');seen.add(cursor);
      }
      if(page.has_more&&newIds.size<CARD_HISTORY_PAGE_SIZE)throw Error('quickHistory.invalid');
      guard(ctx);if(sequence!==cardPageSequence)throw Error('quickHistory.scope');
      onBeforeCommit?.();guard(ctx);
      // Keep any newer forward-poll projection read while this page was in
      // flight. Loading history must not roll a live job status backwards.
      emit({cards:{...bundle.cards,...state.cards},revisions:{...bundle.revisions,...state.revisions},submissions:{...bundle.submissions,...state.submissions},cardTurns:{...bundle.turns,...state.cardTurns},cardHistoryCursor:cursor,cardHistoryHasMore:page.has_more,cardHistoryLoaded:true,cardHistoryError:null});
      return {added:newIds.size,start_reached:!page.has_more};
    }catch(error){if(ctx.epoch===epoch&&ctx.sessionEpoch===sessionEpoch&&sequence===cardPageSequence)emit({cardHistoryError:{key:error.status===422?'quickHistory.cursor':error.message?.startsWith('quickHistory.')?error.message:'quickHistory.failed',status:Number.isInteger(error.status)?String(error.status):'read',reset:error.status===422}});throw error;}
    finally{if(ctx.epoch===epoch&&ctx.sessionEpoch===sessionEpoch&&sequence===cardPageSequence){cardPageRequest=null;emit({cardHistoryLoading:false});}}})();
    cardPageRequest=request;return request;
  }
  async function loadTextHistory({onBeforeCommit}={}){
    if(state.textHistoryLoading||!state.historyCursor)return;
    const ctx={...context(),readGeneration},sequence=++textPageSequence,known=new Set([...Object.values(state.cardTurns),...state.timeline.map(event=>event.record)].filter(turn=>turn?.text!==undefined).map(turn=>turn.id)),seen=new Set();
    let cursor=state.historyCursor,events=[];emit({textHistoryLoading:true,textHistoryError:null});
    try{while(cursor){if(seen.has(cursor)||seen.size>=64)throw Error('quickHistory.invalid');seen.add(cursor);const data=await client.timeline(ctx.sessionId,{before_cursor:cursor,limit:50});guard(ctx);events=merge(events,data.events||[]);cursor=data.before_cursor||data.next_cursor||null;if(events.some(event=>event.record?.text!==undefined&&!known.has(event.record.id)))break;}
      guard(ctx);if(sequence!==textPageSequence)throw Error('quickHistory.scope');onBeforeCommit?.();guard(ctx);emit({timeline:merge(state.timeline,events).sort((a,b)=>a.seq-b.seq),historyCursor:cursor});
    }catch(error){if(ctx.epoch===epoch&&ctx.sessionEpoch===sessionEpoch&&sequence===textPageSequence)emit({textHistoryError:{key:'quickHistory.failed',status:Number.isInteger(error.status)?String(error.status):'read'}});throw error;}
    finally{if(ctx.epoch===epoch&&ctx.sessionEpoch===sessionEpoch&&sequence===textPageSequence)emit({textHistoryLoading:false});}
  }
  async function loadResources(id,{initial=false}={}){
    const ctx={...context(),readGeneration};const [sessionData,materialsData,firstPage]=await Promise.all([client.session(id),client.materials(id),client.timeline(id,{...(initial?{}:{after_cursor:state.forwardCursor})})]);guard(ctx);
    const session=record(sessionData,'session');if(Number.isInteger(state.session?.version)&&session.version<state.session.version)throw Error('旧版本读取已丢弃，请刷新最新内容。');if(session.id!==id)throw Error('会话响应不匹配。');if(session.trash_state&&session.trash_state!=='active'){closeArchivedSession([session]);throw Error('这条创作已在回收站或删除处理中，请先核对或恢复原记录。');}
    let timelineData=firstPage,events=[...(firstPage.events||[])];
    // Drain forward pages without jumping to latest_seq: a busy Agent can
    // write more than one page between polls. Older history is user-paged.
    if(!initial)for(let page=0;page<5&&timelineData.has_more&&timelineData.next_cursor;page++){
      timelineData=await client.timeline(id,{after_cursor:timelineData.next_cursor});guard(ctx);events=merge(events,timelineData.events||[]);
    }
    // Automatic naming may finish without an authoring-version change. Merge
    // the current summary rather than reloading or discarding paged history.
    const sessions=state.sessions.map(item=>item.id===session.id?{...item,title:session.title??item.title,updated_at:session.updated_at??item.updated_at,version:session.version,...Object.fromEntries(['trash_state','trashable','blocked_reason','is_test','archive_pending','review_hold'].filter(key=>Object.hasOwn(session,key)).map(key=>[key,session[key]]))}:item);
    const clearedDraft=clearAcceptedDraft(storage,state.account,id,session.composer_reset);
    emit({session,sessions,...(clearedDraft?{draftReset:{account:state.account,sessionId:id,submissionId:session.composer_reset.submission_id}}:{}),materials:materialsData.materials||materialsData.bindings||[],timeline:initial?events:merge(state.timeline,events),historyCursor:initial?(timelineData.before_cursor||timelineData.next_cursor||null):state.historyCursor,forwardCursor:timelineData.after_cursor||timelineData.latest_cursor||state.forwardCursor,lastUpdated:Date.now(),error:''});
    if(initial&&typeof client.cards==='function')try{await loadCardHistory();}catch{guard(ctx);/* A scoped history error stays visible with retry; session reads continue. */}
    // Timeline holds durable creation references; current job state is read
    // separately even when its sequence has not changed.
    const newCardIds=initial?[]:events.filter(event=>event.type==='card.created').map(event=>event.record?.card_id||event.card_id).filter(Boolean),liveCardIds=[...new Set([...state.liveCardIds,...newCardIds])];
    if(newCardIds.length)emit({liveCardIds});
    const inWindow=id=>!state.cardHistoryLoaded||!!state.cards[id]||liveCardIds.includes(id);
    const cardIds=new Set(events.map(event=>event.record?.card_id||event.card_id).filter(id=>id&&inWindow(id)));
    // The cursor may advance before card/revision reads finish. Recover only
    // missing data referenced by loaded history, at most 20 cards per poll.
    const loadedCardIds=new Set([...Object.keys(state.cards),...state.timeline.map(event=>event.record?.card_id||event.card_id).filter(id=>id&&inWindow(id))]);
    let recovering=0;
    for(const cardId of loadedCardIds){const card=state.cards[cardId],current=card?.current_revision_id||card?.current_revision?.id;if(!cardIds.has(cardId)&&(!card||current&&!state.revisions[current])){cardIds.add(cardId);if(++recovering>=20)break;}}
    for(const revisionId of new Set(events.map(event=>event.record?.revision_id).filter(Boolean))){if(state.cardHistoryLoaded&&!state.revisions[revisionId])continue;const revision=state.revisions[revisionId]||record(await client.revision(id,revisionId),'revision');guard(ctx);emit({revisions:{...state.revisions,[revision.id]:revision}});if(revision.card_id&&inWindow(revision.card_id))cardIds.add(revision.card_id);}
    const eventPreflights=Object.fromEntries(events.filter(event=>event.type==='preflight.completed'&&event.record?.id).map(event=>[event.record.id,event.record]));
    if(Object.keys(eventPreflights).length)emit({preflights:{...state.preflights,...eventPreflights}});
    for(const cardId of cardIds){await loadCard(cardId,ctx);}
    const pendingTurns=new Map([...Object.values(state.cardTurns),...state.timeline.map(event=>event.record)].filter(turn=>turn?.text!==undefined&&['pending','running'].includes(turn.status)).map(turn=>[turn.id,turn]));
    for(const turnId of pendingTurns.keys()){const turn=record(await client.getTurn(id,turnId),'turn');guard(ctx);emit({timeline:state.timeline.map(event=>event.record?.id===turnId?{...event,record:turn}:event),...(state.cardTurns[turnId]?{cardTurns:{...state.cardTurns,[turnId]:turn}}:{})});}
    const submissionRefs=values=>values.filter(event=>(event.type.startsWith('submission.')||event.type.startsWith('item.'))&&(!state.cardHistoryLoaded||state.submissions[event.record?.id]||state.revisions[event.record?.revision_id])).map(event=>event.record?.id).filter(Boolean);
    const submissionIds=new Set([...Object.keys(state.submissions),...submissionRefs(events)]);
    // A failed dependent GET must not lose the durable submission reference
    // after the forward cursor advances. Recover only loaded history.
    let recoveringSubmissions=0;
    for(const submissionId of submissionRefs(state.timeline))if(!state.submissions[submissionId]&&!submissionIds.has(submissionId)){submissionIds.add(submissionId);if(++recoveringSubmissions>=20)break;}
    for(const submissionId of submissionIds){const value=record(await client.submission(id,submissionId),'submission');guard(ctx);const cleared=clearAcceptedDraft(storage,state.account,id,value.composer_reset);emit({submissions:{...state.submissions,[value.id]:value},...(cleared?{draftReset:{account:state.account,sessionId:id,submissionId:value.id}}:{})});}
    const importIds=new Set([...Object.keys(state.imports),...events.filter(event=>event.type.startsWith('result_import.')).map(event=>event.record?.id).filter(Boolean)]);
    for(const importId of importIds){const value=record(await client.getResultImport(id,importId),'import');guard(ctx);emit({imports:{...state.imports,[value.id]:value}});}
    return session;
  }
  async function loadCard(cardId,ctx=context()){
    const response=await client.card(ctx.sessionId,cardId);guard(ctx);const card=record(response,'card');emit({cards:{...state.cards,[card.id]:card}});
    const ids=new Set([card.current_revision_id,card.current_revision?.id,...(response.revisions||card.revisions||[]).map(item=>item.id)].filter(Boolean));
    for(const id of ids){const revision=record(await client.revision(ctx.sessionId,id),'revision');guard(ctx);emit({revisions:{...state.revisions,[revision.id]:revision}});}
    return card;
  }
  async function focusCard({cardId,revisionId,submissionId}={}){
    const ctx={...context(),readGeneration},valid=value=>typeof value==='string'&&value.length>0&&value.length<=160;
    if(!ctx.account||!ctx.sessionId||![cardId,revisionId,submissionId].some(valid)
        ||[cardId,revisionId,submissionId].some(value=>value!=null&&!valid(value)))throw Error('quickHistory.scope');
    const bundle={cards:{},revisions:{},submissions:{},turns:{}};
    if(submissionId){const value=state.submissions[submissionId]||record(await client.submission(ctx.sessionId,submissionId),'submission');guard(ctx);
      if(value?.id!==submissionId||!valid(value.revision_id)||revisionId&&revisionId!==value.revision_id)throw Error('quickHistory.invalid');
      revisionId=value.revision_id;bundle.submissions[value.id]=value;}
    if(revisionId){const value=state.revisions[revisionId]||record(await client.revision(ctx.sessionId,revisionId),'revision');guard(ctx);
      if(value?.id!==revisionId||!valid(value.card_id)||cardId&&cardId!==value.card_id)throw Error('quickHistory.invalid');
      cardId=value.card_id;bundle.revisions[value.id]=value;}
    if(!valid(cardId))throw Error('quickHistory.invalid');
    await readHistoryCard({id:cardId,created_at:state.cards[cardId]?.created_at},ctx,bundle);guard(ctx);
    const cards={...state.cards};for(const [id,value]of Object.entries(bundle.cards))if(!cards[id]||!Number.isInteger(cards[id].version)||value.version>=cards[id].version)cards[id]=value;
    emit({cards,revisions:{...bundle.revisions,...state.revisions},submissions:{...bundle.submissions,...state.submissions},cardTurns:{...bundle.turns,...state.cardTurns}});
    return {card_id:cardId,revision_id:revisionId||cards[cardId].current_revision_id,submission_id:submissionId||null};
  }
  async function refresh(){if(!state.account||!state.session||refreshing)return;refreshing=true;const ctx={...context(),readGeneration};try{await loadResources(state.session.id);}catch(error){try{guard(ctx);emit({error:'云端状态暂未更新，保留原任务；稍后自动核对。'});}catch{}throw error;}finally{refreshing=false;}}
  async function act(fn,{success}={}){if(state.busy)throw Error('原操作仍在处理中，请等待。');const ctx=context();emit({busy:true,error:'',notice:''});try{const value=await fn();guard(ctx,{session:false});if(success)emit({notice:success});return value;}catch(error){if(ctx.epoch===epoch)emit({error:error.message||'操作没有完成。请核对原记录。'});throw error;}finally{if(ctx.epoch===epoch)emit({busy:false});}}
  function transferIdentity(value,operation){return value&&typeof value.id==='string'&&(!operation.transferId||operation.transferId===value.id)&&value.source_session_id===operation.args[0]&&value.item_id===operation.args[1]&&value.project_id===operation.body.target_project_id&&value.expected_project_version===operation.body.expected_project_version&&value.parent_id===operation.body.parent_id&&value.seed_locked===true;}
  function closedTransfer(value,operation){return transferIdentity(value,operation)&&value.status==='blocked'&&value.no_shot_committed===true&&value.error_code==='target_version_conflict'&&typeof value.shot_id==='string'&&value.web_url===null;}
  function finishTransfer(value,operation){
    if(!transferIdentity(value,operation))throw Error('takes.recipeMissing');
    if(value.status!=='completed'){writePending({...operation,transferId:value.id,...(closedTransfer(value,operation)?{closedReceipt:value}:{})});return value;}
    if(typeof value.shot_id!=='string'||!Number.isInteger(value.project_version)||value.project_version<=operation.body.expected_project_version||typeof value.web_url!=='string')throw Error('takes.recipeMissing');
    const link=new URL(value.web_url,'https://local.invalid');if(link.origin!=='https://local.invalid'||link.pathname!=='/'||link.searchParams.get('project')!==value.project_id||link.searchParams.get('entity')!==value.shot_id)throw Error('takes.recipeMissing');
    writePending(null);return value;
  }
  async function annotateTake(operation,ctx){
    guard(ctx);try{const value=await client.starTake(...operation.args,operation.body,operation.key);guard(ctx);
      if(value?.take_id!==operation.args[1]||!Number.isSafeInteger(value.version)||value.version<=operation.body.expected_version||Object.hasOwn(operation.body,'title')&&value.title!==operation.body.title||Object.hasOwn(operation.body,'starred')&&value.starred!==operation.body.starred)throw Error('trash.failed');
      if(stableJSON(state.pending)!==stableJSON(operation))throw Error('trash.pending');writePending(null);return value;
    }catch(error){guard(ctx);const proof=error.detail?.detail||error.detail;if(proof?.resource==='quick_chat_take'&&proof.code==='version_conflict'&&proof.session_id===operation.args[0]&&proof.take_id===operation.args[1]&&proof.no_mutation_committed===true&&proof.request_hash===await digest(operation.body)){guard(ctx);if(stableJSON(state.pending)===stableJSON(operation))writePending(null);}throw error;}
  }
  async function command(method,args,body,{sessionId=state.session?.id,draft,validate,metadataTarget}={}){
    if(state.pending)throw Error('有原操作结果待核对，请先恢复原操作，不要重复提交。');
    readGeneration++;
    const ctx=context(),operation={id:uuid(),method,args,body:clone(body),sessionId,account:state.account,key:'quick-'+uuid(),created_at:Date.now(),...(draft?{draft:clone(draft)}:{}),...(metadataTarget?{metadataTarget:clone(metadataTarget)}:{})};writePending(operation);if(method==='starTake')return annotateTake(operation,ctx);
    try{guard(ctx,{session:!!sessionId});const value=await client[method](...args,body,operation.key);guard(ctx,{session:!!sessionId});if(method==='takeToStory')return finishTransfer(value,operation);if(validate)validate(value);if(method==='turn'&&draft)rememberDraftOrigin(storage,state.account,sessionId,draft,record(value,'turn').id);writePending(null);return value;}
    catch(error){guard(ctx,{session:!!sessionId});if(method!=='takeToStory'&&error.status>=400&&error.status<500&&![408,429].includes(error.status))writePending(null);throw error;}
  }
  return {client,subscribe(fn){listeners.add(fn);return()=>listeners.delete(fn);},getState:()=>state,
    async setAccount(account){if(account===state.account)return;epoch++;sessionEpoch++;historyEpoch++;listSequence={active:0,trash:0};availabilitySequence++;availabilityRequest=null;client.setAccount(account);emit({account,schema:null,capabilities:null,availability:null,availabilityChecking:false,operatorAccess:false,sessions:[],sessionsCursor:null,session:null,materials:[],timeline:[],historyCursor:null,forwardCursor:null,cards:{},revisions:{},preflights:{},submissions:{},imports:{},busy:false,error:'',notice:'',pending:readPending(account),lastUpdated:null,draftReset:null,...initialHistory(),...resetCardHistory()});if(!account)return;const ctx=context();const availabilityLoad=refreshAvailability().catch(()=>null);const operatorLoad=readOperatorAccess();try{const [schema,capabilities]=await Promise.all([client.schema(),client.capabilities()]);guard(ctx,{session:false});emit({schema,capabilities});await list();await Promise.all([availabilityLoad,operatorLoad]);}catch(error){try{guard(ctx,{session:false});emit({error:error.message});}catch{}}},
    list:options=>act(()=>list(options)),
    historyView:view=>act(async()=>{if(!['active','trash'].includes(view))throw Error('列表类型不正确。');invalidateHistory({historyView:view,historySelection:[],historyReceipt:null});if(view==='trash')await list({view});}),
    hideTests:value=>act(async()=>{invalidateHistory({hideTests:!!value,historySelection:[]});await list();}),
    selectHistory:ids=>{if(state.busy)throw Error('请等待原操作完成。');const rows=state.historyView==='active'?state.sessions:state.trashSessions;if(ids.length)historySelection(rows,ids,state.historyView);invalidateHistory({historySelection:[...ids]});},
    dismissHistoryPreview:()=>invalidateHistory(),
    previewHistory:action=>act(async()=>{
      if(state.pending)throw Error('请先核对原操作，未建立新的清理确认。');
      const view=action==='trash'?'active':'trash';if(view!==state.historyView||!['trash','restore','purge','empty_trash'].includes(action))throw Error('请在对应列表重新选择。');
      const sessions=action==='empty_trash'?[]:historySelection(view==='active'?state.sessions:state.trashSessions,state.historySelection,view),body={action,sessions};
      invalidateHistory({historyReceipt:null});const ctx=context(),sequence=historyEpoch,value=await client.previewHistory(body);guard(ctx,{session:false});
      if(sequence!==historyEpoch)throw Error('账户、筛选或选择已改变，请重新预览。');
      const preview=checkedHistoryPreview(value,body);emit({historyPreview:{...preview,request:body,account:state.account,epoch:sequence}});return preview;
    }),
    confirmHistory:()=>act(async()=>{
      if(state.pending)throw Error('请先核对原操作，不要再次确认。');const preview=state.historyPreview;
      if(!preview||preview.account!==state.account||preview.epoch!==historyEpoch||preview.expires_at<=Date.now()/1000||!preview.items.length||preview.items.some(item=>!item.eligible))throw Error('预览已失效或含不可操作的记录，请重新核对。');
      if(preview.action!=='empty_trash'){const selected=historySelection(state.historyView==='active'?state.sessions:state.trashSessions,state.historySelection,state.historyView);if(stableJSON(selected)!==stableJSON(preview.request.sessions))throw Error('所选记录已更新，请重新预览。');}
      const ctx=context(),operation={id:uuid(),method:'actionHistory',args:[],body:{preview_id:preview.preview_id,confirmed:true},sessionId:null,account:state.account,key:'quick-history-'+uuid(),created_at:Date.now(),action:preview.action,items:preview.items.map(({id,expected_version})=>({id,expected_version}))};
      readGeneration++;writePending(operation);guard(ctx,{session:false});
      let value;try{value=await client.actionHistory(operation.body,operation.key);guard(ctx,{session:false});}
      catch(error){return historyFailure(error,operation,ctx);}
      // A later list refresh is not the confirmation result. In particular,
      // a failed refresh must not erase an accepted pending purge journal.
      return finishHistory(value,operation,ctx);
    }),
    open:(id,{revisionId,cardId,submissionId}={})=>act(async()=>{sessionEpoch++;client.reset();emit({session:{id},materials:[],timeline:[],cards:{},revisions:{},preflights:{},submissions:{},imports:{},historyCursor:null,forwardCursor:null,error:'',notice:'',draftReset:null,...resetCardHistory()});const session=await loadResources(id,{initial:true}),ctx=context();if(submissionId){const submission=record(await client.submission(id,submissionId),'submission');guard(ctx);emit({submissions:{...state.submissions,[submission.id]:submission}});revisionId=revisionId||submission.revision_id;}if(revisionId){const revision=record(await client.revision(id,revisionId),'revision');guard(ctx);emit({revisions:{...state.revisions,[revision.id]:revision}});cardId=cardId||revision.card_id;}if(cardId)await loadCard(cardId,ctx);return session;}),
    create:body=>act(async()=>{const response=await command('createSession',[],body,{sessionId:null}),session=record(response,'session');sessionEpoch++;emit({session,materials:[],timeline:[],cards:{},revisions:{},preflights:{},submissions:{},imports:{},historyCursor:null,forwardCursor:null,...resetCardHistory()});await list();await loadResources(session.id,{initial:true});return session;}),
    patch:patch=>act(async()=>{const id=state.session.id,body={expected_version:state.session.version,...patch},metadata=isHistoryMetadata(patch),result=await command('patchSession',[id],body,metadata?{validate:value=>checkMetadata(value,id,body)}:{}),session=record(result,'session');emit({session,sessions:state.sessions.map(item=>item.id===session.id?{...item,title:session.title,updated_at:session.updated_at,version:session.version,is_test:session.is_test,review_hold:session.review_hold,trash_state:session.trash_state}:item)});if(metadata){closeArchivedSession([session]);invalidateHistory({historySelection:[]});await list();}return result;},{success:'创作设置已保存；历史卡片保持原版。'}),
    patchRow:(target,patch)=>act(async()=>{
      try{currentConversationRow(state,target);}catch{throw Error(translate(readLocale(storage,state.account),'quickSidebar.changed'));}
      if(!patch||!Object.keys(patch).length||Object.keys(patch).some(key=>!['title','is_test'].includes(key))||Object.hasOwn(patch,'title')&&(typeof patch.title!=='string'||!patch.title.trim()||patch.title.length>160)||Object.hasOwn(patch,'is_test')&&typeof patch.is_test!=='boolean')throw Error(translate(readLocale(storage,state.account),'quickSidebar.invalid'));
      const body={expected_version:target.version,...patch},result=await command('patchSession',[target.id],body,{sessionId:null,metadataTarget:{account:target.account,id:target.id,version:target.version},validate:value=>checkMetadata(value,target.id,body)}),session=record(result,'session');
      if(state.session?.id===session.id)emit({session});
      invalidateHistory({historySelection:[]});await list();return result;
    }),
    saveMaterials:bindings=>act(async()=>{const ready=bindings.filter(binding=>{const previous=state.materials.find(item=>item.binding_id===binding.binding_id),asset=previous?.asset;if(asset?.status&&asset.status!=='ready')return false;return !(previous?.version===0&&binding.enabled===false&&stableJSON(binding)===stableJSON(bindingPayload(previous)));});const result=await command('saveMaterials',[state.session.id],{expected_version:state.session.version,bindings:ready});await loadResources(state.session.id);return result;},{success:'本轮材料选择已保存；历史原件保留。'}),
    upload:(file,assetId=uuid())=>act(async()=>{if(state.pending)throw Error('先核对原操作。');readGeneration++;const ctx=context(),key='quick-upload-'+assetId;writePending({id:assetId,method:'uploadMaterial',args:[ctx.sessionId],body:{client_asset_id:assetId,file_name:file.name},sessionId:ctx.sessionId,account:ctx.account,key,created_at:Date.now()});try{const value=await client.uploadMaterial(ctx.sessionId,file,assetId,key);guard(ctx);writePending(null);await loadResources(ctx.sessionId);return value;}catch(error){guard(ctx);if(error.status>=400&&error.status<500&&![408,429].includes(error.status))writePending(null);throw error;}},{success:'文件接收结果已记录；是否可生成以素材校验与预检为准。'}),
    resumeMaterial:assetId=>act(async()=>{const result=await command('resumeMaterial',[state.session.id,assetId],{});await loadResources(state.session.id);return result;},{success:'已核对原素材处理收据，没有重新上传原文件。'}),
    refreshAvailability,
    refresh:()=>act(async()=>{const ctx=context(),[schema,capabilities]=await Promise.all([client.schema(),client.capabilities()]);guard(ctx,{session:false});emit({schema,capabilities});await refreshAvailability().catch(()=>null);await list();return refresh();}),poll:()=>state.busy?Promise.resolve():refresh(),
    history:options=>loadCardHistory(options),focusCard,
    textHistory:options=>loadTextHistory(options),
    turn:(text,model_id,assistant_mode='assist',{createCard=false,draft}={})=>act(async()=>{if(createCard&&assistant_mode!=='none')throw Error('直接出卡只用于原输入模式，不替代助手回复。');const result=await command('turn',[state.session.id],{expected_version:state.session.version,text,model_id,assistant_mode,...(createCard?{create_card:true}:{})},{draft});await loadResources(state.session.id);return result;},{success:'这一轮已保存；只有确认任务卡才会生成视频。'}),
    createCard:body=>act(async()=>{const result=await command('createCard',[state.session.id],body);await loadResources(state.session.id);const card=record(result,'card');if(card.id)await loadCard(card.id);return result;},{success:'任务卡已保存，尚未提交视频生成。'}),
    reviseCard:(card,body)=>act(async()=>{const result=await command('reviseCard',[state.session.id,card.id],{expected_card_version:card.version,...body});await loadCard(card.id);await loadResources(state.session.id);return result;},{success:'新版本已保存；旧版本及已提交输入保留。'}),
    readTakes:async(card,options={})=>{const ctx=context(),value=await client.takes(ctx.sessionId,card.id,options);guard(ctx);return value;},
    readPreferences:async()=>{const ctx=context(),value=await client.preferences();guard(ctx,{session:false});return value;},
    savePreferences:body=>act(()=>command('savePreferences',[],body,{sessionId:null})),
    renameTake:(take,title)=>act(()=>command('starTake',[state.session.id,take.id],{expected_version:take.annotation.version,title})),
    starTake:(take,starred)=>act(()=>command('starTake',[state.session.id,take.id],{expected_version:take.annotation.version,starred})),
    again:(card,take)=>act(async()=>{const result=await command('again',[state.session.id,card.id],{expected_card_version:card.version,source_revision_id:take.revision_id,item_id:take.id,copies:1});await loadCard(card.id);await loadResources(state.session.id);return result;}),
    acknowledgeBlockedTransfer:()=>act(async()=>{const operation=state.pending,ctx=context();if(operation?.method!=='takeToStory'||operation.account!==ctx.account||operation.sessionId!==ctx.sessionId||!operation.transferId)throw Error('takes.wait');const value=await client.storyTransfer(operation.sessionId,operation.transferId);guard(ctx);if(!closedTransfer(value,operation))throw Error('takes.wait');writePending(null);return value;}),
    resumeStoryTransfer:()=>act(async()=>{const operation=state.pending,ctx=context();if(operation?.method!=='takeToStory'||operation.account!==ctx.account||operation.sessionId!==ctx.sessionId||!operation.transferId)throw Error('takes.wait');const value=await client.takeToStory(...operation.args,operation.body,operation.key);guard(ctx);return finishTransfer(value,operation);}),
    takeToStory:(take,body)=>act(()=>command('takeToStory',[state.session.id,take.id],body)),
    preflight:(revision,scope={})=>act(async()=>{await checkGeneration(revision);const response=await command('preflight',[state.session.id,revision.id],{capabilities_version:state.capabilities?.capabilities_version,revision_hash:revision.input_hash,...scope});const value=record(response,'preflight');emit({preflights:{...state.preflights,[value.id]:value}});return value;},{success:'输入与执行条件已检查。'}),
    submit:(revision,preflight)=>act(async()=>{await checkGeneration(revision);const response=await command('submit',[state.session.id,revision.id],{preflight_id:preflight.id,revision_hash:revision.input_hash,confirmed:true});const value=record(response,'submission');emit({submissions:{...state.submissions,[value.id]:value}});await loadResources(state.session.id);return value;},{success:'生成确认已接受，查看原任务进度，无需重复提交。'}),
    cancel:(submission,item_ids)=>act(async()=>{const value=await command('cancelSubmission',[state.session.id,submission.id],item_ids?{item_ids}:{});await refresh();return value;},{success:'取消意愿已记录；以最终停止状态为准，已执行部分可能计费。'}),
    retry:(submission,item,preflight)=>act(async()=>{await checkGeneration(state.revisions[submission.revision_id]||{});const result=await command('retryItem',[state.session.id,submission.id,item.id],{retry_of_execution_id:item.current_execution_id||item.execution?.id,fresh_preflight_id:preflight.id,confirmed:true});await refresh();return result;},{success:'原失败项的重试已记录，成功份保持不变。'}),
    resume:(submission,item,preflight)=>act(async()=>{await checkGeneration(state.revisions[submission.revision_id]||{});const result=await command('resumeAdmission',[state.session.id,submission.id],{item_ids:[item.id],fresh_preflight_id:preflight.id,confirmed:true});await refresh();return result;},{success:'原未准入项已恢复；未创建第二批。'}),
    importResult:(artifact,purpose='reference')=>act(async()=>{const response=await command('importResult',[state.session.id],{source_artifact_id:artifact.id,purpose});const value=record(response,'import');emit({imports:{...state.imports,[value.id]:value}});await loadResources(state.session.id);return value;},{success:'已记录结果导入；校验完成后才能作为参考。'}),
    acknowledge:turn=>act(async()=>{const result=await command('acknowledgeUnknown',[state.session.id,turn.id],{acknowledged:true});await loadResources(state.session.id);return result;}),
    async recover(){return act(async()=>{const operation=state.pending;if(!operation||operation.corrupt)throw Error('原操作记录无法读取，请管理员核对，不要重新创建任务。');if(operation.account!==state.account)throw Error('请登录原操作账户。');if(operation.method==='actionHistory')return recoverHistory(operation);if(operation.method==='patchSession'&&(isHistoryMetadata(operation.body)||operation.metadataTarget))return recoverMetadata(operation);if(operation.sessionId&&operation.sessionId!==state.session?.id)throw Error('请先打开原操作会话。');readGeneration++;const ctx=context();if(operation.method==='starTake')return annotateTake(operation,ctx);if(operation.method==='takeToStory'){const result=operation.transferId?await client.storyTransfer(operation.sessionId,operation.transferId):await client.takeToStory(...operation.args,operation.body,operation.key);guard(ctx);return finishTransfer(result,operation);}if(operation.sessionId)await loadResources(operation.sessionId);guard(ctx,{session:!!operation.sessionId});if(operation.method==='uploadMaterial'){const catalog=await client.sessionAssets(operation.sessionId,operation.body.client_asset_id);guard(ctx);if(!(catalog.assets||[]).length)throw Error('原上传收据暂未查到，保持原标识；未自动重新上传。请管理员核对后恢复。');writePending(null);await loadResources(operation.sessionId);return catalog;}const result=await client[operation.method](...operation.args,operation.body,operation.key);guard(ctx,{session:!!operation.sessionId});if(operation.method==='turn'&&operation.draft)rememberDraftOrigin(storage,state.account,operation.sessionId,operation.draft,record(result,'turn').id);writePending(null);if(operation.sessionId)await loadResources(operation.sessionId);else await list();return result;},{success:'已使用原标识核对操作；没有建立新的确认。'});},
    destroy(){epoch++;sessionEpoch++;availabilitySequence++;availabilityRequest=null;client.reset();listeners.clear();},
  };
}
