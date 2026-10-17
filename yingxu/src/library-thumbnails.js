// Display-only native media queue. No API writes, retries or persistent state.
const queues=new WeakMap();
export function createThumbnailQueue(){
 let active=null;const waiting=[];
 function drain(){if(active)return;while(waiting.length){const item=waiting.shift();if(item.cancelled)continue;active=item;item.start(item.release);return;}}
 return {enqueue(start){const item={start,cancelled:false,release:null};item.release=()=>{if(active!==item)return;active=null;queueMicrotask(drain);};waiting.push(item);queueMicrotask(drain);return ()=>{item.cancelled=true;const i=waiting.indexOf(item);if(i>=0)waiting.splice(i,1);item.release();};}};
}
export function thumbnailQueue(api){let queue=queues.get(api);if(!queue){queue=createThumbnailQueue();queues.set(api,queue);}return queue;}
