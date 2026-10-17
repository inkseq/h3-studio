// Read-only browser acceptance probe. Call through the authorized Chrome tab.
// Report deliberate content tiles/ellipsis separately; never mask failures.
export function auditVisibleUI({locale='en'}={}){
 const root=document.querySelector('dialog[open]')||document.body,issues=[],exceptions=[];
 const label=el=>(el.getAttribute('aria-label')||el.getAttribute('title')||el.textContent||'').trim().replace(/\s+/g,' ').slice(0,100);
 const visible=el=>{const r=el.getBoundingClientRect(),s=getComputedStyle(el);return r.width>0&&r.height>0&&r.bottom>0&&r.right>0&&r.top<innerHeight&&r.left<innerWidth&&s.visibility!=='hidden'&&s.display!=='none'&&!el.closest('[hidden],[inert],[aria-hidden="true"]');};
 const elements=[...root.querySelectorAll('*')].filter(visible);
 const dataSelector='.quick-recipe-prompt,.bubble,.bot-body,.story-recent-title,.story-card h3,.story-card p,.sc-title,.sc .title,.profile-name,.asset-name,.library-media-name,.bt-label,.take-top,.qc-link,.qc-rename,.libp-tile figcaption,.storyboard-takes .take-top,.storyboard-takes .takes-compare b,.assistant-title';
 for(const el of elements){
  const own=[...el.childNodes].filter(n=>n.nodeType===Node.TEXT_NODE).map(n=>n.textContent).join('').trim(),s=getComputedStyle(el),rect=el.getBoundingClientRect();
  if(own&&parseFloat(s.fontSize)<10.95)issues.push({type:'small-text',label:label(el),px:parseFloat(s.fontSize)});
  if(own&&/\$\s*\d+\.\d{3,}/.test(own))issues.push({type:'money-precision',label:label(el)});
  if(locale==='en'&&/[\u3400-\u9fff]/.test(own)&&!el.closest(dataSelector)&&!el.closest('.language-switch,.account-avatar,.avatar,input,textarea,option,[data-user-content]'))issues.push({type:'mixed-language',label:label(el)});
  if(own&&el.clientWidth>0&&el.scrollWidth>el.clientWidth+2&&['hidden','clip'].includes(s.overflowX)){
   if(s.textOverflow==='ellipsis'&&(el.title||el.getAttribute('aria-label')||el.closest(dataSelector)))exceptions.push({type:'intentional-ellipsis',label:label(el)});
   else if(Number(s.webkitLineClamp)>0)exceptions.push({type:'expandable-clamp',label:label(el)});
   else issues.push({type:'clipped-text',label:label(el),width:Math.round(rect.width)});
  }
 }
 for(const el of elements.filter(el=>el.tagName==='BUTTON'||el.getAttribute('role')==='button')){
  const text=(el.textContent||'').trim(),r=el.getBoundingClientRect();
  if(/^[○✓⋯×‹›＋＋✕✧★☆▸◀▶⏪]+$/.test(text)||/^[○✓⋯×‹›＋✧]\s+\S/.test(text))issues.push({type:'glyph-icon',label:label(el)});
  const tile=el.matches('.bt-preview,.sc,.sc-open,.tl-seg,.tl-shot,.story-card,.libp-tile,.qc-link,.slot,.ver,.asset-card,.profile-card,.tl-thumb,.timeline-clip,.library-tile,.qc-scrim,.sb-library-backdrop')||el.getAttribute('aria-roledescription')==='sortable';
  if(tile){exceptions.push({type:'content-tile-or-scrim',label:label(el)});continue;}
  if(![28,32,36].some(height=>Math.abs(r.height-height)<=1))issues.push({type:'control-height',label:label(el),height:Math.round(r.height*10)/10,className:el.className});
 }
 if(document.documentElement.scrollWidth>innerWidth+1)issues.push({type:'document-overflow',width:document.documentElement.scrollWidth,viewport:innerWidth});
 return {viewport:{width:innerWidth,height:innerHeight},locale,visibleElements:elements.length,issues,exceptions};
}
