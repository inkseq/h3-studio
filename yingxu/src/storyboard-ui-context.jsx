import React,{createContext,useContext,useMemo,useState} from 'react';

const StoryboardContext=createContext(null);
// This is a view bridge, not an authoring reducer or task ledger. All durable
// mutations remain with the injected existing-store/application API adapter.
export function StoryboardUIProvider({adapter,children}){
  const [openProfile,setOpenProfile]=useState(null),[error,setError]=useState('');
  const value=useMemo(()=>{
    const state={...adapter.state,openProfile,error:error||adapter.state.error||''};
    const dispatch=async action=>{
      if(action.type==='openProfile'){setOpenProfile(action.id||null);return {ok:true};}
      setError('');
      try{
        const result=await adapter.dispatch(action);
        if(result?.ok===false){setError(result.error||'这次修改没有完成，原记录已保留。');return result;}
        if(action.type==='newProfile'&&(typeof result==='string'||result?.id))setOpenProfile(typeof result==='string'?result:result.id);
        return result;
      }catch(problem){const message=problem?.message||'这次操作尚未确认，请核对原记录。';setError(message);return {ok:false,error:message};}
    };
    const db={...adapter.db,state,expand:shot=>adapter.expand(shot),planDrop:(shot,item,slot)=>adapter.planDrop(shot,item,slot)};
    return {state,dispatch,db,adapter,clearError:()=>setError('')};
  },[adapter,openProfile,error]);
  return <StoryboardContext.Provider value={value}>{children}</StoryboardContext.Provider>;
}
export function useStoryboardUI(){const value=useContext(StoryboardContext);if(!value)throw Error('Storyboard UI requires its existing-workspace adapter.');return value;}
