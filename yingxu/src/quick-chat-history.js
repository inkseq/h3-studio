import {mergeTimelineTurns} from './quick-chat-model.js';

export const CARD_HISTORY_PAGE_SIZE=10;
export const emptyCardHistory=()=>({cardHistoryCursor:null,cardHistoryLoaded:false,cardHistoryHasMore:false,cardHistoryLoading:false,cardHistoryError:null,textHistoryLoading:false,textHistoryError:null,cardTurns:{},liveCardIds:[]});
// A saved conversation is not an empty new creation while auth, selection or
// its first card page is pending. Errors must remain actionable, never shimmer.
export function initialThreadLoading({requestedSession,sessionReady,account,readyAccount,state,entryCount}){
  if(entryCount||state.error||state.cardHistoryError)return false;
  if(!sessionReady)return !!requestedSession;
  if(!account)return false;
  if(readyAccount!==account)return !!(requestedSession||state.session);
  if(requestedSession&&requestedSession!==state.session?.id)return true;
  return !!state.session&&!state.cardHistoryLoaded;
}
const id=value=>typeof value==='string'&&value.length>0&&value.length<=160;
export function checkedCardPage(value,sessionId,beforeCursor,limit=CARD_HISTORY_PAGE_SIZE){
  if(value?.session_id!==sessionId||value.limit!==limit||!Array.isArray(value.cards)||value.cards.length>limit||typeof value.has_more!=='boolean'||value.start_reached!==!value.has_more||value.has_more&&(typeof value.next_cursor!=='string'||!value.next_cursor||value.next_cursor.length>4096||value.next_cursor===beforeCursor)||!value.has_more&&value.next_cursor!=null)throw Error('quickHistory.invalid');
  const seen=new Set();let previous;
  for(const card of value.cards){
    if(!id(card.id)||!Number.isFinite(card.created_at)||seen.has(card.id)||card.submission_ids!=null&&(!Array.isArray(card.submission_ids)||card.submission_ids.some(value=>!id(value))))throw Error('quickHistory.invalid');
    const next=[card.created_at,card.id];if(previous&&(next[0]<previous[0]||next[0]===previous[0]&&next[1]<previous[1]))throw Error('quickHistory.invalid');
    previous=next;seen.add(card.id);
  }
  if(!value.cards.length&&value.has_more)throw Error('quickHistory.invalid');
  return value;
}

// Timeline sequence is only meaningful for event records. Card pages have an
// immutable creation timestamp/id key and must not jump when old events arrive.
export function quickThreadEntries(timeline,cards,cardTurns={}){
  const turns=new Map(Object.values(cardTurns).map(turn=>[turn.id,turn]));
  for(const turn of mergeTimelineTurns(timeline))turns.set(turn.id,turn);
  const linked=new Map(Object.values(cards).filter(card=>card.turn_id).map(card=>[card.turn_id,card.id]));
  const shown=new Set(),entries=[];
  for(const original of turns.values()){
    const cardId=original.card_id||linked.get(original.id),turn=cardId?{...original,card_id:cardId}:original;
    if(cardId)shown.add(cardId);entries.push({kind:'turn',record:turn});
  }
  for(const card of Object.values(cards))if(!shown.has(card.id))entries.push({kind:'card',record:card});
  const eventTimes=new Map(timeline.filter(event=>event.record?.id&&Number.isFinite(event.created_at)).map(event=>[event.record.id,event.created_at]));
  const time=entry=>Number.isFinite(entry.record.created_at)?entry.record.created_at:eventTimes.get(entry.record.id)??(entry.kind==='turn'?cards[entry.record.card_id]?.created_at:undefined)??Infinity;
  return entries.sort((a,b)=>time(a)-time(b)||(String(a.record.id)<String(b.record.id)?-1:String(a.record.id)>String(b.record.id)?1:0));
}

// Anchor an existing visible row, not total height: newly loaded media may
// change height and the history toolbar can change its own label/error size.
export function captureThreadAnchor(container){
  if(!container)return null;const top=container.getBoundingClientRect().top;
  const row=[...container.querySelectorAll('[data-history-entry]')].find(node=>node.getBoundingClientRect().bottom>top);
  return {element:row,key:row?.getAttribute?.('data-history-entry')||null,offset:row?row.getBoundingClientRect().top-top:0,scrollTop:container.scrollTop,scrollHeight:container.scrollHeight};
}
export function restoreThreadAnchor(container,anchor){
  if(!container||!anchor)return;
  const row=anchor.element?.isConnected&&container.contains(anchor.element)?anchor.element:anchor.key?[...container.querySelectorAll('[data-history-entry]')].find(node=>node.getAttribute('data-history-entry')===anchor.key):null;
  if(row)container.scrollTop+=row.getBoundingClientRect().top-container.getBoundingClientRect().top-anchor.offset;
  else container.scrollTop=anchor.scrollTop+container.scrollHeight-anchor.scrollHeight;
}
