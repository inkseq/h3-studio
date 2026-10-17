import {storyboardCommandStore,verifyStoryboardReceipt} from './storyboard-commands.js';
import {boundVideoRange,videoBinding} from './video-cut-model.js';
import {createCloudClient,safeCloudPath} from './cloud-client.js';
import {store as defaultStore} from './store.js';
import {getFile,setCloudMediaHandlers} from './media.js';
import {executionPresetControls,selectedRecipe,selectedProfileRecipe,blankCloudProject,freestyleProject,copyForCloud,checkedProjectRecord,referenceSpecs,shotSnapshot,stableJSON,digest,buildPlanPayload,mergeJobCandidate,activeJobStatuses} from './cloud-model.js';
import {roughCutSnapshot,roughCutJobs} from './roughcut-model.js';
import {burnCaptionStatus} from './caption-model.js';
import {requireModeAvailable} from './generation-availability.js';
import {chapterShots,storyboardProfile,storyboardView,expandStoryboard} from './storyboard-domain.js';

const clone=value=>structuredClone(value),uid=()=>crypto.randomUUID();
const identity=result=>result?.username||result?.user?.username||result?.user||result?.account?.username;
export function createCloudController({store=defaultStore,client,storage=globalThis.localStorage,workspaceStorage,readFile=getFile,mediaHandlers=setCloudMediaHandlers}={}){
  if(workspaceStorage===undefined){try{workspaceStorage=globalThis.sessionStorage;}catch{workspaceStorage=null;}}
  let state={account:null,auth:null,capabilities:null,availability:null,availabilityChecking:false,operatorAccess:false,storyboardEnabled:false,projects:[],projectsLoaded:false,nextProjectOffset:100,moreProjects:false,plans:{},preflightErrors:{},batchChecking:null,renderPlans:{},renderOptions:{},renderPending:{},jobs:[],activity:null,activityError:'',nextJobOffset:100,moreJobs:false,batches:[],batchesLoaded:false,nextBatchOffset:10,moreBatches:false,assets:[],usage:null,assetError:'',batchPending:null,busy:false,error:'',message:'',conflict:null,draftOffer:null,resumeOffer:null,resumeError:'',remoteUpdate:null,remoteUpdateError:'',unknown:{}},epoch=0,stopped=false,polling=false,operation=false,detailCursor=0,sessionCheck=null,lastRemoteCheck=0,lastCapabilitiesCheck=0,capabilityRequest=0,availabilityRequest=0,lastAvailabilityCheck=0;
  const listeners=new Set(),emit=patch=>{state={...state,...patch};for(const cb of listeners)cb();};
  const api=client||createCloudClient({onUnauthorized:()=>expire()});
  const current=()=>store.getState(),context=()=>({epoch,workspaceEpoch:current().workspaceEpoch,projectId:current().project.id,account:state.account});
  // Per-tab navigation memory only: never store private titles, project bodies
  // or authentication here. A fresh /me check must precede any resume offer.
  const visitKey='yingxu-workspace-visit-v1';
  const rememberVisit=value=>{try{workspaceStorage?.setItem(visitKey,JSON.stringify(value));}catch{/* Draft persistence is independent; navigation memory is optional. */}};
  function offerResume(){
    if(!state.account||current().workspace.mode!=='local')return;
    try{const visit=JSON.parse(workspaceStorage?.getItem(visitKey)||'null');if(visit?.mode==='cloud'&&visit.account===state.account&&typeof visit.projectId==='string'&&visit.projectId.length>0&&visit.projectId.length<=160)emit({resumeOffer:{projectId:visit.projectId}});}catch{/* Ignore malformed navigation memory; never infer an owner. */}
  }
  const guard=ctx=>{if(ctx.epoch!==epoch||ctx.workspaceEpoch!==current().workspaceEpoch||ctx.projectId!==current().project.id||ctx.account!==state.account)throw Error('账户或项目已经切换；迟到的响应没有写入当前作品。');};
  const requireCloud=()=>{if(!state.account||current().workspace.mode!=='cloud'||current().workspace.account!==state.account)throw Error('请先登录并打开一个云项目。');};
  const intent=(kind,scope)=>{if(!storage)throw Error('浏览器无法保存提交标识；为避免重复提交，请先恢复存储。');const key=`yingxu-cloud-intent-v1:${state.account}:${kind}:${scope}`;let value=storage.getItem(key);if(!value){value=uid();storage.setItem(key,value);}return value;};
  const pendingKey=()=>`yingxu-cloud-pending-v1:${state.account}:${current().project.id}`;
  const readPending=()=>{try{return JSON.parse(storage?.getItem(pendingKey())||'{}');}catch{throw Error('未确认任务的记录无法读取。请保留浏览器数据并先核对服务任务。');}};
  const writePending=value=>{if(!storage)throw Error('无法保留提交记录，未提交任务。');storage.setItem(pendingKey(),JSON.stringify(value));};
  const readBatchPending=()=>{try{return JSON.parse(storage?.getItem(pendingKey()+':batch')||'null');}catch{throw Error('未确认批次记录无法读取，请先核对云端任务。');}};
  const writeBatchPending=value=>{if(!storage)throw Error('无法保留批次标识，未提交。');storage.setItem(pendingKey()+':batch',JSON.stringify(value));emit({batchPending:value});};
  const readRenderPending=()=>{try{return JSON.parse(storage?.getItem(pendingKey()+':render')||'{}');}catch{throw Error('未确认粗剪记录无法读取，请保留浏览器数据并核对任务。');}};
  const writeRenderPending=value=>{if(!storage)throw Error('无法保存粗剪提交标识，未提交。');storage.setItem(pendingKey()+':render',JSON.stringify(value));emit({renderPending:value});};
  const sessionEvent='yingxu-cloud-session-change-v1';
  const announceSession=()=>{try{storage?.setItem(sessionEvent,uid());}catch{/* No password or session cookie is ever stored here. */}};
  const sessionChanged=event=>{if(event.key===sessionEvent&&state.account)expire();};
  globalThis.window?.addEventListener('storage',sessionChanged);
  function installMedia(){api.setAccount?.(state.account);mediaHandlers({maxBytes:state.capabilities?.upload_max_bytes,read:state.account?async fileId=>{const match=/^cloud_(asset|artifact)_([A-Za-z0-9_-]+)$/.exec(fileId);if(!match)throw Error('云素材标识无效。');const ctx=context(),blob=await api.download(`/v1/${match[1]}s/${match[2]}/content`);guard(ctx);return blob;}:null,
    native:state.account&&current().workspace.mode==='cloud'?(fileId,{download=false}={})=>{
      requireCloud();const ctx=context(),match=/^cloud_(asset|artifact)_([A-Za-z0-9_-]+)$/.exec(fileId);
      if(!match)throw Error('client.mediaPath');
      const href=api.nativeContentURL(`/v1/${match[1]}s/${match[2]}/content`,{download});guard(ctx);
      return Object.freeze({href,account:ctx.account,projectId:ctx.projectId,workspaceEpoch:ctx.workspaceEpoch,assertCurrent:()=>guard(ctx)});
    }:null,upload:state.account&&current().workspace.mode==='cloud'?upload:null});}
  function replaceIdentity(account,message=''){
    epoch++;lastAvailabilityCheck=0;api.reset?.();store.leaveCloudProject();
    emit({...emptyProjectList,account,capabilities:null,availability:null,availabilityChecking:false,operatorAccess:false,storyboardEnabled:false,capabilityError:'',assets:[],usage:null,assetError:'',plans:{},preflightErrors:{},batchChecking:null,renderPlans:{},renderOptions:{},renderPending:{},jobs:[],activity:null,activityError:'',nextJobOffset:100,moreJobs:false,batches:[],batchesLoaded:false,nextBatchOffset:10,moreBatches:false,batchPending:null,conflict:null,draftOffer:null,resumeOffer:null,resumeError:'',remoteUpdate:null,remoteUpdateError:'',unknown:{},error:'',message});installMedia();
  }
  function expire(){replaceIdentity(null);emit({error:'云登录已失效或身份已改变。原本机作品已恢复，云草稿仍按原账户保留，请重新核对登录。'});}
  async function recheckSession({loadLists=false,refreshCapabilities=false}={}){
    if(sessionCheck)return sessionCheck;
    let observedEpoch=epoch;
    sessionCheck=(async()=>{
      try{
        const accountRecord=await api.me(),me=identity(accountRecord);if(observedEpoch!==epoch)return;
        if(typeof me!=='string'||!me)throw Error('服务没有返回可核对的账户身份。');
        const changed=state.account!==me||current().workspace.mode==='cloud'&&current().workspace.account!==me;
        if(changed){replaceIdentity(me,'登录身份已重新核对。原本机作品已恢复；请打开当前账户的云项目。');observedEpoch=epoch;}
        emit({operatorAccess:accountRecord.operator_capacity?.view===true,storyboardEnabled:accountRecord.features?.storyboard_workspace===true});
        if(loadLists||changed)await reloadProjects();if(observedEpoch===epoch&&(loadLists||changed||refreshCapabilities||Date.now()-lastCapabilitiesCheck>15000))await reloadCapabilities();
        if(observedEpoch===epoch)offerResume();
      }catch(error){if(observedEpoch!==epoch)return;if(error.status===401){if(state.account||current().workspace.mode==='cloud')expire();}else emit({error:'账户状态暂未核对：'+error.message});}
      finally{sessionCheck=null;}
    })();return sessionCheck;
  }
  const focusSession=()=>{if(state.auth)void recheckSession({refreshCapabilities:true});};
  const visibleSession=()=>{if(globalThis.document?.visibilityState==='visible')focusSession();};
  globalThis.window?.addEventListener('focus',focusSession);
  globalThis.document?.addEventListener('visibilitychange',visibleSession);
  async function action(fn){if(operation)throw Error('上一项云操作还在处理中。');operation=true;emit({busy:true,error:'',message:''});try{return await fn();}catch(error){emit({error:error.message});throw error;}finally{operation=false;emit({busy:false});}}
  const emptyProjectList={projects:[],projectsLoaded:false,nextProjectOffset:100,moreProjects:false};
  const accountContext=()=>({epoch,account:state.account});
  const guardAccount=ctx=>{if(!ctx.account||ctx.epoch!==epoch||ctx.account!==state.account)throw Error('账户已经改变，旧账户列表没有写入。');};
  async function reloadProjects({replace=false}={}){const ctx=accountContext(),result=await api.projects({limit:100,offset:0});guardAccount(ctx);const page=result.projects||[],retained=replace?[]:state.projects.filter(p=>!page.some(item=>item.id===p.id));emit({projects:[...page,...retained],projectsLoaded:true,nextProjectOffset:!replace&&state.projectsLoaded?state.nextProjectOffset:page.length,moreProjects:!replace&&state.projectsLoaded&&state.nextProjectOffset>100?state.moreProjects:page.length===100});return result;}
  async function loadMoreProjects(){if(!state.projectsLoaded)return reloadProjects();const ctx=accountContext(),offset=state.nextProjectOffset,result=await api.projects({limit:100,offset});guardAccount(ctx);const page=result.projects||[],merged=[...new Map([...state.projects,...page].map(project=>[project.id,project])).values()];emit({projects:merged,nextProjectOffset:offset+page.length,moreProjects:page.length===100,message:page.length?'已载入更早项目；新项目或排序变化可能使分页重叠。':'这次没有读到更早项目。'});return result;}
  async function reloadCapabilities(){const ctx=accountContext(),request=++capabilityRequest;try{const capabilities=await api.capabilities();guardAccount(ctx);if(request===capabilityRequest){lastCapabilitiesCheck=Date.now();emit({capabilities,capabilityError:''});installMedia();}await reloadAvailability();return capabilities;}catch(error){if(ctx.epoch===epoch&&request===capabilityRequest)emit({capabilities:null,capabilityError:'服务状态暂未核对，请重试；草稿已保留。'});throw error;}}
  async function reloadAvailability({fresh=false}={}){
    if(!state.account)return null;
    if(!fresh&&Date.now()-lastAvailabilityCheck<10000)return state.availability;
    const ctx=accountContext(),request=++availabilityRequest;emit({availabilityChecking:true});
    try{const availability=await api.generationAvailability();guardAccount(ctx);if(request!==availabilityRequest)return null;if(request===availabilityRequest){lastAvailabilityCheck=Date.now();emit({availability});}return availability;}
    catch{guardAccount(ctx);if(request===availabilityRequest){lastAvailabilityCheck=Date.now();emit({availability:null});}return null;}
    finally{if(ctx.epoch===epoch&&ctx.account===state.account&&request===availabilityRequest)emit({availabilityChecking:false});}
  }
  async function assetInventory(){requireCloud();const ctx=context();try{const [result,usage]=await Promise.all([api.assets(ctx.projectId),api.storageUsage()]);guard(ctx);emit({assets:result.assets||[],usage,assetError:''});return result.assets||[];}catch(error){guard(ctx);emit({assetError:error.message});throw error;}}
  function attachRecoveredAsset(asset){requireCloud();if(asset.project_id!==current().project.id||asset.status!=='ready'||!['image','video','audio'].includes(asset.kind))throw Error('素材尚未就绪或属于其他项目，未加入素材库。');if(current().project.entities.some(e=>e.data.cloudAssetId===asset.id))return;if(asset.content_url)safeCloudPath(asset.content_url);const id=store.addEntity(asset.kind,null,{title:asset.file_name||'已恢复素材',data:{fileId:'cloud_asset_'+asset.id,cloudAssetId:asset.id,cloudContentPath:asset.content_url||`/v1/assets/${asset.id}/content`,fileName:asset.file_name||'reference',mime:asset.mime||asset.metadata?.mime||'',...(Number.isFinite(asset.metadata?.bytes)?{bytes:asset.metadata.bytes}:{}),metadata:asset.metadata||{},source:'upload',missingFile:false}});if(!id)throw Error(current().notice);emit({message:'素材已加入当前项目素材库，请明确选择镜头和用途；尚未提交生成。'});}
  async function open(id,{restoreDraft=false,discardDraft=false}={}){const me=state.account,ctx=accountContext();if(!me)throw Error('先登录云工作室。');const response=await api.project(id);guardAccount(ctx);const record=checkedProjectRecord(response);const draft=store.getCloudDraft(me,id);
    if(draft?.dirty&&!restoreDraft&&!discardDraft){emit({draftOffer:{record,draft}});return false;}
    if(restoreDraft&&draft?.serverVersion!==record.version){emit({draftOffer:{record,draft},conflict:{id,remote:record,draft}});throw Error('本机云草稿基于较旧版本。请下载草稿或保存副本；未覆盖云端。');}
    epoch++;api.reset?.();if(!store.enterCloudProject(record.project,{account:me,version:record.version,restoreDraft}))throw Error(current().notice);
    rememberVisit({mode:'cloud',account:me,projectId:record.id});emit({resumeOffer:null,resumeError:'',remoteUpdate:null,remoteUpdateError:''});
    const pending=readPending(),renderPending=readRenderPending();emit({preflightErrors:{},batchChecking:null,renderPlans:Object.fromEntries(Object.entries(renderPending).map(([id,value])=>[id,value.plan])),renderOptions:{},renderPending,assets:[],usage:null,assetError:'',plans:Object.fromEntries(Object.entries(pending).map(([id,value])=>[id,value.plan])),jobs:[],activity:null,activityError:'',nextJobOffset:100,moreJobs:false,batches:[],batchesLoaded:false,nextBatchOffset:10,moreBatches:false,batchPending:readBatchPending(),conflict:null,draftOffer:null,unknown:pending,message:restoreDraft?'已恢复尚未同步的云草稿。':'云项目已打开。'});installMedia();await refreshJobs();return true;}
  async function save(){requireCloud();const ctx=context(),project=store.exportProject(),version=current().workspace.serverVersion;
    try{const record=checkedProjectRecord(await api.saveProject(project.id,project,version));guard(ctx);store.markCloudSaved(record.version,project);emit({conflict:null,remoteUpdate:null,remoteUpdateError:'',message:'项目版本已保存。'});return record;}
    catch(error){guard(ctx);if(error.status===409){emit({conflict:{id:project.id,draft:project,baseVersion:version}});throw Error('另一处已更新这个项目。当前草稿已保留；请保存为云副本或下载后比较，未覆盖远端。');}throw error;}}
  async function upload(file,type,clientAssetId){requireCloud();const ctx=context();const asset=await api.upload(file,ctx.projectId,clientAssetId||uid(),intent('upload',ctx.projectId+':'+(clientAssetId||uid())));guard(ctx);
    if(!asset?.asset_id||asset.status&&asset.status!=='ready')throw Error('素材尚未处理完成，未当作可生成的输入。');
    if(asset.content_url)safeCloudPath(asset.content_url);
    return {type:asset.kind||type,data:{fileId:'cloud_asset_'+asset.asset_id,cloudAssetId:asset.asset_id,cloudContentPath:asset.content_url||`/v1/assets/${asset.asset_id}/content`,fileName:file.name||'reference',mime:asset.metadata?.mime||file.type,bytes:file.size,metadata:asset.metadata||{},source:'upload',missingFile:false}};}
  async function syncEntity(id){requireCloud();const p=current().project,asset=p.entities.find(e=>e.id===id);if(!asset||!['image','video','audio'].includes(asset.type))throw Error('参考素材不存在。');if(asset.data.cloudAssetId)return asset.data.cloudAssetId;
    const ctx=context(),oldFile=asset.data.fileId,blob=oldFile?await readFile(oldFile):null;if(!blob)throw Error(`「${asset.title}」的本机文件缺失，请补传。`);guard(ctx);
    const file=new File([blob],asset.data.fileName||`${id}.${{image:'png',video:'mp4',audio:'wav'}[asset.type]}`,{type:asset.data.mime||blob.type});
    const result=await upload(file,asset.type,id+':'+oldFile);guard(ctx);
    const latest=current().project.entities.find(e=>e.id===id);if(latest?.data.fileId!==oldFile)throw Error('上传时原素材已更换，请重新预检。');
    if(!store.editProject(project=>{const entity=project.entities.find(e=>e.id===id);entity.data={...entity.data,...result.data};for(const item of project.entities)if(boundVideoRange(item.data.selectedVideoRange,asset))item.data.selectedVideoRange={...item.data.selectedVideoRange,...videoBinding(entity)};for(const item of project.entities)for(const range of Object.values(item.data.referenceRanges||{}))if(range.fileId===oldFile)range.fileId=result.data.fileId;for(const item of project.entities)for(const guide of item.data.h3?.guides||[])if(guide.source_range?.fileId===oldFile)guide.source_range.fileId=result.data.fileId;for(const tracks of Object.values(project.journey?.soundTracks||{}))for(const track of tracks)if(track.fileId===oldFile)track.fileId=result.data.fileId;},{history:false}))throw Error(current().notice);
    return result.data.cloudAssetId;}
  function assertNotHeld(key,id){if(state.jobs.some(job=>job.status==='recovery_hold'&&job.client_ref?.[key]===id&&(key!=='chapter_id'||job.recipe_id==='chapter-roughcut-v1')))throw Error('原任务恢复后待核对，请等待管理员核对执行与费用；可以继续编辑作品，不能重新提交。');}
  async function extractFrame(artifactId,selection){
    requireCloud();const ctx=context(),key=intent('frame',await digest({project:ctx.projectId,artifactId,selection}));guard(ctx);
    const asset=await api.saveFrame(ctx.projectId,artifactId,selection,key);guard(ctx);
    if(!asset?.asset_id||asset.status!=='ready'||asset.project_id!==ctx.projectId||asset.kind!=='image'||asset.selection?.artifact_id!==artifactId)throw Error('截帧收据尚未确认；未加入档案或替代原参考。');
    const existing=current().project.entities.find(e=>e.data.cloudAssetId===asset.asset_id);if(existing)return existing.id;
    const id=store.addEntity('image',null,{title:asset.file_name||'成片截帧',data:{fileId:'cloud_asset_'+asset.asset_id,cloudAssetId:asset.asset_id,cloudContentPath:asset.content_url,fileName:asset.file_name,mime:asset.metadata?.mime||'image/png',bytes:asset.metadata?.bytes||0,metadata:asset.metadata||{},source:'artifact_frame',sourceSelection:asset.selection,storyboardInbox:true,missingFile:false}});if(!id)throw Error(current().notice);return id;
  }
  async function prepareStoryboard(shotId,{sourceJobId}={}){
    requireCloud();assertNotHeld('shot_id',shotId);const ctx=context();if(readPending()[shotId]||readBatchPending()?.shotIds.includes(shotId))throw Error('原提交尚未核对，请先刷新原记录。');
    const plans={...state.plans};delete plans[shotId];emit({plans,preflightErrors:{...state.preflightErrors,[shotId]:null}});
    try{
    await reloadCapabilities();guard(ctx);let shot=current().project.entities.find(e=>e.id===shotId);if(!shot)throw Error('镜头不存在。');
    const b=shot.data.storyboard;if(!b)throw Error('请先保存分镜配置。');const selected=storyboardProfile(state.capabilities,b.quality,b.mode,shot.data.h3?.deploymentProfileId);if(!selected||selected.generation_support?.[b.mode]?.enabled!==true||(b.quality==='final'?selected.model_id!=='MiniMax-H3-Base-INT8':selected.model_id!=='MiniMax-H3-Pruned-Rank8-INT8'))throw Error('所选模型与草稿/精修配置不一致或尚不可用；未使用另一模型替代。');
    if(state.jobs.some(j=>j.client_ref?.shot_id===shotId&&activeJobStatuses.has(j.status)))throw Error('这一镜已有任务排队或执行，请查看原任务。');
    if(b.continuePrevious&&(b.mode==='fl'||selected.generation_support?.[b.mode]?.input_support?.first_frame===true)&&!sourceJobId){
      const chapter=current().project.entities.find(e=>e.type==='chapter'&&chapterShots(current().project,e.id).some(s=>s.id===shotId)),shots=chapter?chapterShots(current().project,chapter.id):[],previous=shots[shots.findIndex(s=>s.id===shotId)-1],chosen=current().project.entities.find(e=>e.id===previous?.data.selectedAssetId);
      if(!previous||!chosen?.data.cloudArtifactId||chosen.type!=='video')throw Error('请先选定上一镜的真实成片；不会使用未选定候选。');
      if(b.previousFrame?.selectedAssetId!==chosen.id||!current().project.entities.some(e=>e.id===b.previousFrame?.frameAssetId)){
        const frameId=await extractFrame(chosen.data.cloudArtifactId,{position:'last'});guard(ctx);const latest=current().project.entities.find(e=>e.id===previous.id);if(latest?.data.selectedAssetId!==chosen.id)throw Error('截帧期间上一镜的选择已改变，请重新预检。');
        store.updateEntity(shotId,{data:{storyboard:{...current().project.entities.find(e=>e.id===shotId).data.storyboard,previousFrame:{shotId:previous.id,selectedAssetId:chosen.id,frameAssetId:frameId}}}});
      }
    }
    // Upload only inputs the chosen mode will send. A generated artifact reused
    // as a reference gets an ordinary owned asset receipt through this same path.
    if(!sourceJobId){const p=current().project,chapter=p.entities.find(e=>e.type==='chapter'&&chapterShots(p,e.id).some(s=>s.id===shotId)),view=storyboardView(p,{chapterId:chapter?.id,capabilities:state.capabilities,jobs:state.jobs}),s=view.shots.find(s=>s.id===shotId);for(const id of new Set(expandStoryboard(s,view,{forSync:true}).items.filter(i=>i.sent&&i.a?.id).map(i=>i.a.id))){if(!p.entities.find(e=>e.id===id)?.data.cloudAssetId){await syncEntity(id);guard(ctx);}}}
    const saved=await save();guard(ctx);const snapshot=current().project,localHash=await digest(shotSnapshot(snapshot,shotId));guard(ctx);
    let preview,plan,recipeId;
    if(sourceJobId){
      const source=await api.job(sourceJobId);guard(ctx);if(source.status!=='succeeded'||source.client_ref?.project_id!==ctx.projectId||source.client_ref?.shot_id!==shotId||!source.effective_request)throw Error('原选定成片的请求尚未确认，不能用当前编辑草稿代替。');
      const chosen=snapshot.entities.find(e=>e.id===shotId)?.data.selectedAssetId,candidate=snapshot.entities.find(e=>e.id===chosen);if(!candidate||candidate.data.sourceJobId!==sourceJobId||candidate.type!=='video')throw Error('精修必须使用当前选定成片的原请求；选择已改变，请重新预检。');
      const request=source.effective_request;recipeId=source.recipe_id;const recipe=state.capabilities.recipes.find(r=>r.id===recipeId),support=selected.generation_support?.[request.mode];if(!support||b.mode!==request.mode)throw Error('精修模型或模式与原选定请求不一致。');
      const controls=Object.fromEntries(Object.keys(support.controls||recipe?.controls||{}).filter(k=>Object.hasOwn(request,k)&&!['guides','video_audio'].includes(k)).map(k=>[k,request[k]]));
      if(!snapshot.entities.find(e=>e.id===shotId)?.data.h3?.controls?.seed)controls.seed=null;
      const inputs={images:(request.inputs.images||[]).map(asset_id=>({asset_id})),videos:(request.inputs.videos||[]).map(asset_id=>({asset_id,include_audio:request.video_audio?.[asset_id]===true})),audios:(request.inputs.audios||[]).map(asset_id=>({asset_id})),first_frame:request.inputs.first_frame?{asset_id:request.inputs.first_frame}:null,last_frame:request.inputs.last_frame?{asset_id:request.inputs.last_frame}:null,guides:request.guides||[]};
      const payload={recipe_id:recipeId,deployment_profile_id:selected.id,capabilities_version:state.capabilities.capabilities_version,client_ref:{...source.client_ref,shot_version:snapshot.entities.find(e=>e.id===shotId).version,source_hash:localHash},prompt:request.prompt,controls,inputs,client_edit:{edit_duration_s:request.duration}};
      plan=await api.plan(payload,intent('final-plan',await digest(payload)));guard(ctx);preview={draft:{prompt:request.prompt,storyboard:{notes:['精修使用选定版本的原提示词、实际输入和参数；旧成片保留。']}}};
    }else{
      preview=await api.generationDraft(ctx.projectId,shotId);guard(ctx);if(preview.project_version!==saved.version||preview.issues?.length)throw Error(preview.issues?.join(' ')||'草稿版本已经改变，请重新预检。');
      recipeId=preview.draft.recipe_id;if(preview.draft.deployment_profile_id!==selected.id)throw Error('服务端模型档案与界面所选不一致，未提交。');
      plan=await api.savedPlan(ctx.projectId,shotId,{expected_version:saved.version,capabilities_version:state.capabilities.capabilities_version},intent('story-plan',ctx.projectId+':'+shotId+':'+saved.version));guard(ctx);
    }
    if(localHash!==await digest(shotSnapshot(current().project,shotId)))throw Error('预检期间镜头或档案已改变，旧计划未当作可提交计划。');
    if(!plan.plan_id||!['ready','blocked'].includes(plan.status))throw Error('生成计划响应无效。');
    if(plan.effective_request?.model!==selected.model_id||plan.effective_request?.mode!==b.mode)throw Error('实际预检模型或模式与所选配置不一致，未提交。');
    emit({plans:{...state.plans,[shotId]:{...plan,storyboardPreview:preview.draft.storyboard,availabilitySettings:{deployment_profile_id:selected.id,recipe_id:recipeId},sourceHash:localHash,sourceSnapshot:stableJSON(shotSnapshot(snapshot,shotId)),shotId,projectId:ctx.projectId}},message:'预检已完成，请核对实际发送输入和预算后确认。'});return plan;
    }catch(error){guard(ctx);const plans={...state.plans};delete plans[shotId];emit({plans,preflightErrors:{...state.preflightErrors,[shotId]:error.message}});throw error;}
  }
  async function prepare(shotId){requireCloud();assertNotHeld('shot_id',shotId);if(readPending()[shotId]||readBatchPending()?.shotIds.includes(shotId))throw Error('这个镜头有未确认的提交。先刷新任务或用原标识核对提交，不能创建新计划绕过。');const ctx=context();emit({preflightErrors:{...state.preflightErrors,[shotId]:null}});const currentHash=await digest(shotSnapshot(current().project,shotId));guard(ctx);if(state.jobs.some(job=>job.client_ref?.shot_id===shotId&&job.client_ref.source_hash===currentHash&&activeJobStatuses.has(job.status)))throw Error('这个镜头的相同版本已在队列中，请查看进度或先取消原任务。');await reloadCapabilities();guard(ctx);
    if(current().project.entities.find(e=>e.id===shotId)?.data.storyboard)return prepareStoryboard(shotId);
    let shot=current().project.entities.find(e=>e.id===shotId);if(!shot)throw Error('镜头不存在。');const recipe=selectedProfileRecipe(state.capabilities,shot.data.h3);if(!recipe)throw Error('此生成配方已不可用，请刷新并重新选择。');
    const runtime=executionPresetControls(recipe),saved=shot.data.h3?.controls||{};
    if(Object.entries(runtime).some(([field,value])=>saved[field]!==value)){
      if(!store.updateEntity(shotId,{data:{h3:{...shot.data.h3,controls:{...saved,...runtime}}}}))throw Error('自动匹配云端设置未保存，请重新预检。');
    }

    const matchedHash=await digest(shotSnapshot(current().project,shotId));guard(ctx);if(state.jobs.some(job=>job.client_ref?.shot_id===shotId&&job.client_ref.source_hash===matchedHash&&activeJobStatuses.has(job.status)))throw Error('这个镜头的相同版本已在队列中，请查看进度或先取消原任务。');
    let refs=referenceSpecs(current().project,shotId,{mode:recipe.mode});if(refs.issues.length)throw Error(refs.issues.join(' '));
    for(const id of new Set([...refs.references.map(r=>r.entity.id),...(shot.data.h3?.guides||[]).map(g=>g.media_id)])){await syncEntity(id);guard(ctx);}
    refs=referenceSpecs(current().project,shotId,{mode:recipe.mode});const assetMap={};for(const ref of refs.references){if(ref.range){const derivative=await api.derivative(ref.entity.data.cloudAssetId,ref.range,intent('clip',await digest({id:ref.entity.data.cloudAssetId,start:ref.range.start,end:ref.range.end})));guard(ctx);if(!derivative.asset_id||derivative.status!=='ready')throw Error('选段仍在处理中，请稍后重新预检；未使用整个原文件替代。');assetMap[ref.key]=derivative.asset_id;}}
    const updated=current().project,liveShot=updated.entities.find(e=>e.id===shotId);for(const [index,guide]of (liveShot.data.h3?.guides||[]).entries()){if(!guide.source_range)continue;const entity=updated.entities.find(e=>e.id===guide.media_id),range=guide.source_range;if(range.fileId!==entity?.data.fileId||!Number.isFinite(range.start)||!Number.isFinite(range.end)||range.start<0||range.end<=range.start)throw Error('时间锚点的原文件已改变或选段无效，请重设锚点。');const derivative=await api.derivative(entity.data.cloudAssetId,range,intent('clip',await digest({id:entity.data.cloudAssetId,start:range.start,end:range.end})));guard(ctx);if(!derivative.asset_id||derivative.status!=='ready')throw Error('锚点选段尚未就绪，没有使用完整原文件替代。');assetMap['guide:'+index]=derivative.asset_id;}
    await save();guard(ctx);const snapshot=current().project,hash=await digest(shotSnapshot(snapshot,shotId)),payload=buildPlanPayload(snapshot,shotId,{recipe,capabilitiesVersion:state.capabilities.capabilities_version,assetMap,sourceHash:hash});
    const plan=await api.plan(payload,intent('plan',snapshot.id+':'+shotId+':'+hash));guard(ctx);
    if(!plan.plan_id||!['ready','blocked'].includes(plan.status))throw Error('服务返回了无效的生成计划。');
    emit({plans:{...state.plans,[shotId]:{...plan,availabilitySettings:{deployment_profile_id:payload.deployment_profile_id||null,recipe_id:payload.recipe_id},sourceHash:hash,sourceSnapshot:stableJSON(shotSnapshot(snapshot,shotId)),shotId,projectId:snapshot.id}},message:plan.status==='ready'?'预检通过；核对生效参数后再提交。':'预检已完成，仍有执行条件未满足。'});return plan;}
  async function validPlan(shotId){const ctx=context();await reloadCapabilities();guard(ctx);assertNotHeld('shot_id',shotId);const plan=state.plans[shotId];if(!plan||plan.status!=='ready')throw Error('请先完成这个镜头的预检，并解决阻塞项。');if(!state.capabilities?.execution_enabled)throw Error('当前服务未启用生成；未提交任务。');if(plan.sourceHash!==await digest(shotSnapshot(current().project,shotId)))throw Error('镜头或参考已改变，请重新预检。');if(plan.expires_at&&(typeof plan.expires_at==='number'?plan.expires_at*1000:Date.parse(plan.expires_at))<=Date.now())throw Error('计划已过期，请重新预检。');return plan;}
  async function mergeJobs(jobs,ctx){guard(ctx);const hashes={};for(const job of jobs){const shot=current().project.entities.find(e=>e.id===job.client_ref?.shot_id);if(shot&&!shot.data.storyboard)hashes[shot.id]=await digest(shotSnapshot(current().project,shot.id));}guard(ctx);store.editProject(project=>{let changed=false;for(const job of jobs)changed=mergeJobCandidate(project,job,{snapshotHash:hashes[job.client_ref?.shot_id]}).changed||changed;return changed;},{history:false});emit({jobs:jobs.filter(job=>job.client_ref?.project_id===ctx.projectId)});}
  async function collectJobArtifacts(jobs,ctx){for(const job of jobs)if(job.status==='succeeded'&&!job.artifacts?.length){const result=await api.artifacts(job.id);guard(ctx);job.artifacts=result.artifacts||[];}return jobs;}
  async function refreshJobs(){if(!state.account||current().workspace.mode!=='cloud'||polling)return;polling=true;const ctx=context();try{
    const [data,aggregate]=await Promise.all([api.jobs(ctx.projectId),api.activitySummary?api.activitySummary(ctx.projectId).then(value=>({value}),error=>({error})):null]);guard(ctx);
    const recent=data.jobs||[],recentIds=new Set(recent.map(job=>job.id)),retained=state.jobs.filter(job=>!recentIds.has(job.id));
    // Follow known active jobs outside the newest page without fetching all history.
    const missing=retained.filter(job=>activeJobStatuses.has(job.status)||['submission_unknown','planned','recovery_hold'].includes(job.status));
    if(api.job&&missing.length){const rotated=[...missing.slice(detailCursor%missing.length),...missing.slice(0,detailCursor%missing.length)].slice(0,10);detailCursor=(detailCursor+rotated.length)%missing.length;
      const updates=await Promise.allSettled(rotated.map(job=>api.job(job.id)));guard(ctx);for(const result of updates)if(result.status==='fulfilled'){const at=retained.findIndex(job=>job.id===result.value.id);if(at>=0)retained[at]=result.value;}}
    const jobs=await collectJobArtifacts([...recent,...retained],ctx);await mergeJobs(jobs,ctx);
    const activity=aggregate?.value||null;emit({activity,activityError:aggregate?.error?'项目任务总量暂未读取，当前列表不能代表全部任务。':'',moreJobs:activity?activity.total>jobs.length:recent.length>=100||state.moreJobs});
    const pending=readPending();for(const [id,value]of Object.entries(pending))if(jobs.some(job=>job.plan_id===value.planId))delete pending[id];writePending(pending);emit({unknown:pending});const renderPending=readRenderPending();for(const [id,value]of Object.entries(renderPending))if(jobs.some(job=>job.plan_id===value.planId))delete renderPending[id];writeRenderPending(renderPending);
    if(api.batches){const result=await api.batches(ctx.projectId,{limit:10,offset:0});guard(ctx);const page=result.batches||[],retained=state.batches.filter(batch=>!page.some(item=>item.id===batch.id));emit({batches:[...page,...retained],batchesLoaded:true,nextBatchOffset:state.batchesLoaded?state.nextBatchOffset:(result.next_offset??page.length),moreBatches:state.batchesLoaded&&state.nextBatchOffset>10?state.moreBatches:(result.has_more??page.length===10)});}
  }finally{polling=false;}}
  async function loadMoreJobs(){requireCloud();if(polling)throw Error('任务正在刷新，请稍后加载更早记录。');polling=true;try{const ctx=context(),offset=state.nextJobOffset,data=await api.jobs(ctx.projectId,{limit:100,offset});guard(ctx);const page=await collectJobArtifacts(data.jobs||[],ctx),merged=[...new Map([...state.jobs,...page].map(job=>[job.id,job])).values()];await mergeJobs(merged,ctx);emit({nextJobOffset:offset+page.length,moreJobs:page.length>0&&(state.activity?state.activity.total>merged.length:page.length===100),message:page.length?'已加载更早任务，按任务编号去重；有新任务进入时，历史分页可能重叠。':'未读到更早任务。任务总量和列表若不同，请刷新最新状态。'});}finally{polling=false;}}

  async function loadMoreBatches(){requireCloud();if(!api.batches)return;const ctx=context(),offset=state.nextBatchOffset,result=await api.batches(ctx.projectId,{limit:10,offset});guard(ctx);const page=result.batches||[],merged=[...new Map([...state.batches,...page].map(batch=>[batch.id,batch])).values()];emit({batches:merged,batchesLoaded:true,nextBatchOffset:result.next_offset??offset+page.length,moreBatches:result.has_more??page.length===10,message:page.length?'已加载更早批次，按编号去重。历史状态以最近读取为准，可刷新对应任务核对。':'这次没有读到更早批次。'});return result;}

  async function submit(shotId){requireCloud();assertNotHeld('shot_id',shotId);if(readBatchPending()?.shotIds.includes(shotId))throw Error('此镜头属于待核对批次，请先恢复原批次。');const ctx=context(),existing=readPending()[shotId],plan=existing?.plan||await validPlan(shotId);guard(ctx);
    if(!existing){const availability=await reloadAvailability({fresh:true});guard(ctx);requireModeAvailable(availability,state.capabilities,plan.availabilitySettings||{});}
    const key=existing?.key||intent('submit',ctx.projectId+':'+plan.plan_id);
    const pending={...readPending(),[shotId]:{planId:plan.plan_id,key,plan}};writePending(pending);emit({unknown:pending});
    try{const job=await api.submit(plan.plan_id,key);guard(ctx);const unresolved=readPending();delete unresolved[shotId];writePending(unresolved);emit({unknown:unresolved});await mergeJobs([job,...state.jobs.filter(j=>j.id!==job.id)],ctx);return job;}
    catch(error){guard(ctx);if(error.status>=400&&error.status<500){const unresolved=readPending();delete unresolved[shotId];writePending(unresolved);emit({unknown:unresolved});if(error.status===409&&error.detail?.detail==='capacity_drain_repreflight'){const plans={...state.plans};delete plans[shotId];emit({plans,preflightErrors:{...state.preflightErrors,[shotId]:error.message}});}throw error;}throw Error(error.message+' 提交结果未确认。先刷新任务，重试只会使用同一个提交标识。');}}
  async function submitBatch(shotIds,{recover=false}={}){requireCloud();for(const id of (recover?readBatchPending()?.shotIds||[]:shotIds))assertNotHeld('shot_id',id);const ctx=context();let pending=readBatchPending();if(pending&&!recover)throw Error('已有未确认的批次，请先恢复原批次。');if(!pending){const plans=[];for(const id of shotIds){if(readPending()[id])throw Error('有镜头正在核对提交，不能重复加入批次。');plans.push(await validPlan(id));}guard(ctx);if(!plans.length)throw Error('先选择需要生成的镜头。');const availability=await reloadAvailability({fresh:true});guard(ctx);for(const plan of plans)requireModeAvailable(availability,state.capabilities,plan.availabilitySettings||{});pending={shotIds,planIds:plans.map(p=>p.plan_id),key:intent('batch',await digest(plans.map(p=>p.plan_id)))};writeBatchPending(pending);}
    try{const batch=await api.batch(ctx.projectId,pending.planIds,pending.key);guard(ctx);writeBatchPending(null);emit({batches:[batch,...state.batches.filter(b=>b.id!==batch.id)]});await refreshJobs();emit({message:'批次已记录。可查看任务进度和逐镜审核；不会自动采用结果。'});return batch;}catch(error){guard(ctx);if(error.status>=400&&error.status<500)writeBatchPending(null);throw error;}}
  async function prepareRender(chapterId,options){
    requireCloud();assertNotHeld('chapter_id',chapterId);const ctx=context();
    if(readRenderPending()[chapterId])throw Error('本章有未确认的粗剪提交，先恢复原任务，不能创建新计划绕过。');
    if(roughCutJobs(state.jobs,ctx.projectId,chapterId).some(j=>activeJobStatuses.has(j.status)))throw Error('本章已有粗剪正在排队或执行，请查看原任务。');
    if(!state.capabilities?.chapter_render?.implemented)throw Error('当前服务尚未提供章节粗剪，未提交任务。');
    const before=roughCutSnapshot(current().project,chapterId,options);
    if(options.burn_subtitles){
      if(!(state.capabilities.chapter_render.subtitles===true&&state.capabilities.chapter_render.render_contract_version>=3))throw Error('当前服务不支持已确认字幕烧录。请刷新能力或升级服务；没有静默导出无字幕版本。');
      const subtitles=burnCaptionStatus(current().project,chapterId);if(subtitles.issues.length)throw Error(subtitles.issues.join(' '));
    }
    if(before.shots.some(shot=>shot.selectedVideoRange)&&!(state.capabilities.chapter_render.source_trim===true&&state.capabilities.chapter_render.render_contract_version>=2))throw Error('当前服务尚不支持已保存的剪辑选段。请升级服务，或明确恢复从视频开头取片；未静默忽略入点。');
    if(before.tracks.some(track=>track.generatedFrom)&&state.capabilities.chapter_render.generated_audio!==true)throw Error('当前服务尚不支持同次生成声音绑定，未忽略绑定按普通音轨导出。请升级服务，或明确静音／解除关联后再预检。');
    const invalidSound=before.tracks.find(track=>track.generatedIssue);if(invalidSound)throw Error(invalidSound.generatedIssue);
    if(!before.chapterId)throw Error('请先选择一个章节。');
    const ids=new Set([...before.shots.map(s=>s.source?.id),...before.tracks.map(t=>t.source?.id)].filter(Boolean));
    for(const id of ids){const entity=current().project.entities.find(e=>e.id===id);if(entity?.data.fileId&&!entity.data.missingFile&&!entity.data.cloudArtifactId){await syncEntity(id);guard(ctx);}}
    const saved=await save();guard(ctx);
    const sourceSnapshot=stableJSON(roughCutSnapshot(saved.project,chapterId,options));
    if(sourceSnapshot!==stableJSON(roughCutSnapshot(current().project,chapterId,options)))throw Error('保存期间时间线又有修改，请重新预检。');
    if(options.burn_subtitles){const subtitles=burnCaptionStatus(current().project,chapterId);if(subtitles.issues.length)throw Error('素材同步后字幕依据有变化，请重新核对字幕。'+subtitles.issues.join(' '));}
    const plan=await api.renderPlan({client_ref:{project_id:ctx.projectId,chapter_id:chapterId},resolution:options.resolution,aspect:options.aspect,...(options.burn_subtitles?{burn_subtitles:true}:{})});guard(ctx);
    if(!plan.plan_id||!['ready','blocked'].includes(plan.status)||plan.client_ref?.project_id!==ctx.projectId||plan.client_ref?.chapter_id!==chapterId)throw Error('粗剪计划响应无效，没有当作可执行计划。');
    if(plan.status==='ready'&&options.burn_subtitles&&plan.timeline?.subtitles?.burned!==true)throw Error('服务未确认字幕将烧入本次成片，未接受这个可执行计划。请刷新服务能力后重新预检。');
    emit({renderPlans:{...state.renderPlans,[chapterId]:{...plan,sourceSnapshot,options:clone(options)}},message:plan.status==='ready'?'粗剪预检通过，请核对时间线后确认。':'粗剪预检发现需要处理的素材或时间线问题。'});return plan;
  }
  async function submitRender(chapterId){
    requireCloud();assertNotHeld('chapter_id',chapterId);const ctx=context(),existing=readRenderPending()[chapterId],plan=existing?.plan||state.renderPlans[chapterId];
    if(!existing){
      if(!plan||plan.status!=='ready')throw Error('请先完成本章粗剪预检。');
      if(!state.capabilities?.chapter_render?.enabled)throw Error('服务尚未启用章节粗剪，未提交任务。');
      if(plan.sourceSnapshot!==stableJSON(roughCutSnapshot(current().project,chapterId,plan.options)))throw Error('章节时间线或素材已改变，请重新预检。');
      if(plan.expires_at&&(typeof plan.expires_at==='number'?plan.expires_at*1000:Date.parse(plan.expires_at))<=Date.now())throw Error('粗剪计划已过期，请重新预检。');
    }
    const key=existing?.key||intent('render-submit',ctx.projectId+':'+plan.plan_id);
    writeRenderPending({...readRenderPending(),[chapterId]:{planId:plan.plan_id,key,plan}});
    try{const job=await api.submit(plan.plan_id,key);guard(ctx);const pending=readRenderPending();delete pending[chapterId];writeRenderPending(pending);await mergeJobs([job,...state.jobs.filter(j=>j.id!==job.id)],ctx);emit({message:'粗剪任务已提交，可在本章粗剪版本查看进度；结果不替换镜头或时间线。'});return job;}
    catch(error){guard(ctx);if(error.status>=400&&error.status<500){const pending=readRenderPending();delete pending[chapterId];writeRenderPending(pending);throw error;}throw Error(error.message+' 粗剪提交结果未确认，请用原标识恢复；不会自动再排一次。');}
  }
  async function checkRemoteVersion(){
    if(!api.projectMeta||operation||!state.account||current().workspace.mode!=='cloud')return;
    const ctx=context();
    try{const meta=await api.projectMeta(ctx.projectId);guard(ctx);if(meta.id!==ctx.projectId||!Number.isInteger(meta.version)||meta.version<1)throw Error('Invalid project metadata');emit({remoteUpdate:meta.version>current().workspace.serverVersion?meta:null,remoteUpdateError:''});return meta;}
    catch(error){try{guard(ctx);}catch{return;}emit({remoteUpdateError:'暂时无法检查云端新版本，当前编辑仍保留。'});}
  }
  const storyboardPending=()=>storyboardCommandStore({storage,account:state.account,projectId:current().project.id});
  // Prepare ordinary owned inputs only. Aggregate preflight remains the sole
  // plan writer for this flow; inherited defaults are never written to a shot.
  async function prepareWorkbenchInputs(shotIds, ctx) {
    await reloadCapabilities(); guard(ctx);
    for (const shotId of shotIds) {
      assertNotHeld('shot_id', shotId);
      if (readPending()[shotId] || readBatchPending()?.shotIds.includes(shotId)
        || state.jobs.some(job => job.client_ref?.shot_id === shotId && activeJobStatuses.has(job.status))) throw Error('workbench.existingJob');
      if (current().workspace.dirty) { await save(); guard(ctx); }
      let preview = await api.generationDraft(ctx.projectId, shotId); guard(ctx);
      const recipe = state.capabilities?.recipes?.find(row => row.id === preview.draft?.recipe_id);
      const profile = state.capabilities?.deployment_profiles?.find(row => row.id === preview.draft?.deployment_profile_id);
      if (!recipe || (preview.draft.deployment_profile_id && !profile)) throw Error('workbench.capabilities');
      const mode = recipe.mode, support = profile ? profile.generation_support?.[mode] : recipe;
      const shot = current().project.entities.find(row => row.id === shotId), b = shot?.data.storyboard;
      if (!shot) throw Error('workbench.changed');
      if (shot.data.h3?.explicitReferences) {
        if (preview.project_version !== current().workspace.serverVersion || preview.issues?.length) throw Error('workbench.inputsInvalid');
        continue;
      }
      if (!b) throw Error('workbench.changed');
      if (b.continuePrevious && (mode === 'fl' || support?.input_support?.first_frame === true)) {
        const project = current().project, chapter = project.entities.find(row => row.type === 'chapter' && chapterShots(project, row.id).some(row => row.id === shotId));
        const shots = chapter ? chapterShots(project, chapter.id) : [], previous = shots[shots.findIndex(row => row.id === shotId) - 1];
        const chosen = project.entities.find(row => row.id === previous?.data.selectedAssetId);
        if (!chosen?.data.cloudArtifactId || chosen.type !== 'video') throw Error('workbench.previousMissing');
        if (b.previousFrame?.selectedAssetId !== chosen.id || !project.entities.some(row => row.id === b.previousFrame?.frameAssetId)) {
          const before = stableJSON(shot.data.storyboard);
          const frameId = await extractFrame(chosen.data.cloudArtifactId, {position:'last'}); guard(ctx);
          if (current().project.entities.find(row => row.id === previous.id)?.data.selectedAssetId !== chosen.id
            || stableJSON(current().project.entities.find(row => row.id === shotId)?.data.storyboard) !== before) throw Error('workbench.changed');
          if (!store.updateEntity(shotId, {data:{storyboard:{...b,previousFrame:{shotId:previous.id,selectedAssetId:chosen.id,frameAssetId:frameId}}}})) throw Error('workbench.changed');
        }
      }
      const project = current().project, chapter = project.entities.find(row => row.type === 'chapter' && chapterShots(project, row.id).some(row => row.id === shotId));
      const view = storyboardView(project, {chapterId:chapter?.id,capabilities:state.capabilities,jobs:state.jobs});
      const projected = view.shots.find(row => row.id === shotId);
      // Select uploads with the server's effective profile, without persisting it.
      const effective = {...projected,mode:mode.toUpperCase(),quality:projected.quality,entity:{...projected.entity,data:{...projected.entity.data,h3:{...projected.entity.data.h3,deploymentProfileId:profile?.id}}}};
      for (const id of new Set(expandStoryboard(effective,view,{forSync:true,effectiveProfile:profile,effectiveRecipe:recipe}).items.filter(item=>item.sent&&item.a?.id).map(item=>item.a.id))) {
        if (!current().project.entities.find(row=>row.id===id)?.data.cloudAssetId) { await syncEntity(id); guard(ctx); }
      }
      if (current().workspace.dirty) { await save(); guard(ctx); }
      preview = await api.generationDraft(ctx.projectId, shotId); guard(ctx);
      if (preview.project_version !== current().workspace.serverVersion || preview.issues?.length) throw Error('workbench.inputsInvalid');
    }
  }
  async function workbenchRead(suffix){requireCloud();const ctx=context();const value=await api.request(`/v1/projects/${encodeURIComponent(ctx.projectId)}/${suffix}`);guard(ctx);return value;}
  async function workbenchCommand(kind,body,{recover=false}={}){
    if(!recover)body=JSON.parse(JSON.stringify(body));
    requireCloud();const ctx=context(),journal=storyboardPending();let op=journal.read();
    if(!recover){if(op)throw Error('workbench.pending');if(kind==='preflight'){await prepareWorkbenchInputs(body.shot_ids,ctx);guard(ctx);body={...body,capabilities_version:state.capabilities?.capabilities_version};}if(['preferences','copies'].includes(kind)){op=journal.begin(kind,body);}else if(kind==='batch'){if(current().workspace.serverVersion!==body.projectVersion||current().workspace.dirty)throw Error('workbench.changed');op=journal.begin(kind,{client_project_id:ctx.projectId,plan_ids:[...body.plan_ids]});}else{if(body.expected_version!==undefined){if(current().workspace.dirty||body.expected_version!==current().workspace.serverVersion)throw Error('workbench.changed');}else if(current().workspace.dirty){await save();guard(ctx);}const frozen={...body,expected_version:body.expected_version??current().workspace.serverVersion};op=journal.begin(kind,frozen);}}
    const beforeProject=JSON.parse(JSON.stringify(current().project));
    if(!op||op.account!==ctx.account||op.projectId!==ctx.projectId)throw Error('workbench.pending');
    const route=op.kind==='actions'?`/v1/projects/${encodeURIComponent(ctx.projectId)}/actions`:op.kind==='preflight'?`/v1/projects/${encodeURIComponent(ctx.projectId)}/storyboard/preflights`:op.kind==='batch'?'/v1/batches':op.kind==='preferences'?'/v1/storyboard/preferences':op.kind==='copies'?'/v1/quick-chat/preferences':null;
    if(!route)throw Error('workbench.pendingCorrupt');
    let value;try{value=await api.request(route,{method:['preferences','copies'].includes(op.kind)?'PATCH':'POST',body:op.body,key:op.key});guard(ctx);}catch(error){guard(ctx);const proof=typeof error.detail?.detail==='object'?error.detail.detail:error.detail;if(proof?.no_mutation_committed===true&&((proof.code==='document_version_conflict'&&((['actions','preflight'].includes(op.kind)&&proof.project_id===ctx.projectId)||(op.kind==='preferences'&&proof.resource==='storyboard_preferences')))||(op.kind==='copies'&&proof.code==='version_conflict'&&proof.resource==='quick_chat_preferences'))&&proof.request_hash===await digest(op.body)){guard(ctx);journal.clear(op);throw Error('workbench.closed');}throw error;}
    if(op.kind==='preflight'){
      if(typeof value?.id!=='string'||!value.id||value.project_id!==ctx.projectId||value.project_version!==op.body.expected_version||value.request_hash!==await digest(op.body)||op.receiptId&&op.receiptId!==value.id)throw Error('workbench.receipt');guard(ctx);
      if(value.no_generation_submitted===true&&(value.closed_preflight===true||value.status==='pending')){journal.recordReceipt(op,value.id);return value;}
      if(value.status!=='completed'||value.items?.some(item=>!item.plan))throw Error('workbench.receipt');
    }
    if(!verifyStoryboardReceipt(op,value))throw Error('workbench.receipt');
    if(op.kind==='actions'){
      const alreadyObserved=!current().workspace.dirty&&current().workspace.serverVersion===value.version&&stableJSON(current().project)===stableJSON(value.project);
      if(!alreadyObserved&&(current().workspace.dirty||!store.applyCloudCommand(value.project,{account:ctx.account,expectedVersion:op.body.expected_version,version:value.version,expectedProject:beforeProject})))throw Error('workbench.changed');
    }
    journal.clear(op);if(op.kind==='batch'){try{await refreshJobs();}catch{/* The verified submission remains accepted even if its list refresh fails. */}}
    return value;
  }
  const unsubscribe=store.subscribe(installMedia);
  return {subscribe(fn){listeners.add(fn);return()=>listeners.delete(fn);},getState:()=>state,
    async initialize(){const observedEpoch=epoch;try{const auth=await api.authConfig();if(observedEpoch!==epoch)return;emit({auth});await recheckSession({loadLists:true});}catch(error){if(observedEpoch===epoch)emit({error:error.message});}},
    recheckSession:()=>recheckSession(),checkRemoteVersion,
    workbenchAbandonClosed:id=>action(async()=>{requireCloud();const ctx=context(),journal=storyboardPending(),op=journal.read();if(op?.kind!=='preflight')throw Error('workbench.pending');const value=await api.request(`/v1/projects/${encodeURIComponent(ctx.projectId)}/storyboard/preflights/${encodeURIComponent(id)}`);guard(ctx);if(op.receiptId!==id||value.id!==id||value.project_version!==op.body.expected_version||value.project_id!==ctx.projectId||value.closed_preflight!==true||value.no_generation_submitted!==true||value.request_hash!==await digest(op.body))throw Error('workbench.receipt');guard(ctx);journal.clear(op);return value;}),
    workbenchCopies:async()=>{requireCloud();const ctx=context();const value=await api.request('/v1/quick-chat/preferences');guard(ctx);return value;},
    workbenchPreferences:async()=>{requireCloud();const ctx=context();const value=await api.request('/v1/storyboard/preferences');guard(ctx);return value;},
    workbenchRead: suffix=>workbenchRead(suffix),
    workbenchCommand:(kind,body,options)=>action(()=>workbenchCommand(kind,body,options)),
    workbenchPending:()=>state.account&&current().workspace.mode==='cloud'?storyboardPending().read():null,
    async readProjectActivity(options={}){requireCloud();const ctx=context();const result=await api.projectActivity(ctx.projectId,options);guard(ctx);return result;},
    login:(username,password)=>action(async()=>{const observedEpoch=epoch,result=await api.login(username,password);if(observedEpoch!==epoch)throw Error('登录期间会话已经改变，请重新核对。');const me=identity(result);if(typeof me!=='string')throw Error('登录响应缺少账户身份。');replaceIdentity(me);announceSession();await reloadProjects();await reloadCapabilities();offerResume();}),
    changePassword:(previous,next)=>action(async()=>{const ctx=accountContext();await api.changePassword(previous,next);guardAccount(ctx);rememberVisit({mode:'local'});replaceIdentity(null,'密码已修改，请重新登录；原 API Key 已撤销，云草稿仍按账户保留。');announceSession();}),
    logout:()=>action(async()=>{await api.logout();rememberVisit({mode:'local'});replaceIdentity(null,'已退出。云草稿仍按原账户保留，本机作品已恢复。');announceSession();}),
    leave(){epoch++;lastAvailabilityCheck=0;api.reset?.();store.leaveCloudProject();rememberVisit({mode:'local'});emit({availability:null,availabilityChecking:false,assets:[],usage:null,assetError:'',plans:{},preflightErrors:{},batchChecking:null,renderPlans:{},renderOptions:{},renderPending:{},jobs:[],activity:null,activityError:'',nextJobOffset:100,moreJobs:false,batches:[],batchesLoaded:false,nextBatchOffset:10,moreBatches:false,batchPending:null,conflict:null,draftOffer:null,resumeOffer:null,resumeError:'',remoteUpdate:null,remoteUpdateError:''});installMedia();},
    resume:()=>action(async()=>{const ctx=accountContext(),offer=state.resumeOffer;if(!offer||current().workspace.mode!=='local')throw Error('请重新核对当前账户与上次作品。');try{const done=await open(offer.projectId);emit({resumeError:''});return done;}catch(error){guardAccount(ctx);if([403,404].includes(error.status)){rememberVisit({mode:'local'});emit({resumeOffer:null,resumeError:'上次云作品已不存在，或当前账户无权读取。当前本机作品和已有草稿均未改变。'});}else emit({resumeError:'上次云作品暂时无法读取。可稍后重试或从云项目列表打开；当前本机作品未改变。'});throw error;}}),
    dismissResume(){rememberVisit({mode:'local'});emit({resumeOffer:null,resumeError:'',draftOffer:null});},
    list:options=>action(()=>reloadProjects(options)),loadMoreProjects:()=>action(loadMoreProjects),reloadCapabilities:()=>action(reloadCapabilities),reloadAvailability:()=>reloadAvailability({fresh:true}),open:(id,options)=>action(()=>open(id,options)),save:()=>action(save),
    create:(title,{copy=false}={})=>action(async()=>{if(!state.account)throw Error('先登录云工作室。');const ctx=accountContext(),project=copy?copyForCloud(store.exportProject()):blankCloudProject(title);if(copy&&title)project.title=title;const record=checkedProjectRecord(await api.createProject(project,intent('create',project.id)));guardAccount(ctx);await reloadProjects();guardAccount(ctx);return open(record.id);}),
    createQuick:title=>action(async()=>{if(!state.account)throw Error('先登录云工作室。');const ctx=accountContext(),project=freestyleProject(title||'快速创作 · '+new Date().toLocaleDateString());const record=checkedProjectRecord(await api.createProject(project,intent('create',project.id)));guardAccount(ctx);await reloadProjects();guardAccount(ctx);return open(record.id);}),
    saveCopy:()=>action(async()=>{requireCloud();const ctx=accountContext(),project=copyForCloud(store.exportProject());project.title=project.title.slice(0,145)+' · 冲突草稿副本';const record=checkedProjectRecord(await api.createProject(project,intent('create',project.id)));guardAccount(ctx);await reloadProjects();guardAccount(ctx);return open(record.id);}),
    assetInventory:()=>action(assetInventory),
    resumeAsset:id=>action(async()=>{requireCloud();const ctx=context();try{const asset=await api.resumeAsset(id);guard(ctx);if(asset.status==='ready')attachRecoveredAsset(asset);await assetInventory();return asset;}catch(error){guard(ctx);try{await assetInventory();}catch{}throw error;}}),
    attachAsset:id=>action(async()=>{requireCloud();const ctx=context(),asset=await api.asset(id);guard(ctx);attachRecoveredAsset(asset);}),
    upload,syncAll:()=>action(async()=>{requireCloud();for(const asset of [...current().project.entities])if(['image','video','audio'].includes(asset.type)&&asset.data.fileId&&!asset.data.cloudArtifactId)await syncEntity(asset.id);await save();emit({message:'可读取的本机素材已同步，项目版本已保存。'});}),
    prepare:shotId=>action(()=>prepare(shotId)),prepareStoryboard:(shotId,options)=>action(()=>prepareStoryboard(shotId,options)),saveFrame:(artifactId,selection)=>action(()=>extractFrame(artifactId,selection)),jobReceipt:id=>action(async()=>{requireCloud();const ctx=context(),job=await api.job(id);guard(ctx);if(job.id!==id||job.client_ref?.project_id!==ctx.projectId)throw Error('任务不属于当前项目。');return job;}),submit:shotId=>action(()=>submit(shotId)),refresh:()=>action(refreshJobs),loadMoreJobs:()=>action(loadMoreJobs),loadMoreBatches:()=>action(loadMoreBatches),
    setRenderOptions(chapterId,options){requireCloud();emit({renderOptions:{...state.renderOptions,[chapterId]:clone(options)}});},
    prepareRender:(chapterId,options)=>action(()=>prepareRender(chapterId,options)),submitRender:chapterId=>action(()=>submitRender(chapterId)),
    prepareBatch:shotIds=>action(async()=>{requireCloud();const ctx=context(),errors=[],ids=[...new Set(shotIds)];try{for(const [index,id] of ids.entries()){guard(ctx);emit({batchChecking:{projectId:ctx.projectId,shotId:id,completed:index,total:ids.length}});try{await prepare(id);}catch(error){guard(ctx);errors.push({id,message:error.message});emit({preflightErrors:{...state.preflightErrors,[id]:error.message}});}}guard(ctx);emit({message:`已检查 ${ids.length} 个镜头；请在完整清单核对通过、阻塞和未完成项，再明确提交。`,error:''});return errors;}finally{if(ctx.epoch===epoch&&ctx.projectId===current().project.id&&ctx.account===state.account)emit({batchChecking:null});}}),
    submitBatch:shotIds=>action(()=>submitBatch(shotIds)),recoverBatch:()=>action(()=>submitBatch([],{recover:true})),
    cancel:id=>action(async()=>{requireCloud();const ctx=context();await api.cancel(id);guard(ctx);await refreshJobs();guard(ctx);emit({message:'取消请求已提交。以任务最终状态为准，已执行部分可能已产生费用。'});}),
    cancelBatch:id=>action(async()=>{requireCloud();const ctx=context(),batch=await api.cancelBatch(id);guard(ctx);emit({batches:state.batches.map(b=>b.id===id?batch:b)});await refreshJobs();}),
    clearError(){emit({error:'',message:''});},dismissDraft(){emit({draftOffer:null});},
    startPolling(){stopped=false;let timer;const tick=async()=>{if(stopped)return;try{await recheckSession();await refreshJobs();if(Date.now()-lastAvailabilityCheck>=10000)await reloadAvailability();if(Date.now()-lastRemoteCheck>15000){lastRemoteCheck=Date.now();await checkRemoteVersion();}}catch(error){if(!stopped)emit({error:error.message});}if(!stopped)timer=setTimeout(tick,5000);};timer=setTimeout(tick,5000);return()=>{stopped=true;clearTimeout(timer);};},
    destroy(){stopped=true;epoch++;api.reset?.();unsubscribe();globalThis.window?.removeEventListener('storage',sessionChanged);globalThis.window?.removeEventListener('focus',focusSession);globalThis.document?.removeEventListener('visibilitychange',visibleSession);mediaHandlers({});listeners.clear();},
  };
}
export const cloudController=createCloudController();
