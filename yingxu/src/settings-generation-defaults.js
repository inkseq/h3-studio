export function checkedGenerationDefaults(value){
 if(!Number.isSafeInteger(value?.version)||value.version<0||!value.defaults||typeof value.defaults!=='object'||Array.isArray(value.defaults)||Object.keys(value.defaults).some(key=>!['recipe_id','deployment_profile_id','controls'].includes(key))||value.defaults.controls!==undefined&&(!value.defaults.controls||typeof value.defaults.controls!=='object'||Array.isArray(value.defaults.controls)))throw Error('settings.defaultsFailed');
 return value;
}
export function generationDefaultsEditor(snapshot,account){
 if(!account||snapshot?.workspace?.mode!=='cloud'||snapshot.workspace.account!==account||!snapshot.project?.id)return null;
 const shots=(snapshot.project.entities||[]).filter(entity=>entity.type==='shot');
 const shot=shots.find(entity=>entity.id===snapshot.selectedId)||shots[0];
 if(!shot)return null;
 return '/?'+new URLSearchParams({view:'workspace',project:snapshot.project.id,entity:shot.id,defaults:'account'});
}
