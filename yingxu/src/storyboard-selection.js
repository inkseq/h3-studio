// View-only selection; project identity is owned by the caller.
export function selectShot(state, orderedIds, id, {range=false,toggle=false}={}) {
  if (!orderedIds.includes(id)) return state;
  const selected=new Set(state.ids.filter(value=>orderedIds.includes(value)));
  if(range && orderedIds.includes(state.anchor)) {
    const a=orderedIds.indexOf(state.anchor),b=orderedIds.indexOf(id);
    for(const value of orderedIds.slice(Math.min(a,b),Math.max(a,b)+1)) selected.add(value);
  } else if(toggle) { if(selected.has(id))selected.delete(id);else selected.add(id); }
  else { selected.clear();selected.add(id); }
  return {ids:orderedIds.filter(value=>selected.has(value)),anchor:range?state.anchor:id};
}
export const emptySelection=()=>({ids:[],anchor:null});
export function recipeChanges(current,previous) {
  if(!current || !previous)return null;
  const keys=new Set([...Object.keys(current),...Object.keys(previous)]);
  return [...keys].filter(key=>JSON.stringify(current[key])!==JSON.stringify(previous[key]));
}
