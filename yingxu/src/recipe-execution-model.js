import {controlsForRecipe,referenceSpecs} from './cloud-model.js';

const names={sampler_name:'采样器',scheduler:'调度器',video_decode:'视频 VAE 解码',audio_decode:'音频 VAE 解码',encoder_device:'编码器设备',ref_image_size:'参考图尺寸'};
const display=value=>value==='cpu'?'CPU':value==='tiled'?'分块解码':String(value);
const mediaKey=(entity,range)=>`${entity.id}:${range?.start??''}:${range?.end??''}`;

function describeInputLimits(limits,{recipe,project,shot,references,guides,issues,summary}){
  if(!limits)return;
  const labels={image:'图片参考',video:'视频参考',audio:'独立音频参考'},kinds={image:new Map(),video:new Map(),audio:new Map()},all=new Map();
  for(const ref of references){const entry={entity:ref.entity,range:ref.range},key=mediaKey(ref.entity,ref.range);all.set(key,entry);if(!['firstFrame','lastFrame'].includes(ref.purpose))kinds[ref.entity.type]?.set(key,entry);}
  for(const guide of guides){const entity=project?.entities.find(e=>e.id===guide.media_id);if(!entity)continue;const key=mediaKey(entity,guide.source_range),entry={entity,range:guide.source_range};all.set(key,entry);kinds[entity.type]?.set(key,entry);}
  for(const [kind,entries]of Object.entries(kinds)){
    const maxCount=limits[{image:'max_images',video:'max_videos',audio:'max_audios'}[kind]];
    if(Number.isInteger(maxCount)){summary.push(`${labels[kind]}最多 ${maxCount} 份（含同类锚点；首尾帧另计）`);if(entries.size>maxCount)issues.push(`当前有 ${entries.size} 份${labels[kind]}，超过云端 ${maxCount} 份上限。`);}
    const maxDuration=limits[`max_${kind}_duration_seconds`];
    if(Number.isFinite(maxDuration)){
      summary.push(`${labels[kind]}总时长最多 ${Number(maxDuration.toFixed(3))} 秒，原选段与模型副本均需满足`);
      let total=0;for(const {entity,range}of entries.values()){const meta=entity.data.metadata||{},duration=range?range.end-range.start:Math.max(meta.duration||0,meta.source_duration||0);if(Number.isFinite(duration))total+=duration;}
      if(total>maxDuration+1e-6)issues.push(`${labels[kind]}的已知总时长超过云端 ${Number(maxDuration.toFixed(3))} 秒上限；请明确选择更短片段后预检。`);
    }
  }
  for(const kind of ['image','video']){const maximum=limits[`max_${kind}_pixels`];if(Number.isFinite(maximum))summary.push(`${kind==='image'?'所有图片（含首尾帧、锚点）':'视频参考'}每份最多 ${maximum.toLocaleString('zh-CN')} 像素`);}
  for(const {entity}of all.values()){const meta=entity.data.metadata||{},maximum=limits[`max_${entity.type}_pixels`];if(Number.isFinite(maximum)&&Number(meta.width)*Number(meta.height)>maximum)issues.push(`「${entity.title}」的已知尺寸 ${meta.width}×${meta.height} 超过云端输入像素上限；原素材会保留。`);}
  if(Array.isArray(limits.guide_recipe_ids)&&!limits.guide_recipe_ids.includes(recipe.id)){summary.push('此模式暂不接受时间锚点');if(guides.length)issues.push('当前模式尚未开放时间锚点，可保留锚点或明确移除后重新预检。');}
  if(Array.isArray(limits.guide_kinds)){summary.push(`时间锚点类型：${limits.guide_kinds.map(kind=>labels[kind]||kind).join(' / ')||'暂未开放'}`);if(guides.some(g=>!limits.guide_kinds.includes(project?.entities.find(e=>e.id===g.media_id)?.type)))issues.push('当前时间锚点的素材类型超出云端范围。');}
  if(Number.isFinite(limits.max_guide_time_seconds)){summary.push(`时间锚点位置不晚于 ${limits.max_guide_time_seconds} 秒`);if(guides.some(g=>g.time_seconds>limits.max_guide_time_seconds))issues.push(`时间锚点须位于 ${limits.max_guide_time_seconds} 秒以内。`);}
  if(limits.allow_video_audio===false){summary.push('暂不接受参考视频原声');const hasReference=references.some(ref=>ref.entity.type==='video'&&ref.entity.data.metadata?.has_audio&&shot?.data.h3?.video_audio?.[ref.entity.id]!==false),hasGuide=guides.some(g=>g.use_audio&&project?.entities.find(e=>e.id===g.media_id)?.type==='video');if(hasReference||hasGuide)issues.push('当前云端尚未开放参考视频原声；请明确关闭原声或保留设置等待开放。');}
  summary.push('素材尺寸、选段及归一化后的时长以服务端预检为准；本提示不修改素材。');
}

