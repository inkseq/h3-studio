import {reconcileGeneratedTracks} from './generated-sound-model.js';
import {rangeShapeIssue} from './video-cut-model.js';
import {detachDeletedReferences} from './entity-references.js';
import {effectiveLook,lookShots} from './character-model.js';
import {locationShots} from './location-model.js';
import {resolveCanvasPositions} from './canvas-layout.js';
import legacySeed from './legacy-seed.json' with { type: 'json' };

export const typeLabels = Object.freeze({chapter:'章节',scene:'场戏',shot:'镜头',character:'角色',location:'地点',prop:'道具',image:'图片',audio:'音频',video:'视频',note:'笔记',generation:'生成步骤'});
export const roleLabels = Object.freeze({identity:'人物身份',location:'场景地点',firstFrame:'首帧图片',lastFrame:'尾帧图片',motion:'动作参考',audio:'声音参考',reference:'通用参考',dependency:'前置步骤'});
const KEY='yingxu-studio-v4', LEGACY_KEY='yingxu-series-project-v1';
const clone=x=>structuredClone(x);
const isObject=x=>x!==null&&typeof x==='object'&&!Array.isArray(x);
const finite=x=>typeof x==='number'&&Number.isFinite(x);
const uid=prefix=>`${prefix}-${globalThis.crypto.randomUUID()}`;
const timestamp=()=>new Date().toISOString();
function freeze(x){if(x&&typeof x==='object'&&!Object.isFrozen(x)){Object.freeze(x);Object.values(x).forEach(freeze);}return x;}
function plain(value,depth=0){
  if(depth>12)return false;
  if(value===null||typeof value==='boolean')return true;
  if(typeof value==='string')return value.length<=200000;
  if(typeof value==='number')return Number.isFinite(value);
  if(Array.isArray(value))return value.length<=20000&&value.every(x=>plain(x,depth+1));
  return isObject(value)&&[Object.prototype,null].includes(Object.getPrototypeOf(value))&&Object.keys(value).length<=5000&&Object.entries(value).every(([k,v])=>!['__proto__','prototype','constructor'].includes(k)&&plain(v,depth+1));
}
function validParent(type,parent){
  if(type==='chapter')return !parent;
  if(type==='scene')return parent?.type==='chapter';
  if(type==='shot')return parent?.type==='scene';
  return !parent||['chapter','scene','shot'].includes(parent.type);
}
export function children(project,parentId){return project.entities.filter(e=>e.parentId===(parentId??null)).sort((a,b)=>a.order-b.order);}
export function ancestors(project,id){const result=[],seen=new Set();let e=project.entities.find(x=>x.id===id);while(e?.parentId&&!seen.has(e.parentId)){seen.add(e.parentId);e=project.entities.find(x=>x.id===e.parentId);if(e)result.unshift(e);}return result;}
function descendants(project,id){const ids=new Set([id]);let changed=true;while(changed){changed=false;for(const e of project.entities)if(ids.has(e.parentId)&&!ids.has(e.id)){ids.add(e.id);changed=true;}}return ids;}
export function validateLink(project,source,target,role){
  const fail=error=>({ok:false,error});
  const a=project.entities.find(e=>e.id===source),b=project.entities.find(e=>e.id===target);
  if(!a||!b)return fail('连线两端的节点必须存在。');
  if(a.id===b.id)return fail('节点不能连接自身。');
  if(!Object.hasOwn(roleLabels,role))return fail('请选择有效的参考用途。');
  if(!['shot','generation'].includes(b.type))return fail('参考只能接入镜头或生成步骤。');
  if(project.links.some(l=>l.source===source&&l.target===target&&l.role===role))return fail('这条连接已经存在。');
  const allowed={identity:['character','image'],location:['location'],firstFrame:['image'],lastFrame:['image'],motion:['video'],audio:['audio'],reference:['image','video','audio','character','location','note'],dependency:['shot','generation']};
  if(!allowed[role].includes(a.type))return fail(`${typeLabels[a.type]||'该节点'}不能作为${roleLabels[role]}。`);
  if(['firstFrame','lastFrame','motion','audio'].includes(role)&&b.type==='generation'&&b.data.recipe!=='video')return fail(`${roleLabels[role]}只能接入视频生成步骤或镜头。`);
  if(role==='dependency'&&b.type!=='generation')return fail('前置步骤只能连接到生成步骤。');
  if(role==='dependency'){
    const seen=new Set(),queue=[target];
    while(queue.length){const id=queue.shift();if(id===source)return fail('这条连接会产生循环依赖，请移除回路中的一条连接。');if(seen.has(id))continue;seen.add(id);for(const l of project.links)if(l.role==='dependency'&&l.source===id)queue.push(l.target);}
  }
  return {ok:true};
}
export function validateProject(input){
  const fail=error=>({ok:false,error});
  if(!isObject(input)||!plain(input))return fail('项目必须是普通 JSON 数据，不能包含非法键、过深结构或过长内容。');
  if(input.schemaVersion!==4)return fail('项目版本不支持；请导入本工作室导出的 v4 项目。');
  if(typeof input.id!=='string'||!input.id||input.id.length>160)return fail('项目 ID 无效。');
  if(typeof input.title!=='string'||!input.title.trim()||input.title.length>160)return fail('项目标题应为 1 至 160 个字符。');
  if(typeof input.logline!=='string'||input.logline.length>24000)return fail('项目简介应为不超过 24000 字符的文本。');
  if(input.journey!==undefined){if(!isObject(input.journey))return fail('创作导航内容必须为普通对象。');if(input.journey.stage!==undefined&&(!Number.isInteger(input.journey.stage)||input.journey.stage<1||input.journey.stage>7))return fail('创作导航阶段应为 1 至 7。');for(const field of ['brief','sound'])if(input.journey[field]!==undefined&&!isObject(input.journey[field]))return fail('创作导航的简报和声音设定格式不正确。');}
  if(!Array.isArray(input.entities)||input.entities.length>5000)return fail('项目节点必须为数组，最多 5000 个。');
  if(!Array.isArray(input.links)||input.links.length>20000)return fail('项目连线必须为数组，最多 20000 条。');
  const ids=new Set();
  for(const e of input.entities){
    if(!isObject(e)||typeof e.id!=='string'||!e.id||e.id.length>160||ids.has(e.id))return fail('节点 ID 无效或重复，请检查导出文件。');ids.add(e.id);
    if(!Object.hasOwn(typeLabels,e.type))return fail(`节点 ${e.id} 的类型不支持。`);
    if(typeof e.title!=='string'||e.title.length>160)return fail(`节点 ${e.id} 的标题应为不超过 160 个字符的文本。`);
    if(typeof e.description!=='string'||e.description.length>24000)return fail(`节点「${e.title}」描述过长或不是文本。`);
    if(e.parentId!==null&&typeof e.parentId!=='string')return fail(`节点「${e.title}」的父级无效。`);
    if(!finite(e.order)||!Number.isInteger(e.order)||e.order<0)return fail(`节点「${e.title}」的顺序必须为非负整数。`);
    if(!Number.isInteger(e.version)||e.version<1)return fail(`节点「${e.title}」的版本必须为正整数。`);
    if(!['draft','review','ready'].includes(e.status)||!isObject(e.data))return fail(`节点「${e.title}」的状态或内容格式无效。`);
    if(e.data.selectedVideoRange!==undefined){const issue=rangeShapeIssue(e.data.selectedVideoRange);if(e.type!=='shot'||issue)return fail(`镜头「${e.title}」${issue||'只有镜头可以设置采用视频选段。'}`);}
    if(e.data.seconds!==undefined&&(!finite(e.data.seconds)||e.data.seconds<=0||e.data.seconds>3600))return fail(`节点「${e.title}」的时长必须大于 0 且不超过 3600 秒。`);
    if(e.type==='generation'&&!['video','image','music'].includes(e.data.recipe))return fail(`生成步骤「${e.title}」请选择视频、图片或音乐配方。`);
    if(e.data.script!==undefined&&(typeof e.data.script!=='string'||e.data.script.length>120000))return fail(`节点「${e.title}」的剧本应为不超过 120000 字符的文本。`);
    for(const field of ['prompt','goal','look','role','time','props','age','source','referenceRole','worldStatus'])if(e.data[field]!==undefined&&(typeof e.data[field]!=='string'||e.data[field].length>24000))return fail(`节点「${e.title}」的 ${field} 应为文本。`);
    if(e.data.bytes!==undefined&&(!Number.isSafeInteger(e.data.bytes)||e.data.bytes<0))return fail(`素材「${e.title}」的文件大小无效。`);
    for(const field of ['fileId','fileName','mime'])if(e.data[field]!==undefined&&(typeof e.data[field]!=='string'||e.data[field].length>1000))return fail(`素材「${e.title}」的 ${field} 无效。`);
  }
  for(const e of input.entities){
    if(e.data.locationId!==undefined&&(typeof e.data.locationId!=='string'||e.data.locationId!==''&&(!['scene','shot'].includes(e.type)||input.entities.find(asset=>asset.id===e.data.locationId)?.type!=='location')))return fail(`「${e.title}」的绑定地点无效。`);
    if(e.data.referenceAssetIds!==undefined&&(e.type!=='location'||!Array.isArray(e.data.referenceAssetIds)||new Set(e.data.referenceAssetIds).size!==e.data.referenceAssetIds.length||e.data.referenceAssetIds.some(id=>typeof id!=='string'||input.entities.find(asset=>asset.id===id)?.type!=='image')))return fail(`地点「${e.title}」的参考图片不存在或格式无效。`);
    if(e.data.selectedAssetId!==undefined&&e.data.selectedAssetId!==''){const asset=input.entities.find(a=>a.id===e.data.selectedAssetId);if(e.type!=='shot'||typeof e.data.selectedAssetId!=='string'||!asset||!['image','video'].includes(asset.type))return fail(`镜头「${e.title||'未命名'}」选定的候选素材不存在或不是图片 / 视频，请重新选择。`);}
    const parent=e.parentId===null?null:input.entities.find(x=>x.id===e.parentId);
    if(e.parentId!==null&&!parent)return fail(`节点「${e.title}」引用的父级不存在。`);
    if(!validParent(e.type,parent))return fail(`「${e.title}」的层级不正确：章节包含场戏，场戏包含镜头；共享节点可位于项目或故事节点内。`);
    const seen=new Set([e.id]);let p=parent;
    while(p){if(seen.has(p.id))return fail(`「${e.title}」存在循环父级关系。`);seen.add(p.id);p=input.entities.find(x=>x.id===p.parentId);}
  }
  const check={...input,links:[]},linkIds=new Set();
  for(const l of input.links){
    if(!isObject(l)||typeof l.id!=='string'||!l.id||l.id.length>160||linkIds.has(l.id))return fail('连线 ID 无效或重复。');linkIds.add(l.id);
    const result=validateLink(check,l.source,l.target,l.role);if(!result.ok)return fail(`连线检查失败：${result.error}`);check.links.push(l);
  }
  if(!isObject(input.layout)||!isObject(input.layout.positions)||!isObject(input.layout.viewport))return fail('画布布局格式无效。');
  for(const [id,p]of Object.entries(input.layout.positions))if(!ids.has(id)||!isObject(p)||!finite(p.x)||!finite(p.y)||Math.abs(p.x)>1e7||Math.abs(p.y)>1e7)return fail('画布节点位置无效，或位置引用了不存在的节点。');
  const v=input.layout.viewport;if(!finite(v.x)||!finite(v.y)||!finite(v.zoom)||v.zoom<.05||v.zoom>10)return fail('画布缩放或视口无效。');
  if(!Array.isArray(input.jobs)||input.jobs.length>10000)return fail('任务记录格式无效。');
  for(const tracks of Object.values(input.journey?.soundTracks||{}))if(Array.isArray(tracks))for(const track of tracks)if(track.generatedFrom!==undefined){
    const g=track.generatedFrom;
    if(!isObject(g)||Object.keys(g).sort().join(',')!=='audioArtifactId,jobId,videoArtifactId,videoEntityId'||Object.values(g).some(id=>typeof id!=='string'||!id||id.length>200)||typeof track.shotId!=='string'||!track.shotId||track.offset!==0||track.needsReview!==undefined&&typeof track.needsReview!=='boolean')return fail('生成声音须保留同任务视频／音频绑定与镜头起点；请解除关联后再独立编辑。');
  }
  return {ok:true,project:clone(input)};
}
export function readiness(project,id,{includeConnectionNotice=true}={}){
  const e=project.entities.find(x=>x.id===id);if(!e)return ['节点不存在。'];
  const blockers=[];
  if(['shot','generation'].includes(e.type)){
    if(!e.description.trim()&&!String(e.data.prompt||'').trim())blockers.push('补充镜头内容或生成描述。');
    if((e.type==='shot'||e.data.recipe==='video')&&(!finite(e.data.seconds)||e.data.seconds<=0))blockers.push('填写有效的镜头时长。');
    const refs=project.links.filter(l=>l.target===id);
    for(const ref of refs){const a=project.entities.find(x=>x.id===ref.source);if(['image','video','audio'].includes(a?.type)&&(!a.data.fileId||a.data.missingFile))blockers.push(`参考素材「${a.title}」缺少可用文件，请重新上传或恢复完整备份。`);if(ref.role==='dependency')blockers.push(`前置步骤「${a?.title||'未知'}」尚未产生可验证的执行结果。`);}
    if(e.data.selectedAssetId){const candidate=project.entities.find(x=>x.id===e.data.selectedAssetId);if(!candidate||!candidate.data.fileId||candidate.data.missingFile)blockers.push(`选定候选「${candidate?.title||'未知素材'}」缺少可用文件，请重新上传或重新选择。`);}
    if(e.status==='review')blockers.push('参考或内容已改变，请复核当前版本。');
    if(includeConnectionNotice)blockers.push('尚未接入生成服务；当前只能保存配方，不能生成或估算真实费用。');
  }else if(['image','audio','video'].includes(e.type)&&(!e.data.fileId||e.data.missingFile))blockers.push('缺少可用素材文件，请重新上传或恢复完整备份。');
  else if(e.type==='scene'&&!String(e.data.script||'').trim())blockers.push('补充这一场的剧本。');
  return [...new Set(blockers)];
}
function blank(title='未命名短剧'){return {schemaVersion:4,id:uid('project'),title:title.trim()||'未命名短剧',logline:'',entities:[],links:[],layout:{positions:{},viewport:{x:0,y:0,zoom:1}},jobs:[],updatedAt:timestamp()};}
function baseEntity(type,parentId,title,description='',data={},order=0,id=uid(type)){return {id,type,title,description,parentId,order,version:1,status:'draft',data};}
export function migrateLegacy(old){
  if(!isObject(old)||!Array.isArray(old.episodes)||!Array.isArray(old.characters)||!Array.isArray(old.locations))throw Error('旧项目结构不完整，原始数据已保留。');
  const p=blank(String(old.title||'导入的短剧')),entityIds=new Set(),map=new Map();
  const add=(type,oldId,parentId,title,description,data,order)=>{const id=typeof oldId==='string'&&oldId&&!entityIds.has(oldId)?oldId:uid(type);entityIds.add(id);if(oldId)map.set(oldId,id);const e=baseEntity(type,parentId,String(title||typeLabels[type]),String(description||''),data,order,id);p.entities.push(e);return e;};
  old.characters.forEach((c,i)=>add('character',c.id,null,c.name,c.identity,{age:c.age||'',role:c.role||'',sheets:c.sheets||[],requested:c.requested||[],look:c.look||''},i));
  old.locations.forEach((l,i)=>add('location',l.id,null,l.title,l.desc,{worldStatus:l.worldStatus||'未创建'},i));
  old.episodes.forEach((ep,i)=>{const chapter=add('chapter',ep.id,null,ep.title,ep.hook,{},i);(ep.scenes||[]).forEach((s,j)=>{const se=add('scene',s.id,chapter.id,s.title,s.goal,{script:s.script||'',goal:s.goal||'',time:s.time||'',props:s.props||'',look:s.look||'',camera:s.camera||null},j);se.version=Number.isInteger(s.version)&&s.version>0?s.version:1;se.status=s.review?'review':'draft';(s.shots||[]).forEach((sh,k)=>{const shot=add('shot',sh.id,se.id,sh.title,sh.action,{seconds:finite(sh.seconds)&&sh.seconds>0?sh.seconds:6},k);for(const c of s.castRefs||[])if(map.has(c.characterId))p.links.push({id:uid('link'),source:map.get(c.characterId),target:shot.id,role:'identity'});if(map.has(s.place))p.links.push({id:uid('link'),source:map.get(s.place),target:shot.id,role:'location'});});});});
  p.entities.forEach((e,i)=>p.layout.positions[e.id]={x:360*(i%5),y:240*Math.floor(i/5)});
  const valid=validateProject(p);if(!valid.ok)throw Error(valid.error);return valid.project;
}

