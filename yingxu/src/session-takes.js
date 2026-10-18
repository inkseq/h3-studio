const id=value=>typeof value==='string'&&/^[A-Za-z0-9_-]{1,160}$/.test(value);
export function checkedSessionTakes(value,sessionId,previousCursor=null){
 if(value?.projection!=='original_takes_v1'||value.session_id!==sessionId||value.includes_trashed!==true||!Number.isSafeInteger(value.total_count)||value.total_count<0||!Array.isArray(value.takes)||value.takes.length>40||value.before_cursor!=null&&(typeof value.before_cursor!=='string'||!value.before_cursor||value.before_cursor.length>512||value.before_cursor===previousCursor))throw Error('sessionTakes.invalid');
 const seen=new Set();
 for(const row of value.takes){if(!id(row.id)||!id(row.card_id)||!id(row.revision_id)||seen.has(row.id)||typeof row.status!=='string'||typeof row.annotation?.starred!=='boolean'||!Array.isArray(row.artifacts))throw Error('sessionTakes.invalid');seen.add(row.id);}
 if(!value.takes.length&&value.before_cursor)throw Error('sessionTakes.invalid');
 return value;
}
export function takeStatusGroup(status){
 if(['claimed','submitting','running','collecting','checking'].includes(status))return 'running';
 if(['queued','waiting_capacity','pending_admission','active','planned'].includes(status))return 'queued';
 if(['failed','admission_failed','admission_blocked','blocked'].includes(status))return 'failed';
 return ['draft','succeeded','cancelled'].includes(status)?status:'unknown';
}
export function sessionTakeRows(takes,{status='all',starred=false}={}){
 return takes.filter(row=>row.trash_state==='active'&&(!starred||row.annotation.starred)&&(status==='all'||takeStatusGroup(row.status)===status));
}
export async function readSessionTakes(client,sessionId,{signal,isCurrent,onPage}){
 let cursor=null;const cursors=new Set();
 do{
  if(signal?.aborted||!isCurrent())return false;
  const page=checkedSessionTakes(await client.sessionTakes(sessionId,{before_cursor:cursor,signal}),sessionId,cursor);
  if(signal?.aborted||!isCurrent())return false;
  cursor=page.before_cursor;
  if(cursor&&cursors.has(cursor))throw Error('sessionTakes.invalid');
  if(cursor)cursors.add(cursor);
  onPage(page);
 }while(cursor);
 return true;
}
