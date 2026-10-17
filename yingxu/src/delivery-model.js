import {effectiveLook} from './character-model.js';
import {chapterTiming,trackPlacement,validateRange} from './media-timing.js';
import {children,ancestors} from './store.js';
export const chapterShots=(p,id)=>children(p,id).filter(e=>e.type==='scene').flatMap(e=>children(p,e.id).filter(s=>s.type==='shot'));
export const entityPath=(p,id)=>[...ancestors(p,id),p.entities.find(e=>e.id===id)].filter(Boolean).map(e=>e.title).join(' / ');
export function deliveryIssues(p,chapterId){
 const list=[],add=(id,stage,entityId,title,detail,field='')=>list.push({id,stage,entityId,title,detail,field,path:entityId?entityPath(p,entityId):'全剧设定'});
 if(!p.logline.trim())add('story',1,null,'补充故事方向','写清主角、目标与阻碍。','logline');
 const chapter=p.entities.find(e=>e.id===chapterId),scenes=chapter?children(p,chapterId).filter(e=>e.type==='scene'):[],shots=chapterShots(p,chapterId);
 if(!chapter)add('chapter',2,null,'添加第一章','先给这一段故事一个位置。');
 else if(!scenes.length)add('scene-'+chapter.id,2,chapter.id,'添加场戏','这一章还没有场戏。');
 for(const scene of scenes){if(!scene.data.script?.trim())add('script-'+scene.id,2,scene.id,'补充动作与台词','这场发生什么，需要先写清。','script');if(!children(p,scene.id).some(e=>e.type==='shot'))add('shots-'+scene.id,2,scene.id,'为这场添加镜头','目前没有镜头覆盖这段故事。');}
 for(const shot of shots){
  if(!shot.description.trim())add('description-'+shot.id,4,shot.id,'补充镜头画面','描述主体、动作与情绪。','description');
  const asset=p.entities.find(e=>e.id===shot.data.selectedAssetId),review=shot.data.uxReview||{};
  const usable=shot.data.selectedAssetId?asset?.data.fileId&&!asset.data.missingFile:!!review.chosen;
  if(!usable){add('candidate-'+shot.id,5,shot.id,'选择镜头候选',asset?'选定素材文件缺失，请补传或换一个版本。':'还没有选择图片、视频或示例分镜。');list.at(-1).placeholderAllowed=true;list.at(-1).accepted=shot.data.acceptPlaceholder===true;}
  if(shot.status==='review')add('review-'+shot.id,5,shot.id,'复核上游改动','原候选保留；确认内容和参考变化后是否仍可用。');
  if(['revision','failed','retry','processing','cancelled'].includes(review.status))add('revision-'+shot.id,5,shot.id,'完成本轮审核','还有返修或演示状态未解决，选择合适版本后再确认。');
  for(const link of p.links.filter(l=>l.target===shot.id)){
   const ref=p.entities.find(e=>e.id===link.source);
   if(ref&&['image','video','audio'].includes(ref.type)&&(!ref.data.fileId||ref.data.missingFile))add('missing-'+link.id,4,shot.id,'恢复缺失参考',`「${ref.title}」的本机文件不可用。`,'referenceRanges');
   const range=shot.data.referenceRanges?.[link.id];if(ref?.data.fileId&&range&&(range.fileId!==ref.data.fileId||validateRange(range.start,range.end,range.duration)))add('range-'+link.id,4,shot.id,'重新确认参考选段',`「${ref.title}」的原文件或有效范围有变化，请重新试听并保存选段。`,'referenceRanges');
  }
  const characters=[...new Set([...(p.entities.find(e=>e.id===shot.parentId)?.data.cast||[]),...(shot.data.cast||[])].map(c=>c.characterId))],cast=characters.map(id=>effectiveLook(p,shot,id)).filter(Boolean);
  for(const binding of cast||[]){const char=p.entities.find(e=>e.id===binding.characterId),look=char?.data.looks?.find(l=>l.id===binding.lookId);if(!char||!look)add('cast-'+shot.id+'-'+binding.characterId,3,shot.id,'修复人物造型绑定','绑定的人物或造型已不存在，请重新选择。');else for(const [slot,assetId]of Object.entries(look.gallery||{})){if(!assetId)continue;const image=p.entities.find(e=>e.id===assetId),key='gallery-'+char.id+'-'+look.id+'-'+slot;if((!image?.data.fileId||image.data.missingFile)&&!list.some(i=>i.id===key)){add(key,3,char.id,'恢复造型参考图',`「${char.title} / ${look.name}」的${{front:'正面',side:'侧面',full:'全身'}[slot]||slot}图缺失；补传或明确改为稍后补充。`);list.at(-1).lookId=look.id;}}}
 }
 const mode=p.journey?.sound?.mode,tracks=p.journey?.soundTracks?.[chapterId]||[];
 if(!mode)add('sound',6,null,'决定声音方案','可以选择静音先行。');
 else if(mode!=='silent'&&!tracks.length)add('sound-media',6,null,'添加可试听的声音','当前只记录了声音方向，还没有绑定本地音频；也可改为静音。');
 const timeline=chapterTiming(shots),total=timeline.at(-1)?.end||0;
 for(const track of tracks){if(mode==='silent'||track.muted)continue;const a=p.entities.find(e=>e.id===(track.assetId||track.audioId)),placement=trackPlacement(track,timeline),origin=timeline.find(s=>s.id===track.shotId);if(!a?.data.fileId||a.data.missingFile)add('sound-missing-'+track.id,6,null,'恢复声音素材','声音轨道缺少可读取的音频。');else if(track.fileId!==a.data.fileId||validateRange(track.start,track.end,track.duration))add('sound-range-'+track.id,6,null,'重新确认声音选段','原音频或范围有变化，请编辑这条音轨并重新试听保存。');if(!placement||placement.start>=total||(origin&&track.offset>=origin.end-origin.start))add('sound-origin-'+track.id,6,null,'调整声音起点','原起点镜头已删除或缩短，请重新选择起点或先静音此轨。');}
 return list;
}
