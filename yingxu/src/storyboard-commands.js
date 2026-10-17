// Durable operation identity, not a second job or financial ledger.
const clone=value=>JSON.parse(JSON.stringify(value));
export function storyboardCommandStore({storage,account,projectId,keyFactory=()=>crypto.randomUUID()}) {
 const name=`inkseq-storyboard-command-v1:${account}:${projectId}`;
 const read=()=>{try{return JSON.parse(storage.getItem(name)||'null');}catch{throw Error('workbench.pendingCorrupt');}};
 return {read,begin(kind,body){if(read())throw Error('workbench.pending');const op={account,projectId,kind,body:clone(body),key:'storyboard-'+keyFactory()};storage.setItem(name,JSON.stringify(op));return op;},
 recordReceipt(op,id){if(typeof id!=='string'||!id||op.receiptId&&op.receiptId!==id||JSON.stringify(read())!==JSON.stringify(op))throw Error('workbench.receipt');const next={...op,receiptId:id};storage.setItem(name,JSON.stringify(next));return next;},
 clear(op){const current=read();if(JSON.stringify(current)!==JSON.stringify(op))throw Error('workbench.pendingChanged');storage.removeItem(name);}};
}
const canonical = value => Array.isArray(value) ? value.map(canonical) : value && typeof value === 'object' ? Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])])) : value;
const equal = (a, b) => JSON.stringify(canonical(a)) === JSON.stringify(canonical(b));
export function verifyStoryboardReceipt(op, value) {
  if (['preferences', 'copies'].includes(op.kind)) {
    return Number.isInteger(value?.version) && value.version > op.body.expected_version
      && (!Object.hasOwn(op.body, 'defaults') || equal(value.defaults, op.body.defaults))
      && (!Object.hasOwn(op.body, 'copies_max') || value.copies_max === op.body.copies_max)
      && (!Object.hasOwn(op.body, 'takes_fold_count') || value.takes_fold_count === op.body.takes_fold_count);
  }
  if (op.kind === 'actions') return value?.id === op.projectId && value?.project?.id === op.projectId
    && Number.isInteger(value.version) && value.version > op.body.expected_version;
  if (op.kind === 'preflight') {
    if (value?.project_id !== op.projectId || value.project_version !== op.body.expected_version
      || typeof value.id !== 'string' || !value.id || !Array.isArray(value.items)
      || value.items.length !== op.body.shot_ids.length * op.body.copies) return false;
    const seen = new Set(), plans = new Set();
    return value.items.every(item => {
      const identity = `${item.shot_id}:${item.index}`;
      if (!op.body.shot_ids.includes(item.shot_id) || !Number.isInteger(item.index)
        || item.index < 0 || item.index >= op.body.copies || seen.has(identity)
        || typeof item.plan_id !== 'string' || !item.plan_id || plans.has(item.plan_id)) return false;
      seen.add(identity); plans.add(item.plan_id); return true;
    });
  }
  if (op.kind === 'batch') return value?.client_project_id === op.projectId && typeof value.id === 'string'
    && Array.isArray(value.items) && value.items.length === op.body.plan_ids.length
    && value.items.every((item, i) => item.plan_id === op.body.plan_ids[i]);
  return false;
}
