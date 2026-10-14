import {validateProject} from './store.js';

export const LOCAL_STORIES_KEY='yingxu-project-library-v1';
export function readLocalStoryLibrary(storage){
  let raw='';
  try{
    if(storage===undefined)storage=globalThis.localStorage;
    raw=storage?.getItem(LOCAL_STORIES_KEY)||'[]';
    const projects=JSON.parse(raw);
    if(!Array.isArray(projects)||projects.some(p=>!p||typeof p.id!=='string'||typeof p.title!=='string'||!Array.isArray(p.entities)))throw Error('invalid library');
    return {raw,projects,error:false};
  }catch{return {raw,projects:[],error:true};}
}

export function archiveLocalStory(store,storage=globalThis.localStorage){
  if(store.getState().workspace.mode!=='local')throw Error('请先返回本机作品，再操作本机故事列表。');
  const saved=readLocalStoryLibrary(storage);
  if(saved.error)throw Error('本机故事列表无法读取，请先下载列表原始数据和当前作品备份。');
  const project=store.exportProject();
  storage.setItem(LOCAL_STORIES_KEY,JSON.stringify([project,...saved.projects.filter(p=>p.id!==project.id)]));
}

// Preserve the current local work before any replacement; never copy cloud
// project bodies into the browser's shared local-project library.
export function openLocalStory(id,{store,cloud,storage=globalThis.localStorage}){
  if(cloud.getState().busy)throw Error('上一项云操作还在处理中，请稍后切换故事。');
  const original=store.getLocalProjectSummary();
  let target;
  if(id!==original?.id){
    const saved=readLocalStoryLibrary(storage);
    if(saved.error)throw Error('本机故事列表无法读取，请先下载备份。');
    target=saved.projects.find(p=>p.id===id);
    if(!target)throw Error('这个本机故事已不在列表中，请刷新故事列表。');
    const checked=validateProject(target);
    if(!checked.ok)throw Error(checked.error);
    target=checked.project;
  }
  if(store.getState().workspace.mode==='cloud')cloud.leave();
  if(store.getState().workspace.mode!=='local')throw Error('原本机作品暂时无法恢复，请先备份当前作品。');
  if(target){archiveLocalStory(store,storage);if(store.replaceProject(target)===false)throw Error(store.getState().notice);}
  store.setSection('journey');store.select(null);store.setScope(null);
  return true;
}

export function createLocalStory(title,{store,cloud,storage=globalThis.localStorage}){
  const name=title.trim();if(!name)throw Error('请填写故事名称。');
  if(cloud.getState().busy)throw Error('上一项云操作还在处理中，请稍后新建故事。');
  if(store.getState().workspace.mode==='cloud')cloud.leave();
  if(store.getState().workspace.mode!=='local')throw Error('原本机作品暂时无法恢复，请先备份当前作品。');
  archiveLocalStory(store,storage);
  if(store.newProject(name)===false)throw Error(store.getState().notice);
  store.setSection('journey');store.setMode('guide');
  return true;
}


// Library presentation derives from the owned story document. A list summary is
// not an empty document: missing entities keeps counts and covers unknown.
export function storyLibrarySummary(value){
  const project=value?.project||value||{},entities=Array.isArray(project.entities)?project.entities:null;
  const details={id:project.id||value?.id,title:project.title||value?.title||'未命名故事',updatedAt:project.updatedAt??value?.updated_at??value?.updatedAt??null,shotCount:null,chosenCount:null,cover:null};
  if(!entities)return details;
  const byId=new Map(entities.map(entity=>[entity.id,entity])),shots=entities.filter(entity=>entity.type==='shot');
  const orderPath=entity=>{const path=[],seen=new Set();for(let item=entity;item&&!seen.has(item.id);item=byId.get(item.parentId)){seen.add(item.id);path.unshift(Number.isFinite(item.order)?item.order:0);}return path;};
  const compare=(a,b)=>{const x=orderPath(a),y=orderPath(b);for(let i=0;i<Math.max(x.length,y.length);i++){const difference=(x[i]??-1)-(y[i]??-1);if(difference)return difference;}return entities.indexOf(a)-entities.indexOf(b);};
  const chosen=shots.filter(shot=>typeof shot.data?.selectedAssetId==='string'&&shot.data.selectedAssetId).sort(compare);
  details.shotCount=shots.length;details.chosenCount=chosen.length;
  for(const shot of chosen){
    const selected=byId.get(shot.data.selectedAssetId);if(!selected||!['image','video'].includes(selected.type))continue;
    const frame=selected.type==='video'&&selected.data?.cloudArtifactId?entities.find(entity=>entity.type==='image'&&entity.data?.fileId&&!entity.data.missingFile&&entity.data.sourceSelection?.kind==='artifact_frame'&&entity.data.sourceSelection.artifact_id===selected.data.cloudArtifactId):null;
    details.cover=frame||selected;break;
  }
  return details;
}

export function storyCoverFileId(entity,{mode}={}){
  if(!entity||!['image','video'].includes(entity.type)||entity.data?.missingFile)return null;
  const fileId=entity.data?.fileId;
  if(typeof fileId!=='string'||!/^[\w-]{1,100}$/.test(fileId))return null;
  const cloud=/^cloud_(asset|artifact)_([A-Za-z0-9_-]+)$/.exec(fileId);
  if(!cloud)return {fileId,kind:entity.type,cloudPath:null};
  if(mode!=='cloud'||entity.data[cloud[1]==='asset'?'cloudAssetId':'cloudArtifactId']!==cloud[2])return null;
  return {fileId,kind:entity.type,cloudPath:'/v1/'+cloud[1]+'s/'+cloud[2]+'/content'};
}

export function storyEditedTimestamp(value){
  const date=typeof value==='number'?value*(value<1e12?1000:1):Date.parse(value);
  return Number.isFinite(date)&&date>0&&Number.isFinite(new Date(date).getTime())?date:null;
}