export function createStore({storage,now=()=>Date.now(),seed=legacySeed}={}){
  if(storage===undefined){try{storage=globalThis.localStorage;}catch{storage=null;}}
  const listeners=new Set(),past=[],future=[];let coalesce=null,protectedRaw=false,recoveryRaw=null;
  let localWorkspace=null,cloudSavedJSON=null;
  const volatileCloudDrafts=new Map();
  let state={project:null,workspaceEpoch:0,workspace:{mode:'local'},mode:'guide',section:'journey',selectedId:null,scopeId:null,saveState:'尚未保存',notice:'',undoCount:0,redoCount:0};
  const emit=()=>{state=freeze({...state,undoCount:past.length,redoCount:future.length});for(const fn of [...listeners])fn();};
  const cloudKey=(account,id)=>`yingxu-cloud-draft-v1:${encodeURIComponent(account)}:${encodeURIComponent(id)}`;
  const persist=()=>{
    const cloud=state.workspace.mode==='cloud',dirty=cloud&&JSON.stringify(state.project)!==cloudSavedJSON;
    const key=cloud?cloudKey(state.workspace.account,state.project.id):KEY;
    const draft={project:state.project,mode:state.mode,section:state.section,selectedId:state.selectedId,scopeId:state.scopeId,...(cloud?{serverVersion:state.workspace.serverVersion,dirty}:{})};
    try{
      if(!storage)throw Error('不可用');if(protectedRaw)throw Error('旧文件尚未备份');
      storage.setItem(key,JSON.stringify(draft));volatileCloudDrafts.delete(key);
      state={...state,saveState:cloud?(dirty?'云端更改待保存 · 草稿已留本机':'已保存到云端'):'已保存到此浏览器',workspace:{...state.workspace,...(cloud?{dirty}:{})}};return true;
    }catch{
      // Account switches must hide private content even when browser quota is
      // exhausted. Keep an owner-scoped in-memory recovery copy, never another
      // account's workspace. This is not durable storage or encryption.
      if(cloud)volatileCloudDrafts.set(key,{...draft,project:clone(state.project),dirty:true,volatile:true});
      state={...state,saveState:cloud?'保存失败 · 云草稿仅暂存在本页，请导出备份':'保存失败，请导出备份',workspace:{...state.workspace,...(cloud?{dirty:true}:{})}};return false;
    }
  };
  const say=message=>{state={...state,notice:message};emit();};
  const backup=(value=state.project,label='project')=>{try{if(!storage)throw Error('不可用');storage.setItem(`${KEY}-backup-${label}-${now()}-${uid('copy')}`,typeof value==='string'?value:JSON.stringify(value));return true;}catch{say('无法保存旧项目备份。请先导出项目并释放浏览器空间，再新建或替换。');return false;}};
  let raw=null;
  try{
    raw=storage?.getItem(KEY);
    if(raw){const saved=JSON.parse(raw),valid=validateProject(saved.project||saved);if(!valid.ok)throw Error(valid.error);state={...state,project:valid.project};if(['guide','canvas'].includes(saved.mode))state={...state,mode:saved.mode};if(['journey','story','characters','locations','assets','production','activity'].includes(saved.section))state={...state,section:saved.section};if(state.project.entities.some(e=>e.id===saved.selectedId))state={...state,selectedId:saved.selectedId};if(state.project.entities.some(e=>e.id===saved.scopeId&&e.type==='chapter'))state={...state,scopeId:saved.scopeId};}
  }catch(error){if(raw){recoveryRaw=raw;protectedRaw=true;backup(raw,'recovery');state={...state,notice:'已保存项目无法读取；原始数据已保留，请导出备份后检查导入文件。'};}}
  if(!state.project&&protectedRaw)state={...state,project:blank('待恢复的项目')};
  if(!state.project){let oldRaw=null;try{oldRaw=storage?.getItem(LEGACY_KEY);state={...state,project:oldRaw?migrateLegacy(JSON.parse(oldRaw)):migrateLegacy(seed)};if(oldRaw)state={...state,notice:'已迁移原章节项目；旧浏览器数据仍保留。'};}catch{state={...state,project:migrateLegacy(seed),notice:'旧项目无法迁移，原始数据仍保留。当前打开演示项目。'};}}
  persist();emit();
  const commit=(p,{coalesceKey=null,history=true}={})=>{
    const valid=validateProject(p);if(!valid.ok){say(valid.error);return false;}
    reconcileGeneratedTracks(valid.project);
    if(JSON.stringify(state.project)===JSON.stringify(valid.project))return true;
    const t=now();if(history){if(!(coalesceKey&&coalesce?.key===coalesceKey&&t-coalesce.time<1200)){past.push(state.project);if(past.length>100)past.shift();}future.length=0;coalesce=coalesceKey?{key:coalesceKey,time:t}:null;}
    valid.project.updatedAt=timestamp();state={...state,project:valid.project,notice:''};
    if(state.selectedId&&!valid.project.entities.some(e=>e.id===state.selectedId))state.selectedId=null;
    if(state.scopeId&&!valid.project.entities.some(e=>e.id===state.scopeId))state.scopeId=null;
    persist();emit();return true;
  };
  const changeView=patch=>{coalesce=null;state={...state,...patch};persist();emit();};
  const markDownstream=(p,id)=>{const reached=new Set([id]),queue=[id];while(queue.length){const next=queue.shift(),subject=p.entities.find(e=>e.id===next),castShots=subject?.type==='character'?p.entities.filter(e=>e.type==='shot'&&effectiveLook(p,e,next)).map(e=>e.id):[],galleryShots=p.entities.filter(e=>e.type==='character').flatMap(c=>(c.data.looks||[]).filter(l=>Object.values(l.gallery||{}).includes(next)).flatMap(l=>lookShots(p,c.id,l.id).map(e=>e.id))),locationDependents=[...locationShots(p,next).map(e=>e.id),...p.entities.filter(e=>e.type==='location'&&(e.data.referenceAssetIds||[]).includes(next)).flatMap(e=>[e.id,...locationShots(p,e.id).map(shot=>shot.id)])],dependents=new Set([...locationDependents,...castShots,...galleryShots,...p.links.filter(l=>l.source===next).map(l=>l.target),...p.entities.filter(e=>e.parentId===next||e.data.selectedAssetId===next).map(e=>e.id)]);for(const target of dependents)if(!reached.has(target)){reached.add(target);queue.push(target);const e=p.entities.find(x=>x.id===target);if(e&&['scene','shot','generation'].includes(e.type))e.status='review';}}};
  const replace=(p)=>{const valid=validateProject(p);if(!valid.ok){say(valid.error);return false;}if(protectedRaw&&recoveryRaw!==null&&!backup(recoveryRaw,'recovery'))return false;if(!backup())return false;protectedRaw=false;recoveryRaw=null;past.length=0;future.length=0;coalesce=null;state={...state,project:valid.project,workspaceEpoch:state.workspaceEpoch+1,selectedId:null,scopeId:null,notice:'旧项目已备份，当前项目已切换。'};persist();emit();return true;};
  return {
    getState:()=>state,
    getLocalProjectSummary(){const project=state.workspace.mode==='local'?state.project:localWorkspace?.state.project;return project?{id:project.id,title:project.title}:null;},
    getRecoveryRaw:()=>recoveryRaw,
    getPersistenceRisk:()=>({current:state.saveState.startsWith('保存失败'),otherCloudDrafts:[...volatileCloudDrafts.keys()].filter(key=>state.workspace.mode!=='cloud'||key!==cloudKey(state.workspace.account,state.project.id)).length}),
    checkpoint(){const saved=persist()&&!volatileCloudDrafts.size;if(!saved)state={...state,notice:'当前或先前账户仍有未落盘草稿，已阻止刷新。请用原账户恢复后备份，保留本页面。'};emit();return saved;},
    getCloudDraft(account,id){const key=cloudKey(account,id);if(volatileCloudDrafts.has(key))return clone(volatileCloudDrafts.get(key));try{const raw=storage?.getItem(key);if(!raw)return null;const draft=JSON.parse(raw),valid=validateProject(draft.project);return valid.ok?{...draft,project:valid.project}:null;}catch{return null;}},
    enterCloudProject(project,{account,version,restoreDraft=false}={}){
      const valid=validateProject(project);if(!valid.ok||!account||!Number.isInteger(version)||version<1){say(valid.error||'云项目身份或版本无效');return false;}
      if(state.workspace.mode==='local')localWorkspace={state,past:[...past],future:[...future],coalesce,protectedRaw,recoveryRaw};
      else persist();
      const draft=restoreDraft?this.getCloudDraft(account,project.id):null;
      if(draft&&draft.serverVersion!==version){say('草稿基于旧版云项目，未覆盖；请先导出草稿并比较。');return false;}
      cloudSavedJSON=JSON.stringify(valid.project);protectedRaw=false;recoveryRaw=null;past.length=0;future.length=0;coalesce=null;
      state={...state,project:draft?.project||valid.project,workspaceEpoch:state.workspaceEpoch+1,workspace:{mode:'cloud',account,serverVersion:version,dirty:!!draft?.dirty},selectedId:null,scopeId:null,section:'journey',notice:'云项目已打开；原本机作品保留。'};persist();emit();return true;
    },
    markCloudSaved(version,submittedProject){
      if(state.workspace.mode!=='cloud'||!Number.isInteger(version)||version<state.workspace.serverVersion||submittedProject.id!==state.project.id)return false;
      cloudSavedJSON=JSON.stringify(submittedProject);state={...state,workspace:{...state.workspace,serverVersion:version}};persist();emit();return true;
    },
    leaveCloudProject(){
      if(state.workspace.mode!=='cloud')return true;persist();
      if(!localWorkspace){say('本机工作区无法恢复，请先导出当前项目');return false;}
      const saved=localWorkspace;state={...saved.state,workspaceEpoch:state.workspaceEpoch+1,notice:'已回到原本机作品。云草稿仍按账户保留。'};past.splice(0,past.length,...saved.past);future.splice(0,future.length,...saved.future);coalesce=saved.coalesce;protectedRaw=saved.protectedRaw;recoveryRaw=saved.recoveryRaw;localWorkspace=null;cloudSavedJSON=null;emit();return true;
    },
    editProject(mutator,options={}){try{const p=clone(state.project);if(mutator(p)===false)return false;return commit(p,options);}catch(error){say(error.message||'这次修改未保存，原项目保留。');return false;}},
    updateProject(patch,options={}){const p=clone(state.project);for(const key of ['title','logline','journey'])if(Object.hasOwn(patch,key))p[key]=patch[key];return commit(p,options);},
    subscribe(fn){listeners.add(fn);return()=>listeners.delete(fn);},
    notify:say,
    setMode(mode){if(['guide','canvas'].includes(mode))changeView({mode});},
    setSection(section){if(['journey','story','characters','locations','assets','production','activity'].includes(section))changeView({section});},
    select(id){if(id===null||state.project.entities.some(e=>e.id===id))changeView({selectedId:id});},
    setScope(id){if(id===null||state.project.entities.some(e=>e.id===id&&e.type==='chapter'))changeView({scopeId:id});},
    addEntity(type,parentId=null,patch={}){
      if(!Object.hasOwn(typeLabels,type)){say('不支持的节点类型。');return false;}
      const p=clone(state.project),siblings=children(p,parentId),data=type==='generation'?{recipe:'video',seconds:6}:type==='shot'?{seconds:6}:type==='scene'?{script:''}:{};
      const id=uid(type),e=baseEntity(type,parentId,patch.title||`新${typeLabels[type]}`,patch.description||'',{...data,...patch.data},siblings.length?Math.max(...siblings.map(x=>x.order))+1:0,id);
      p.entities.push(e);p.layout.positions[id]=resolveCanvasPositions(p.entities,p.layout.positions)[id];
      if(!commit(p))return false;changeView({selectedId:id});return id;
    },
    updateEntity(id,patch,options={}){
      const p=clone(state.project),e=p.entities.find(x=>x.id===id);if(!e)return false;
      for(const key of ['title','description','parentId','status','version'])if(Object.hasOwn(patch,key))e[key]=patch[key];if(patch.data)e.data={...e.data,...patch.data};
      if(e.type==='generation'&&e.data.recipe!==state.project.entities.find(x=>x.id===id).data.recipe){for(const link of p.links.filter(l=>l.target===id)){const checked=validateLink({...p,links:p.links.filter(l=>l.id!==link.id)},link.source,link.target,link.role);if(!checked.ok){say(`无法切换生成能力：${checked.error} 请先解除不兼容的关联；原配方和素材均已保留。`);return false;}}}
      const before=state.project.entities.find(x=>x.id===id);if(JSON.stringify({...e,version:0})===JSON.stringify({...before,version:0}))return true;if(before.status==='ready'&&!Object.hasOwn(patch,'status'))e.status='draft';
      const shotChanged=e.description!==before.description||e.parentId!==before.parentId||['seconds','goal','shotSize','cameraMove','cameraHeight','cast','locationId','referenceRanges','prompt','style','h3'].some(key=>JSON.stringify(e.data[key])!==JSON.stringify(before.data[key]));
      if(e.type==='shot'&&shotChanged&&state.workspace.mode==='cloud'&&!Object.hasOwn(patch,'version'))e.version=before.version+1;
      if(e.type==='shot'&&shotChanged&&before.data.acceptPlaceholder)e.data.acceptPlaceholder=false;
      if(e.type==='shot'&&(before.data.selectedAssetId||before.data.uxReview?.chosen)&&shotChanged&&!Object.hasOwn(patch,'status'))e.status='review';
      if(patch.description!==undefined||patch.data)markDownstream(p,id);return commit(p,options);
    },
    deleteEntity(id){const p=clone(state.project),e=p.entities.find(x=>x.id===id);if(!e)return false;const ids=descendants(p,id);for(const deleted of ids)markDownstream(p,deleted);const detached=detachDeletedReferences(p,ids);for(const changed of detached.changed)markDownstream(p,changed);p.entities=p.entities.filter(x=>!ids.has(x.id));p.links=p.links.filter(l=>!ids.has(l.source)&&!ids.has(l.target));for(const deleted of ids)delete p.layout.positions[deleted];return commit(p);},
    duplicateEntity(id){
      const p=clone(state.project),original=p.entities.find(e=>e.id===id);if(!original)return false;
      const ids=descendants(p,id),remap=new Map([...ids].map(old=>[old,uid(p.entities.find(e=>e.id===old).type)])),copies=p.entities.filter(e=>ids.has(e.id)).map(e=>({...clone(e),id:remap.get(e.id),parentId:remap.get(e.parentId)||e.parentId,title:e.id===id?`${e.title.slice(0,154)} 副本`:e.title,version:1,status:'draft'}));
      for(const copy of copies){delete copy.data.confirmed;if(remap.has(copy.data.locationId))copy.data.locationId=remap.get(copy.data.locationId);if(Array.isArray(copy.data.referenceAssetIds))copy.data.referenceAssetIds=copy.data.referenceAssetIds.map(id=>remap.get(id)||id);if(remap.has(copy.data.selectedAssetId))copy.data.selectedAssetId=remap.get(copy.data.selectedAssetId);if(remap.has(copy.data.selectedVideoRange?.assetId))copy.data.selectedVideoRange.assetId=remap.get(copy.data.selectedVideoRange.assetId);}
      copies.find(e=>e.id===remap.get(id)).order=Math.max(-1,...children(p,original.parentId).map(e=>e.order))+1;
      const links=p.links.filter(l=>ids.has(l.target)).map(l=>({...l,id:uid('link'),source:remap.get(l.source)||l.source,target:remap.get(l.target)}));
      for(const [old,fresh]of remap){const xy=p.layout.positions[old]||{x:0,y:0};p.layout.positions[fresh]={x:xy.x+60,y:xy.y+60};}p.entities.push(...copies);p.links.push(...links);
      if(!commit(p))return false;changeView({selectedId:remap.get(id)});return remap.get(id);
    },
    reorderEntity(id,delta){const p=clone(state.project),e=p.entities.find(x=>x.id===id);if(!e||!finite(delta)||!delta)return false;const siblings=children(p,e.parentId).filter(x=>x.type===e.type),from=siblings.findIndex(x=>x.id===id),to=Math.max(0,Math.min(siblings.length-1,from+Math.sign(delta)));if(from===to)return false;const [moved]=siblings.splice(from,1);siblings.splice(to,0,moved);siblings.forEach((x,i)=>x.order=i);return commit(p);},
    addLink(source,target,role){const result=validateLink(state.project,source,target,role);if(!result.ok){say(result.error);return result;}const p=clone(state.project);p.links.push({id:uid('link'),source,target,role});const e=p.entities.find(x=>x.id===target);e.status='review';markDownstream(p,target);return commit(p)?{ok:true}:{ok:false,error:state.notice};},
    removeLink(id){const p=clone(state.project),link=p.links.find(l=>l.id===id);if(!link)return false;p.links=p.links.filter(l=>l.id!==id);const target=p.entities.find(e=>e.id===link.target);if(target){target.status='review';markDownstream(p,target.id);}return commit(p);},
    setPositions(map){if(!isObject(map))return false;const p=clone(state.project);for(const [id,pos]of Object.entries(map))if(p.entities.some(e=>e.id===id))p.layout.positions[id]=clone(pos);return commit(p,{coalesceKey:'canvas:positions'});},
    setViewport(viewport){return commit({...clone(state.project),layout:{...clone(state.project.layout),viewport:clone(viewport)}},{history:false});},
    undo(){if(!past.length)return false;future.push(state.project);state={...state,project:past.pop(),notice:'已撤销上一步。'};coalesce=null;if(state.selectedId&&!state.project.entities.some(e=>e.id===state.selectedId))state.selectedId=null;if(state.scopeId&&!state.project.entities.some(e=>e.id===state.scopeId))state.scopeId=null;persist();emit();return true;},
    redo(){if(!future.length)return false;past.push(state.project);state={...state,project:future.pop(),notice:'已重做。'};coalesce=null;persist();emit();return true;},
    newProject(title='未命名短剧'){if(state.workspace.mode==='cloud'){say('请通过云项目列表创建新项目，或先返回本机作品。');return false;}return replace(blank(title));},
    loadDemo(){if(state.workspace.mode==='cloud'){say('请先返回本机模式，再打开演示。');return false;}return replace(migrateLegacy(seed));},
    replaceProject(project){if(state.workspace.mode==='cloud'){say('请先返回本机模式导入，再明确复制到云端。');return false;}return replace(project);},
    exportProject:()=>clone(state.project)
  };
}
export const store=createStore();
