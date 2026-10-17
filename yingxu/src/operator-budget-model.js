import {translate} from './i18n/locale.js';
// Presentation only. The service owns budgets, settlement and price policy.
const amount=value=>Number.isSafeInteger(value)&&value>=0;
export function usdMicrousd(value){
  if(typeof value!=='string'||!/^(?:0|[1-9]\d*)(?:\.\d{1,6})?$/.test(value.trim()))return null;
  const [whole,fraction='']=value.trim().split('.'),result=Number(whole)*1000000+Number(fraction.padEnd(6,'0'));
  return amount(result)?result:null;
}
export function usdInput(value){
  if(!amount(value))return '';
  const whole=Math.floor(value/1000000),fraction=String(value%1000000).padStart(6,'0').replace(/0+$/,'');
  return String(whole)+(fraction?'.'+fraction:'');
}
export function budgetUsage(budget){
  if(budget?.limit_mode==='settings'||budget?.legacy_projection_only===true)return null;
  if(!amount(budget?.limit_microusd)||!amount(budget?.spent_microusd)||!amount(budget?.reserved_microusd))return null;
  const used=budget.spent_microusd+budget.reserved_microusd;
  if(!Number.isSafeInteger(used))return null;
  const ratio=budget.limit_microusd===0?1:used/budget.limit_microusd;
  return {used,percent:Math.min(100,Math.round(ratio*100)),status:ratio>=1?'exhausted':ratio>=.8?'warning':'ok'};
}
export function budgetLabel(budget,locale='zh'){return budget?.label||budget?.name||(budget?.owner_id?translate(locale,budget.project_id?'operatorPanel.projectBudget':'operatorPanel.ownerBudget',{owner:budget.owner_id,project:budget.project_id}):translate(locale,'operatorPanel.'+(budget?.id?.includes('-gpu-')?'sharedGpuBudget':budget?.id?.includes('-job-')?'sharedJobBudget':'sharedBudget')));}
export function budgetCosts(budget){
  const costs=budget?.cost_breakdown;
  if(costs?.basis!=='original_account_reservations'||costs.account_layers_not_additive!==true)return [];
  const rows=['job','instance'].map(kind=>({...costs[kind],kind}));
  if(!rows.every(row=>['confirmed_cost_microusd','confirmed_reservation_count','pending_invoice_count','pending_estimate_microusd'].every(key=>amount(row[key]))&&row.pending_actual_cost_microusd===null))return [];
  return rows.filter(row=>row.confirmed_reservation_count>0||row.pending_invoice_count>0);
}
export function validBudgetPage(value,account,{admin=false}={}){
  return value?.account===account&&value.basis==='cumulative'&&value.currency==='USD'&&value.timezone==='UTC'&&Array.isArray(value.budgets)&&value.budgets.length<=100&&value.budgets.every(budget=>typeof budget.id==='string'&&budget.id&&amount(budget.limit_microusd)&&amount(budget.spent_microusd)&&amount(budget.reserved_microusd)&&Number.isSafeInteger(budget.remaining_microusd)&&['ok','warning','exhausted'].includes(budget.status)&&typeof budget.snapshot==='string'&&budget.snapshot&&Array.isArray(budget.daily7)&&budget.daily7.length<=7&&budget.daily7.every(day=>/^\d{4}-\d{2}-\d{2}$/.test(day.date)&&amount(day.cost_microusd))&&(admin||budget.owner_id===account));
}
export function profilePrice(profile){
  if(!amount(profile?.max_price_microusd)||profile.max_price_microusd<=0)return null;
  const ceiling=profile.price_ceiling_microusd??profile.max_price_microusd;
  return amount(ceiling)&&ceiling>=profile.max_price_microusd?{default:profile.max_price_microusd,ceiling}:null;
}
export function rowPrice(row,profile){
  const policy=profilePrice(profile);if(!policy)return null;
  const selected=Object.hasOwn(row?.price_caps||{},profile.id)?usdMicrousd(row.price_caps[profile.id]):policy.default;
  return selected!==null&&selected>0&&selected<=policy.ceiling?selected:null;
}
export function withDefaultPrices(rows,profiles){
  const defaults=Object.fromEntries(profiles.map(profile=>[profile.id,usdInput(profile.max_price_microusd)]));
  return rows.map(row=>({...row,price_caps:{...defaults,...row.price_caps}}));
}
export function priceRows(rows,profiles,expanded){
  if(!expanded)return null;
  const result=expanded.map(item=>({...item,max_price_microusd:rowPrice(rows.find(row=>row.id===item.row_id),profiles.find(profile=>profile.id===item.profile_id))}));
  return result.every(item=>item.max_price_microusd!==null)?result:null;
}
export const wholeGiB=value=>typeof value==='number'&&Number.isFinite(value)&&value>=0?String(Math.round(value)):'—';
export function referenceOffers(offers=[]){
  const groups=new Map();
  for(const offer of offers){
    const key=JSON.stringify([offer.backend,offer.region,offer.gpu_type,offer.gpu_count??1,offer.cpu_count,offer.memory_gib,offer.gpu_memory_gib,offer.disk_gib,offer.hourly_cost_microusd]);
    if(!groups.has(key))groups.set(key,{key,offer,count:0,confirmed:0});
    const group=groups.get(key);group.count++;if(offer.inventory_confirmed===true)group.confirmed++;
  }
  return [...groups.values()];
}
