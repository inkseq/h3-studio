const find=(project,id)=>project.entities.find(entity=>entity.id===id);

export function shotLocationIds(project,shot){
  const scene=find(project,shot?.parentId);
  return [...new Set([shot?.data.locationId||scene?.data.locationId,...project.links.filter(link=>link.target===shot?.id&&['location','reference'].includes(link.role)&&find(project,link.source)?.type==='location').map(link=>link.source)].filter(Boolean))];
}
export function locationShots(project,locationId){return project.entities.filter(entity=>entity.type==='shot'&&shotLocationIds(project,entity).includes(locationId));}
export function setLocationReferences(project,locationId,assetIds){
  const location=find(project,locationId);if(location?.type!=='location')return {ok:false,error:'这个地点已经不存在，请重新选择。'};
  const ids=[...new Set(assetIds)];if(ids.some(id=>find(project,id)?.type!=='image'))return {ok:false,error:'地点参考请选择已上传的图片。'};
  location.data.referenceAssetIds=ids;location.version++;location.status='review';
  const affected=locationShots(project,locationId);for(const shot of affected){shot.version++;shot.status='review';}
  return {ok:true,affected:affected.map(shot=>shot.id)};
}
export function bindLocation(project,entityId,locationId){
  const entity=find(project,entityId);if(!['scene','shot'].includes(entity?.type)||locationId&&find(project,locationId)?.type!=='location')return {ok:false,error:'场戏或地点已经不存在，请重新选择。'};
  if((entity.data.locationId||'')===locationId)return {ok:true,affected:[]};
  const shots=entity.type==='shot'?[entity]:project.entities.filter(shot=>shot.type==='shot'&&shot.parentId===entity.id&&!shot.data.locationId),before=new Map(shots.map(shot=>[shot.id,JSON.stringify(shotLocationIds(project,shot))]));
  entity.data.locationId=locationId;entity.version++;entity.status='review';
  const affected=shots.filter(shot=>before.get(shot.id)!==JSON.stringify(shotLocationIds(project,shot)));for(const shot of affected){if(shot!==entity)shot.version++;shot.status='review';}
  return {ok:true,affected:affected.map(shot=>shot.id)};
}