// Describes the server's qualified envelope, never grants admission or changes
// user inputs. The authenticated preflight remains authoritative for execution.
export function recipeExecutionStatus(recipe,{project,shot,now=Date.now()}={}){
  const support=recipe?.execution_support;
  if(!support)return {kind:'unknown',title:'当前云端执行范围尚未读取',detail:'模型能力与当前服务器开放范围不同，请先读取服务能力并预检。',issues:[],summary:[]};
  if(Number.isFinite(support.expires_at)&&support.expires_at*1000<=now)return {kind:'unavailable',title:'当前云端执行范围已过期',detail:'请刷新服务能力后重新预检；作品和素材可以继续编辑。',issues:[],summary:[]};
  if(!['qualified','runtime_required'].includes(support.status))return {kind:support.status,title:support.status==='not_qualified'?'此模式目前仅可编辑，尚不能生成':support.status==='simulation'?'当前为模拟执行':'当前模式暂不能执行',detail:support.reason||'作品和素材可以继续保存；实际开放范围以服务预检为准。',issues:[],summary:[]};
  const limits=support.constraints;
  if(!limits)return {kind:'unknown',title:'此模式的执行限制尚未读取',detail:'请重新读取服务能力并预检；不能据此认为所有输入和参数已开放。',issues:[],summary:[]};
  const values=controlsForRecipe(recipe,shot?.data.h3?.controls,shot?.data.seconds),issues=[],summary=[];
  if(Number.isFinite(limits.max_duration_seconds)){summary.push(`采样时长最多 ${limits.max_duration_seconds} 秒`);if(Number(values.duration)>limits.max_duration_seconds)issues.push(`当前生成时长 ${values.duration} 秒，超过云端 ${limits.max_duration_seconds} 秒上限。`);}
  if(Number.isFinite(limits.max_steps)){summary.push(`最多 ${limits.max_steps} 步`);if(Number(values.steps)>limits.max_steps)issues.push(`当前采样 ${values.steps} 步，云端最多接受 ${limits.max_steps} 步。`);}
  if(Number.isFinite(limits.max_pixels)){summary.push(`最多 ${limits.max_pixels.toLocaleString('zh-CN')} 像素`);if(values.resolution==='custom'&&Number(values.width)*Number(values.height)>limits.max_pixels)issues.push(`自定义画面 ${values.width}×${values.height} 超过云端像素上限。`);}
  const references=project&&shot?referenceSpecs(project,shot.id,{mode:recipe.mode}).references:[],guides=shot?.data.h3?.guides||[];
  const mediaKeys=new Set(references.map(ref=>mediaKey(ref.entity,ref.range)));
  for(const guide of guides)mediaKeys.add(`${guide.media_id}:${guide.source_range?.start??''}:${guide.source_range?.end??''}`);
  if(Number.isFinite(limits.max_reference_files)){summary.push(limits.max_reference_files===0?'暂不接受参考文件':`参考文件最多 ${limits.max_reference_files} 份`);if(mediaKeys.size>limits.max_reference_files)issues.push(`当前已关联参考素材，超出云端最多 ${limits.max_reference_files} 份的范围；可保留素材，明确调整关联后再预检。`);}
  if(Number.isFinite(limits.max_guides)){summary.push(limits.max_guides===0?'暂不接受时间锚点':`最多 ${limits.max_guides} 个时间锚点`);if(guides.length>limits.max_guides)issues.push(`当前有 ${guides.length} 个时间锚点，云端最多接受 ${limits.max_guides} 个。`);}
  if(limits.allow_first_last===false){summary.push('暂不接受首尾帧');if(references.some(ref=>['firstFrame','lastFrame'].includes(ref.purpose)))issues.push('当前云端尚未开放首尾帧输入；可以继续保存图片，或明确移除关联后使用纯文字生成。');}
  if(limits.allow_audio===false){summary.push('暂不生成声音');if(values.generate_audio)issues.push('当前云端尚未开放声音生成；请保留设置或明确关闭生成声音。');}
  describeInputLimits(limits.input_limits,{recipe,project,shot,references,guides,issues,summary});
  for(const [field,options]of Object.entries(limits.controls||{}))if(names[field]&&Array.isArray(options)&&options.length){summary.push(`${names[field]}：${options.map(display).join(' / ')}`);if(Object.hasOwn(values,field)&&!options.includes(values[field]))issues.push(`${names[field]}当前为 ${display(values[field])}；云端仅接受 ${options.map(display).join(' / ')}。`);}
  const runtime=support.status==='runtime_required';
  return {kind:issues.length?'outside':runtime?'runtime_required':'qualified',title:issues.length?'当前设置超出云端执行范围':runtime?'可提交，GPU启动后先验证当前模式':'此模式已开放有限的生成范围',detail:runtime?'这是待新 GPU 验证的执行范围；验证通过后才执行原任务，仍需预检账户额度与容量窗口。现有设置不会自动更改。':'范围内仍需预检账户额度与实际计算容量；可以继续编辑和保存，现有设置不会自动更改。',issues,summary};
}
