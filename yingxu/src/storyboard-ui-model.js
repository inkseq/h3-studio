export const TYPE_LABEL={character:'人物',location:'场景',prop:'道具'};
export const VARIANT_LABEL={character:'造型',location:'版本',prop:'图片'};
export const KIND_LABEL={image:'图片',video:'动作视频',audio:'声音'};
export const MODE_LABEL={FL:'首尾帧',REF:'全能参考'};
export const QUALITY_LABEL={draft:'草稿',final:'精修'};
export const variantOf=(profile,id)=>profile&&(profile.variants.find(v=>v.id===id)||profile.variants.find(v=>v.id===profile.def)||profile.variants[0]);
export const coverOf=(db,profile,id)=>profile&&db.asset(variantOf(profile,id)?.images[0]||profile.variants.flatMap(v=>v.images)[0]);
export const chosenVersion=shot=>shot.versions.find(version=>version.id===shot.chosen)||null;
export const modeOf=shot=>shot.mode||'FL';
export const prevShot=(shots,shot)=>{const at=shots.findIndex(s=>s.id===shot.id);return at>0?shots[at-1]:null;};
export const expand=(shot,db)=>db.expand(shot);
export const planDrop=(shot,item,slot,db)=>db.planDrop(shot,item,slot);
export const shotProfiles=shot=>[...shot.cast.map(c=>({id:c.id,variant:c.variant})),...(shot.place?[shot.place]:[]),...shot.props.map(id=>({id}))];
export function startFrame(shot,db){
  if(shot.continuePrev){const previous=prevShot(db.state.shots,shot),version=previous&&chosenVersion(previous),frame=shot.previousFrame&&db.asset(shot.previousFrame),binding=shot.entity?.data.storyboard?.previousFrame,selection=frame?.entity?.data.sourceSelection;const current=frame&&version&&binding?.shotId===previous.id&&binding.selectedAssetId===previous.chosen&&selection?.kind==='artifact_frame'&&selection.position==='last'&&selection.artifact_id===version.artifactId;return {asset:current?frame:null,label:version?'上一镜选定版本的最后一帧':'等待上一镜选定版本',pending:!current};}
  const asset=shot.start&&db.asset(shot.start);return asset?{asset,label:asset.name}:null;
}
export const fmt=sec=>Number.isFinite(sec)?`${Math.floor(Math.max(0,sec)/60)}:${String(Math.floor(Math.max(0,sec)%60)).padStart(2,'0')}`:'—';
export function queuePositions(shots){return Object.fromEntries(shots.map(shot=>[shot.id,shot.queuePosition??shot.versions.findLast(v=>v.status==='queued')?.queuePosition??null]));}
export function shotStatus(shot,position){
  const latest=shot.versions.at(-1);
  if(latest&&['planned','waiting_capacity','queued','claimed','submitting','submission_unknown'].includes(latest.status))return {id:'queued',label:position?`排队第 ${position}`:latest.status==='submission_unknown'?'提交待核对':'等待执行',tone:'wait'};
  if(latest&&['generating','running','collecting','checking'].includes(latest.status))return {id:'gen',label:latest.status==='collecting'?'保存与核验结果':Number.isFinite(latest.progress)?`生成中 ${Math.round(latest.progress*100)}%`:'生成中',tone:'busy'};
  if(latest?.status==='failed')return {id:'failed',label:'生成失败 · 查看原因',tone:'warn'};
  if(!shot.versions.length)return {id:'empty',label:'待生成',tone:'muted'};
  if(latest?.id!==shot.chosen&&['done','succeeded'].includes(latest?.status))return {id:'pick',label:'新版本待挑选',tone:'warn'};
  const chosen=chosenVersion(shot);return chosen?{id:'ok',label:`已选定 · ${QUALITY_LABEL[chosen.quality]||'视频'}`,tone:'good'}:{id:'empty',label:'尚未选定版本',tone:'muted'};
}
export function segments(shots){let t=0;return shots.map(shot=>{const version=chosenVersion(shot),dur=Number.isFinite(version?.duration)&&version.duration>0?version.duration:Number.isFinite(shot.duration)&&shot.duration>0?shot.duration:0;const seg={shot,start:t,dur,version};t+=dur;return seg;});}
export const totalDuration=shots=>segments(shots).reduce((sum,s)=>sum+s.dur,0);
