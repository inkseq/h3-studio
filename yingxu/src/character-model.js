// Shared character identity stays on the entity; looks and casting are explicit references.
export const gallerySlots = Object.freeze({front:'正面',side:'侧面',full:'全身'});
const find=(p,id)=>p.entities.find(e=>e.id===id);
const castOf=e=>Array.isArray(e?.data?.cast)?e.data.cast:[];
const looksOf=e=>Array.isArray(e?.data?.looks)?e.data.looks:[];
export function effectiveLook(project,shot,characterId){
  if(!shot||shot.type!=='shot')return null;
  const own=castOf(shot).find(c=>c.characterId===characterId);
  const inherited=castOf(find(project,shot.parentId)).find(c=>c.characterId===characterId);
  const cast=own||inherited;
  return cast?{...cast,inherited:!own,sourceId:own?shot.id:shot.parentId}:null;
}
export function characterPath(project,id){
  const path=[],seen=new Set();let entity=find(project,id);
  while(entity&&!seen.has(entity.id)){seen.add(entity.id);path.unshift(entity.title||'未命名');entity=find(project,entity.parentId);}
  return path.join(' / ');
}
export function lookReferences(project,characterId,lookId){return project.entities.filter(e=>['scene','shot'].includes(e.type)&&castOf(e).some(c=>c.characterId===characterId&&c.lookId===lookId));}
export function lookShots(project,characterId,lookId){return project.entities.filter(e=>e.type==='shot'&&effectiveLook(project,e,characterId)?.lookId===lookId);}
function signatures(project,characterId){return new Map(project.entities.filter(e=>e.type==='shot').map(s=>[s.id,effectiveLook(project,s,characterId)?.lookId||'']));}
function recordImpact(project,characterId,shotIds,reason){
  const character=find(project,characterId);
  for(const id of shotIds){const shot=find(project,id);if(!shot)continue;shot.status='review';shot.data.characterReview={characterId,lookId:effectiveLook(project,shot,characterId)?.lookId||'',reason};}
  project.journey={...project.journey,characterChange:{characterId,characterName:character?.title||'角色',shotIds:[...shotIds],reason}};
  return {ok:true,affected:[...shotIds]};
}
function changes(project,characterId,before,reason){return recordImpact(project,characterId,project.entities.filter(e=>e.type==='shot'&&before.get(e.id)!==(effectiveLook(project,e,characterId)?.lookId||'')).map(e=>e.id),reason);}
export function addLook(project,characterId,{id,name,description='',gallery={}}){
  const character=find(project,characterId);if(character?.type!=='character')return {ok:false,error:'这个角色已经不存在，请重新选择。'};
  if(!name.trim())return {ok:false,error:'请给造型起个名字。'};
  if(looksOf(character).some(l=>l.name===name.trim()))return {ok:false,error:'这个造型名称已经存在，请使用能区分场次的名称。'};
  character.data.looks=[...looksOf(character),{id,name:name.trim().slice(0,100),description,gallery:{...gallery},version:1}];
  character.version+=1;return {ok:true,affected:[]};
}
export function editLook(project,characterId,lookId,patch){
  const character=find(project,characterId),look=looksOf(character).find(l=>l.id===lookId);
  if(!look)return {ok:false,error:'这个造型已经不存在，请重新选择。'};
  if(patch.name!==undefined){if(!patch.name.trim())return {ok:false,error:'造型名称不能留空，原名称已保留。'};if(looksOf(character).some(l=>l.id!==lookId&&l.name===patch.name.trim()))return {ok:false,error:'这个造型名称已经存在，请换一个名称。'};}
  if(patch.gallery){for(const [slot,id] of Object.entries(patch.gallery)){if(!Object.hasOwn(gallerySlots,slot)||id&&find(project,id)?.type!=='image')return {ok:false,error:'请选择素材库中存在的图片。'};}}
  const updated={...look,...patch,name:patch.name?.trim()||look.name,gallery:{...look.gallery,...patch.gallery}};
  if(JSON.stringify(updated)===JSON.stringify(look))return {ok:true,affected:[]};
  Object.assign(look,updated,{version:look.version+1});character.version+=1;
  return recordImpact(project,characterId,lookShots(project,characterId,lookId).map(e=>e.id),`修改造型「${look.name}」`);
}
export function bindLook(project,targetId,characterId,lookId){
  const target=find(project,targetId),character=find(project,characterId);
  if(!['scene','shot'].includes(target?.type)||character?.type!=='character')return {ok:false,error:'角色或场戏已被删除，请重新选择。'};
  if(lookId&&!looksOf(character).some(l=>l.id===lookId))return {ok:false,error:'这个造型已被删除，请重新选择。'};
  const before=signatures(project,characterId),rest=castOf(target).filter(c=>c.characterId!==characterId);
  target.data.cast=lookId?[...rest,{characterId,lookId}]:rest;target.version+=1;
  return changes(project,characterId,before,`${target.type==='scene'?'调整场戏默认':'调整镜头覆盖'}「${target.title}」`);
}
export function deleteLook(project,characterId,lookId,replacementId=''){
  const character=find(project,characterId),look=looksOf(character).find(l=>l.id===lookId);
  if(!look)return {ok:false,error:'这个造型已经不存在。'};
  if(replacementId&&(replacementId===lookId||!looksOf(character).some(l=>l.id===replacementId)))return {ok:false,error:'替代造型不存在，请重新选择。'};
  const before=signatures(project,characterId);
  for(const target of lookReferences(project,characterId,lookId)){
    target.data.cast=castOf(target).flatMap(c=>c.characterId===characterId&&c.lookId===lookId?(replacementId?[{...c,lookId:replacementId}]:[]):[c]);target.version+=1;
  }
  character.data.looks=looksOf(character).filter(l=>l.id!==lookId);character.version+=1;
  return changes(project,characterId,before,`删除造型「${look.name}」${replacementId?'并替换绑定':'并解除绑定'}`);
}
