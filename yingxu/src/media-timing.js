export function validateRange(start,end,duration){
  if(start===''||end===''||!Number.isFinite(Number(start))||!Number.isFinite(Number(end)))return '请输入开始和结束秒数。';
  if(!Number.isFinite(duration)||duration<=0)return '等待文件时长读取完成；若一直无法读取，请重新上传。';
  if(Number(start)<0)return '开始时间不能小于 0 秒。';
  if(Number(end)<=Number(start))return '结束时间必须大于开始时间。';
  if(Number(end)>duration+0.001)return `结束时间不能超过文件总长 ${duration.toFixed(2)} 秒。`;
  return '';
}
// Match CPU rendering: round each shot once, then accumulate integer 24fps frames.
export function chapterTiming(shots){let cursor=0;return shots.map(shot=>{const start=cursor/24;cursor+=Math.max(0,Math.floor((Number(shot.data.seconds)||0)*24+.5));return {id:shot.id,title:shot.title,start,end:cursor/24,shot};});}
export function trackPlacement(track,timeline){
  const origin=track.shotId?timeline.find(s=>s.id===track.shotId):null;
  if(track.shotId&&!origin)return null;
  const offset=Number(track.offset??0),sourceStart=Number(track.start),sourceEnd=Number(track.end),start=(origin?.start||0)+offset,length=sourceEnd-sourceStart;
  if(!Number.isFinite(offset)||offset<0||!Number.isFinite(sourceStart)||sourceStart<0||!Number.isFinite(start)||start<0||!Number.isFinite(length)||length<=0)return null;
  return {start,end:start+length};
}
export function trackAtTime(track,timeline,time){const placement=trackPlacement(track,timeline);if(!placement||track.muted||time<placement.start||time>=placement.end)return null;return Number(track.start)+time-placement.start;}
export const secondsLabel=value=>`${Math.floor(Math.max(0,value)/60).toString().padStart(2,'0')}:${(Math.max(0,value)%60).toFixed(1).padStart(4,'0')}`;
