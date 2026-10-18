// Display-only native media queue. No API writes, retries or persistent state.
const queues=new WeakMap();
export function createThumbnailQueue({maxActive=8}={}){
 if(!Number.isSafeInteger(maxActive)||maxActive<1||maxActive>8)throw new RangeError('Invalid thumbnail concurrency');
 const active=new Set(),waiting=[];
 function drain(){while(active.size<maxActive&&waiting.length){const item=waiting.shift();if(item.cancelled)continue;active.add(item);try{item.start(item.release);}catch(error){item.release();queueMicrotask(()=>{throw error;});}}}
 return {enqueue(start){const item={start,cancelled:false,release:null};item.release=()=>{if(!active.delete(item))return;queueMicrotask(drain);};waiting.push(item);queueMicrotask(drain);return ()=>{item.cancelled=true;const i=waiting.indexOf(item);if(i>=0)waiting.splice(i,1);item.release();};}};
}
export function thumbnailQueue(api){let queue=queues.get(api);if(!queue){queue=createThumbnailQueue();queues.set(api,queue);}return queue;}
