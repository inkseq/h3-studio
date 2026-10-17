import {translate} from './i18n/locale.js';
/* Layout A projection of the existing v4 document. No second project store. */
import {children,ancestors} from './store.js';
import {effectiveLimits,effectiveControlSchema,roleSupported} from './quick-chat-model.js';
const uid=kind=>kind+'-'+crypto.randomUUID();
const find=(p,id)=>p.entities.find(e=>e.id===id);
const mode=shot=>shot.data.storyboard?.mode||shot.data.h3?.inputMode||'fl';
const list=value=>Array.isArray(value)?value:[];
export function chapterShots(project,chapterId){return children(project,chapterId).filter(e=>e.type==='scene').flatMap(scene=>children(project,scene.id).filter(e=>e.type==='shot'));}
export function storyboardProfile(capabilities,quality,inputMode,explicitId){
  const profiles=capabilities?.deployment_profiles||[],enabled=p=>p.generation_support?.[inputMode]?.configured===true&&p.generation_support?.[inputMode]?.enabled===true;
  const id=explicitId||capabilities?.default_deployment_profile_id;
  const exact=profiles.find(p=>p.id===id);
  return exact&&enabled(exact)?exact:null;
}
export function storyboardGenerationProfile(capabilities,shot,{quality,sourceRequest}={}){
  const targetQuality=quality||shot.quality,inputMode=sourceRequest?.mode||shot.mode.toLowerCase();
  return storyboardProfile(capabilities,targetQuality,inputMode,sourceRequest?.deployment_profile_id||shot.entity?.data.h3?.deploymentProfileId);
}
export function storyboardView(project,{chapterId,capabilities,jobs=[],queue=null,busy=false,error=''}={}){
  const chapter=find(project,chapterId)||children(project,null).find(e=>e.type==='chapter');
  const assets=project.entities.filter(e=>['image','video','audio'].includes(e.type)).map(e=>({id:e.id,kind:e.type,name:e.title,fileId:e.data.fileId,entity:e,seconds:e.data.metadata?.source_duration??e.data.metadata?.duration,inbox:e.data.storyboardInbox!==false&&!project.entities.some(profile=>[...list(profile.data.imageIds),...list(profile.data.referenceAssetIds),...list(profile.data.looks).flatMap(v=>list(v.imageIds??Object.values(v.gallery||{}))),...list(profile.data.versions).flatMap(v=>list(v.imageIds)),...list(profile.data.voices).map(v=>v.assetId),profile.data.ambient?.assetId].includes(e.id)),source:e.data.source||'upload'}));
  const profiles=project.entities.filter(e=>['character','location','prop'].includes(e.type)).map(e=>{
    const variants=e.type==='character'?list(e.data.looks):e.type==='location'?(list(e.data.versions).length?e.data.versions:[{id:'legacy',name:'原场景',description:'',imageIds:list(e.data.referenceAssetIds)}]):[{id:'main',name:'主图',imageIds:list(e.data.imageIds)}];
    return {id:e.id,type:e.type,name:e.title,desc:e.description,def:e.data.defaultLookId||e.data.defaultVersionId||variants[0]?.id||'',voice:list(e.data.voices).find(v=>v.id===e.data.mainVoiceId)?.assetId||'',voices:list(e.data.voices).map(v=>({asset:v.assetId,start:v.clipStart||0})),ambient:e.data.ambient?.assetId||'',ambientStart:e.data.ambient?.clipStart??0,variants:variants.map(v=>({id:v.id,name:v.name,desc:v.description||'',images:list(v.imageIds??Object.values(v.gallery||{})),voice:list(e.data.voices).find(vo=>vo.id===v.voiceId)?.assetId||''}))};
  });
  for(const a of assets){const stem=(a.entity.data.fileName||a.name).replace(/\.[^.]+$/,'').toLocaleLowerCase();const matches=profiles.filter(p=>p.name.length>=2&&stem.includes(p.name.toLocaleLowerCase()));if(matches.length===1)a.suggest={pid:matches[0].id,vid:matches[0].def,label:matches[0].name};}
  const shots=(chapter?chapterShots(project,chapter.id):[]).map(s=>{
    const b=s.data.storyboard||{},shotJobs=[...new Map([...project.jobs,...jobs].filter(j=>j.client_ref?.shot_id===s.id).map(j=>[j.id,j])).values()].sort((a,b)=>Number(a.created_at)-Number(b.created_at));
    const versions=shotJobs.map((j,n)=>{const artifact=j.artifacts?.find(a=>a.kind==='video'),entity=project.entities.find(e=>e.data.cloudArtifactId===artifact?.id&&e.type==='video');return {id:entity?.id||j.id,n:n+1,quality:j.effective_request?.model?.includes('Pruned')?'draft':'final',seed:j.effective_request?.seed,mode:j.effective_request?.mode==='ref'?'REF':'FL',status:j.status==='succeeded'?'done':j.status,jobId:j.id,artifactId:artifact?.id,asset:entity?{fileId:entity.data.fileId,entity,kind:'video'}:null,duration:artifact?.metadata?.duration??artifact?.metadata?.duration_seconds,error:j.error_code||j.message||'',audio:j.artifacts?.find(a=>a.kind==='audio'),effectiveRequest:j.effective_request};});
    for(const id of list(s.data.candidateIds)){if(versions.some(v=>v.id===id))continue;const e=find(project,id);if(e?.type==='video')versions.push({id:e.id,n:versions.length+1,status:'done',quality:'draft',mode:mode(s)==='ref'?'REF':'FL',artifactId:e.data.cloudArtifactId,jobId:e.data.sourceJobId,asset:{fileId:e.data.fileId,entity:e,kind:'video'},duration:e.data.metadata?.duration});}
    const scene=find(project,s.parentId),cast=new Map((s.data.storyboard&&Object.hasOwn(s.data,'cast')?list(s.data.cast):[...list(scene?.data.cast),...list(s.data.cast)]).map(c=>[c.characterId,{id:c.characterId,variant:c.lookId}]));
    return {id:s.id,title:s.title,script:s.data.script||'',prompt:s.data.prompt??s.description,mode:mode(s)==='ref'?'REF':'FL',duration:s.data.seconds??5,start:Object.hasOwn(b,'firstFrameId')?b.firstFrameId:project.links.find(l=>l.target===s.id&&l.role==='firstFrame')?.source||'',end:Object.hasOwn(b,'lastFrameId')?b.lastFrameId:project.links.find(l=>l.target===s.id&&l.role==='lastFrame')?.source||'',refs:b.extraRefs?.map(r=>r.entityId)||project.links.filter(l=>l.target===s.id&&['reference','identity','motion','audio'].includes(l.role)&&assets.some(a=>a.id===l.source)).map(l=>l.source),cast:[...cast.values()],place:Object.hasOwn(b,'place')?(b.place?{id:b.place.locationId,variant:b.place.versionId}:null):s.data.locationId?{id:s.data.locationId,variant:''}:scene?.data.locationId?{id:scene.data.locationId,variant:''}:null,props:b.propIds||[],continuePrev:!!b.continuePrevious,previousFrame:b.previousFrame?.frameAssetId,quality:b.quality??s.data.qualityIntent??'draft',seedLock:!!s.data.h3?.controls?.seed,versions,chosen:s.data.selectedAssetId||'',entity:s};
  });
  return {project:{title:project.title,chapter:chapter?.title||'第一章',chapterId:chapter?.id||null,style:project.journey?.storyboard?.style||'',aspect:project.journey?.brief?.aspect||'16:9'},shots,assets,profiles,queue,busy,error,capabilities,modelLabelKeys:Object.fromEntries(['draft','final'].filter(q=>!storyboardProfile(capabilities,q,'fl')?.label).map(q=>[q,'storyboard.modelUnloaded'])),models:Object.fromEntries(['draft','final'].map(q=>[q,storyboardProfile(capabilities,q,'fl')?.label||'服务能力尚未读取']))};
}
export function expandStoryboard(shot,state,{forSync=false,locale='zh',effectiveProfile=null,effectiveRecipe=null}={}){
 const t=(key,values)=>translate(locale,key,values);
  const asset=id=>state.assets.find(a=>a.id===id),profile=id=>state.profiles.find(p=>p.id===id);
  // Explicit saved look/version IDs are exact. Missing IDs are not a request
  // to silently supply another look's image; only an unset ID uses defaults.
  const variant=(p,id)=>id?p?.variants.find(v=>v.id===id):p?.variants.find(v=>v.id===p.def)||p?.variants[0];
  const parts=[],items=[],notes=[],warnings=[],inputMode=shot.mode.toLowerCase();
  const chosen=effectiveProfile||(effectiveRecipe?null:storyboardProfile(state.capabilities,shot.quality,inputMode,shot.entity?.data.h3?.deploymentProfileId)),support=chosen?.generation_support?.[inputMode]||effectiveRecipe;
  const constraints=support?.execution_policy_constraints||support?.constraints||support?.execution_support?.constraints||{};
  const advertised=support?{...support,mode:inputMode,execution_support:{...support,constraints}}:null;
  const schema=effectiveControlSchema(advertised),limits=support?effectiveLimits(advertised):null,nativeRoles=support?.limits?.input_count_semantics==='native_roles_v2';
  if(!support)warnings.push(t('projection.noCapabilities'));
  if(state.project.style.trim())parts.push({label:'全片风格',text:state.project.style.trim()});
  const place=profile(shot.place?.id),pv=variant(place,shot.place?.variant);
  if(shot.place?.id&&!place)warnings.push('保存的场景不存在，请重新选择。');
  if(place){if(shot.place.variant&&!pv)warnings.push('保存的场景版本不存在，请重新选择。');parts.push({label:'场景',text:`${place.name}：${place.desc}${pv?'；'+pv.name+'：'+(pv.desc||''):''}`});}
  const cast=shot.cast.map(c=>({binding:c,p:profile(c.id),v:variant(profile(c.id),c.variant)}));
  cast.forEach(({binding,p,v})=>{if(!p){warnings.push('保存的人物不存在，请重新选择。');return;}if(binding.variant&&!v)warnings.push(t('projection.missingLook',{name:p.name}));parts.push({label:'人物',text:`${p.name}：${p.desc}${v?'；'+v.name+'：'+(v.desc||''):''}`});});
  const props=shot.props.map(profile).filter(Boolean);props.forEach(p=>parts.push({label:'道具',text:`${p.name}：${p.desc}`}));
  if(shot.prompt.trim())parts.push({label:'动作',text:shot.prompt.trim()});else warnings.push('还没写这一镜的动作描述。');
  const kinds={image:'图片',video:'视频',audio:'声音'};
  const add=(id,kind,why,start)=>{
    if(!id)return;const a=asset(id),expected=['开头画面','结尾画面','图片'].includes(kind)?'image':kind==='视频'?'video':'audio';
    let reason=!a?'参考素材不存在，请重新选择':a.kind!==expected?'素材类型不符合这个输入位置':!a.fileId||a.entity?.data.missingFile?'素材文件缺失，请补传':!forSync&&!a.entity?.data.cloudAssetId?'素材尚未同步为可用的云端输入':'';
    items.push({a:a||{id,name:'已失效参考'},kind,why,sent:!reason,reason,clipStart:start});
  };
  const addFrames=()=>{
    if(shot.continuePrev){
      const previous=state.shots[state.shots.findIndex(s=>s.id===shot.id)-1],selected=previous?.versions.find(v=>v.id===previous.chosen),frame=asset(shot.previousFrame),binding=shot.entity?.data.storyboard?.previousFrame,selection=frame?.entity?.data.sourceSelection;
      const currentFrame=frame&&selected&&binding?.shotId===previous.id&&binding.selectedAssetId===previous.chosen&&selection?.kind==='artifact_frame'&&selection.position==='last'&&selection.artifact_id===selected.artifactId;
      if(!selected)warnings.push('请先选定上一镜的成片版本。');
      if(currentFrame)add(frame.id,'开头画面','接上一镜');else items.push({a:{name:'上一镜选定版本的最后一帧'},kind:'开头画面',why:'接上一镜',sent:false,reason:!selected?'上一镜尚未选定版本':'预检前从上一镜当前选定版本提取最后一帧'});
    }else add(shot.start,'开头画面','首帧');add(shot.end,'结尾画面','尾帧');
  };
  if(shot.mode==='REF'){
    // Same ordering as the saved-draft compiler: each character's main image
    // and voice, then place, props, authored extras, and ambient sound.
    cast.filter(c=>c.p).forEach(({p,v})=>{add(v?.images[0],'图片',p.name+' · '+(v?.name||''));const voice=p.voices.find(vo=>vo.asset===(v?.voice||p.voice));if(voice)add(voice.asset,'声音',t('projection.voice',{name:p.name}),voice.start);});
    if(place)add(pv?.images[0],'图片','场景');props.forEach(p=>add(p.variants[0]?.images[0],'图片',t('projection.prop',{name:p.name})));
    shot.refs.forEach(id=>{const a=asset(id),range=shot.entity?.data.storyboard?.extraRefs?.find(ref=>ref.entityId===id);add(id,kinds[a?.kind]||'图片','附加素材',a&&a.kind!=='image'?range?.clipStart??0:undefined);});
    if(place?.ambient)add(place.ambient,'声音','场景环境声',place.ambientStart??0);
    const seen=new Set(),counts={图片:0,视频:0,声音:0},durations={视频:0,声音:0},keys={图片:'max_images',视频:'max_videos',声音:'max_audios'};
    let sent=0;for(const item of items){
      const identity=JSON.stringify([item.kind,item.a.entity?.data.cloudAssetId||item.a.id,item.clipStart??null]),max=limits?.[keys[item.kind]],total=limits?.max_total_files;
      const duration=Object.hasOwn(durations,item.kind)?3:0,maximumSeconds=limits?.[item.kind==='视频'?'max_total_video_duration':'max_total_audio_duration'];
      if(seen.has(identity))item.reason='同一输入区域的素材已发送，不重复发送';
      else if(!Number.isInteger(max)||!Number.isInteger(total))item.reason='当前参考输入范围尚未读取';
      else if(counts[item.kind]>=max||sent>=total)item.reason=t('projection.countLimit',{kind:({图片:t('legacy.projection6'),视频:t('legacy.projection7'),声音:t('legacy.projection8')})[item.kind]||item.kind,max,total});
      else if(duration&&Number.isFinite(maximumSeconds)&&durations[item.kind]+duration>maximumSeconds+.01)item.reason='当前能力总参考时长上限；按优先级未发送';
      item.sent=!item.reason;if(!item.sent)continue;seen.add(identity);counts[item.kind]++;sent++;if(duration)durations[item.kind]+=duration;
    }
    if(roleSupported(advertised,'first_frame','image')||roleSupported(advertised,'last_frame','image'))addFrames();else if(shot.start||shot.end||shot.continuePrev)notes.push('首尾帧设置保留，此旧配方的全能参考中不发送。');
  }else{
    addFrames();
    if(nativeRoles){for(const id of shot.refs){const a=asset(id),slot={image:'images',video:'videos',audio:'audios'}[a?.kind],range=shot.entity?.data.storyboard?.extraRefs?.find(ref=>ref.entityId===id);if(a&&roleSupported(advertised,slot,a.kind)){add(id,kinds[a.kind],'附加控制素材',a.kind!=='image'?range?.clipStart??0:undefined);if(a.kind==='video'&&['denoise','in_context'].includes(shot.entity?.data.h3?.controls?.video_conditioning))items.at(-1).clipDuration=shot.entity?.data.h3?.controls?.duration||shot.duration;}else if(a)items.push({a,kind:kinds[a.kind],why:'附加素材',sent:false,reason:'当前FL配方不接收普通参考图片；需要时请设为时间锚点或改用REF。'});}}else if(shot.refs.length)notes.push('附加参考保留，切回全能参考时使用。');if(cast.length||place||shot.props.length)notes.push('人物、场景和道具在首尾帧模式中使用文字描述。');
  }
  if(nativeRoles){
    const guides=shot.entity?.data.h3?.guides||[];for(const guide of guides){const a=asset(guide.media_id),before=items.length;add(guide.media_id,kinds[a?.kind]||'图片','时间锚点');if(items.length>before){const item=items.at(-1);Object.assign(item,{slot:'guides',time_seconds:guide.time_seconds,use_audio:a?.kind==='video'&&guide.use_audio===true,...(guide.source_range?{source_range:guide.source_range}:{})});if(!limits||Number.isInteger(limits.max_guides)&&guides.length>limits.max_guides){item.sent=false;item.reason='当前能力的时间锚点数量上限';warnings.push(item.reason);}}}
  }
  if(shot.mode==='REF'&&!items.some(i=>i.sent))warnings.push('全能参考至少需要一份可用参考。');
  const known=value=>Number.isFinite(value)?value:'—';
  return {mode:shot.mode,frameInputs:inputMode==='fl'||support?.input_support?.first_frame===true||support?.input_support?.last_frame===true,controlInputs:inputMode==='fl'&&(support?.input_support?.video_conditioning===true||support?.input_support?.kept_soundtrack===true),kind:shot.mode==='REF'?'全能参考':shot.continuePrev?'接上一镜':shot.start&&shot.end?'首尾帧':shot.start?'首帧生成':shot.end?'尾帧生成':'文字生成',machine:chosen?.label||t('projection.noModel'),sec:shot.duration,res:shot.entity?.data.h3?.controls?.resolution||schema.resolution?.default||t('projection.unconfirmed'),parts,items,warnings,notes,durationRange:{minimum:schema.duration?.minimum,maximum:schema.duration?.maximum,step:schema.duration?.multipleOf||1},limitsLabel:limits?t('projection.limits',{images:known(limits.max_images),videos:known(limits.max_videos),audios:known(limits.max_audios),total:known(limits.max_total_files)}):'能力尚未读取'};
}
export function storyboardDrop(shot,item,slot,state){
  if(item.profile){const p=item.profile,v=p.variants.find(v=>v.id===p.def)||p.variants[0];if(['start','end'].includes(slot)&&p.type!=='character'){if(!v?.images[0])return {patch:null,msg:'档案还没有主图。'};return {patch:{[slot]:v.images[0],...(slot==='start'?{continuePrev:false}:{})},msg:'已使用档案主图。'};}
    if(p.type==='character')return shot.cast.some(c=>c.id===p.id)?{patch:null,msg:'人物已经在这一镜中。'}:{patch:{cast:[...shot.cast,{id:p.id,variant:p.def}]},msg:'已加入出场人物。',hint:'松开：加入人物'};
    if(p.type==='location')return {patch:{place:{id:p.id,variant:p.def}},msg:'已设置场景。',hint:'松开：设置场景'};
    return {patch:{props:[...new Set([...shot.props,p.id])]},msg:'已加入道具。',hint:'松开：加入道具'};
  }
  const a=item.asset;if(!a)return {patch:null,msg:'素材不存在。'};
  if(['start','end'].includes(slot)){if(a.kind!=='image')return {patch:null,msg:'首尾帧需要图片。'};return {patch:{[slot]:a.id,...(slot==='start'?{continuePrev:false}:{})},msg:'已设置'+(slot==='start'?'首帧':'尾帧')+'。'};}
  if(slot!=='refs'&&shot.mode==='FL'&&a.kind==='image')return {patch:{[!shot.start&&!shot.continuePrev?'start':'end']:a.id},msg:'已设置画面。',hint:'松开：设置画面'};
  const selected=storyboardProfile(state.capabilities,shot.quality,shot.mode.toLowerCase(),shot.entity?.data.h3?.deploymentProfileId),supported=roleSupported({mode:shot.mode.toLowerCase(),input_support:selected?.generation_support?.[shot.mode.toLowerCase()]?.input_support},{image:'images',video:'videos',audio:'audios'}[a.kind],a.kind);
  return {patch:{refs:[...new Set([...shot.refs,a.id])]},msg:shot.mode==='FL'&&!supported?'参考已保存，切到全能参考时发送。':'已加入附加参考，控制方式由预检核对。',hint:'松开：加入参考'};
}
export function storyboardSnapshot(project,shotId){const shot=find(project,shotId),chap=ancestors(project,shotId).find(e=>e.type==='chapter'),shots=chap?chapterShots(project,chap.id):[],at=shots.findIndex(s=>s.id===shotId);return {shot,style:project.journey?.storyboard?.style||'',profiles:project.entities.filter(e=>['character','location','prop'].includes(e.type)),assets:project.entities.filter(e=>['image','video','audio'].includes(e.type)),previous:shot?.data.storyboard?.continuePrevious?shots[at-1]?.data.selectedAssetId:null};}
export function patchStoryboardShot(project,id,patch){const shot=find(project,id);if(!shot)throw Error('镜头不存在。');
  if(!shot.data.storyboard&&!Object.keys(patch).some(key=>['cast','place','props','continuePrev'].includes(key))&&(!Object.hasOwn(patch,'mode')||shot.data.h3?.explicitReferences)){
    for(const key of ['script','prompt'])if(Object.hasOwn(patch,key))shot.data[key]=patch[key];
    if(Object.hasOwn(patch,'title'))shot.title=patch.title;
    if(Object.hasOwn(patch,'quality'))shot.data.qualityIntent=patch.quality;
    if(Object.hasOwn(patch,'duration')){shot.data.seconds=patch.duration;shot.data.h3={...shot.data.h3,controls:{...shot.data.h3?.controls,duration:patch.duration}};}
    if(Object.hasOwn(patch,'seedLock'))shot.data.h3={...shot.data.h3,controls:{...shot.data.h3?.controls,seed:patch.seedLock?(shot.data.h3?.controls?.seed??project.jobs.findLast(j=>j.client_ref?.shot_id===id)?.effective_request?.seed??'1'):null}};
    if(Object.hasOwn(patch,'mode'))shot.data.h3={...shot.data.h3,inputMode:patch.mode.toLowerCase(),recipeId:patch.mode.toLowerCase()==='ref'?'h3-base-ref2va-v1':'h3-base-fl2va-v1'};
    for(const [field,role] of [['start','firstFrame'],['end','lastFrame']])if(Object.hasOwn(patch,field)){
      project.links=project.links.filter(l=>!(l.target===id&&l.role===role));if(patch[field])project.links.push({id:uid('link'),source:patch[field],target:id,role});
    }
    if(Object.hasOwn(patch,'refs')){
      const roles=['reference','identity','motion','audio'],old=project.links.filter(l=>l.target===id&&roles.includes(l.role));
      project.links=project.links.filter(l=>!(l.target===id&&roles.includes(l.role))||patch.refs.includes(l.source));
      for(const source of patch.refs)if(!old.some(l=>l.source===source))project.links.push({id:uid('link'),source,target:id,role:find(project,source)?.type==='audio'?'audio':'reference'});
    }
    shot.version++;if(shot.data.selectedAssetId)shot.status='review';return;
  }
const b={version:1,mode:mode(shot),quality:'draft',place:null,propIds:[],firstFrameId:'',lastFrameId:'',continuePrevious:false,extraRefs:[],...shot.data.storyboard};
  if(Object.hasOwn(patch,'title'))shot.title=patch.title;if(Object.hasOwn(patch,'script'))shot.data.script=patch.script;if(Object.hasOwn(patch,'prompt'))shot.data.prompt=patch.prompt;
  if(Object.hasOwn(patch,'duration')){shot.data.seconds=patch.duration;shot.data.h3={...shot.data.h3,controls:{...shot.data.h3?.controls,duration:patch.duration}};}
  for(const [from,to] of Object.entries({mode:'mode',quality:'quality',start:'firstFrameId',end:'lastFrameId',continuePrev:'continuePrevious',props:'propIds'}))if(Object.hasOwn(patch,from))b[to]=from==='mode'?patch[from].toLowerCase():patch[from];
  if(Object.hasOwn(patch,'cast'))shot.data.cast=patch.cast.map(c=>({characterId:c.id,lookId:c.variant||''}));if(Object.hasOwn(patch,'place'))b.place=patch.place?{locationId:patch.place.id,versionId:patch.place.variant||''}:null;
  if(Object.hasOwn(patch,'refs'))b.extraRefs=patch.refs.map(entityId=>b.extraRefs.find(r=>r.entityId===entityId)||{entityId});
  if(Object.hasOwn(patch,'seedLock'))shot.data.h3={...shot.data.h3,controls:{...shot.data.h3?.controls,seed:patch.seedLock?(project.jobs.findLast(j=>j.client_ref?.shot_id===id)?.effective_request?.seed||'1'):null}};
  if(Object.hasOwn(patch,'mode'))shot.data.h3={...shot.data.h3,inputMode:b.mode,recipeId:b.mode==='ref'?'h3-base-ref2va-v1':'h3-base-fl2va-v1'};
  shot.data.storyboard=b;shot.version++;if(shot.data.selectedAssetId)shot.status='review';
}
export function newStoryboardEntity(project,type,title,parentId=null,data={}){const entity={id:uid(type),type,title,description:'',parentId,order:children(project,parentId).length,version:1,status:'draft',data};project.entities.push(entity);return entity;}
