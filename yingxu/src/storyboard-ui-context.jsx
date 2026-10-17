import React,{createContext,useContext,useMemo,useState} from 'react';
import {useI18n} from './i18n/I18nProvider.jsx';

const StoryboardContext=createContext(null);
// This is a view bridge, not an authoring reducer or task ledger. All durable
// mutations remain with the injected existing-store/application API adapter.
export function StoryboardUIProvider({adapter,children}){
  const {t}=useI18n();
  const [openProfile,setOpenProfile]=useState(null),[error,setError]=useState('');
  const value=useMemo(()=>{
    const state={...adapter.state,openProfile,error:error||adapter.state.error||''};
    const dispatch=async action=>{
      if(action.type==='openProfile'){setOpenProfile(action.id||null);return {ok:true};}
      setError('');
      try{
        const result=await adapter.dispatch(action);
        if(result?.ok===false){setError(result.error||t('storyboard.changeFailed'));return result;}
        if(action.type==='newProfile'&&(typeof result==='string'||result?.id))setOpenProfile(typeof result==='string'?result:result.id);
        return result;
      }catch(problem){const message=problem?.message||t('storyboard.operationUnknown');const displayed=message.startsWith('workbench.')?t(message):message;setError(displayed);return {ok:false,error:displayed};}
    };
    const db={...adapter.db,state,expand:(shot,locale)=>adapter.expand(shot,locale),planDrop:(shot,item,slot)=>adapter.planDrop(shot,item,slot)};
    return {state,dispatch,db,adapter,clearError:()=>setError('')};
  },[adapter,openProfile,error,t]);
  return <StoryboardContext.Provider value={value}>{children}</StoryboardContext.Provider>;
}
export function useStoryboardUI(){const value=useContext(StoryboardContext);if(!value)throw Error('Storyboard UI requires its existing-workspace adapter.');return value;}
