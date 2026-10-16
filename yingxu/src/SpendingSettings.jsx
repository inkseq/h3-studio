import React,{useEffect,useMemo,useRef,useState} from 'react';
import {createCloudClient} from './cloud-client.js';
import {LanguageSwitcher,useI18n} from './i18n/I18nProvider.jsx';
import './spending-settings.css';

const dollars=value=>String(value/1_000_000);
function micros(value){
  if(!/^(?:0|[1-9]\d*)(?:\.\d{1,6})?$/.test(value))throw Error('settings.invalid');
  const [whole,fraction='']=value.split('.'),result=Number(whole)*1_000_000+Number(fraction.padEnd(6,'0'));
  if(!Number.isSafeInteger(result)||result>9_000_000_000_000_000)throw Error('settings.invalid');
  return result;
}
const money=value=>new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',maximumFractionDigits:6}).format(value/1_000_000);

export default function SpendingSettings({account}){
  const {t}=useI18n(),api=useMemo(()=>{const client=createCloudClient();client.setAccount(account);return client;},[account]);
  useEffect(()=>{document.title=t('settings.documentTitle');},[t]);
  const [user,setUser]=useState(null),[admin,setAdmin]=useState(null),[draft,setDraft]=useState(null),[adminDraft,setAdminDraft]=useState(null);
  const [busy,setBusy]=useState(false),[notice,setNotice]=useState(''),[error,setError]=useState(''),epoch=useRef(0);
  const load=async()=>{
    const current=epoch.current;setBusy(true);setError('');
    try{
      const data=await api.request('/v1/account/spending-settings');
      if(current!==epoch.current)return;
      setUser(data);setDraft({daily:dollars(data.value.daily_limit_microusd),ask:dollars(data.value.ask_above_microusd),
        connections:Object.fromEntries(data.connections.map(row=>[row.actor_id,dollars(row.daily_limit_microusd)]))});
      if(data.admin){const next=await api.request('/v1/operator/admin/spending-settings');if(current!==epoch.current)return;setAdmin(next);setAdminDraft({
        testDaily:dollars(next.value.test_daily_limit_microusd),hourly:dollars(next.value.max_hourly_price_microusd),fleetHourly:dollars(next.value.fleet_hourly_limit_microusd),
        idle:String(next.value.idle_shutdown_seconds/60),runtime:String(next.value.max_runtime_seconds/60)});}
    }catch(failure){if(current===epoch.current)setError(failure.status===401?t('settings.login'):failure.message);}
    finally{if(current===epoch.current)setBusy(false);}
  };
  useEffect(()=>{epoch.current++;setUser(null);setAdmin(null);setDraft(null);setNotice('');void load();return()=>{epoch.current++;api.reset();};},[api]);
  async function save(isAdmin=false){
    const current=epoch.current;setBusy(true);setError('');setNotice('');
    try{
      let body;
      if(isAdmin){
        const seconds=field=>{const value=Number(adminDraft[field])*60;if(!Number.isSafeInteger(value)||value<1)throw Error('settings.invalid');return value;};
        body={expected_version:admin.version,value:{test_daily_limit_microusd:micros(adminDraft.testDaily),
          max_hourly_price_microusd:micros(adminDraft.hourly),fleet_hourly_limit_microusd:micros(adminDraft.fleetHourly),idle_shutdown_seconds:seconds('idle'),max_runtime_seconds:seconds('runtime')}};
      }else body={expected_version:user.version,value:{daily_limit_microusd:micros(draft.daily),ask_above_microusd:micros(draft.ask),
        connection_limits:{...user.value.connection_limits,...Object.fromEntries(Object.entries(draft.connections).map(([key,value])=>[key,micros(value)]))}}};
      const result=await api.request(isAdmin?'/v1/operator/admin/spending-settings':'/v1/account/spending-settings',{method:'PUT',body});
      if(current!==epoch.current)return;
      if(isAdmin)setAdmin(result);else setUser(previous=>({...previous,...result}));
      setNotice('settings.saved');
    }catch(failure){if(current===epoch.current)setError(failure.message==='settings.invalid'?t(failure.message):failure.message);}
    finally{if(current===epoch.current)setBusy(false);}
  }
  const field=(label,value,onChange)=><label className="spending-field"><span>{t(label)}</span><input inputMode="decimal" value={value} disabled={busy} onChange={event=>onChange(event.target.value)}/></label>;
  return <main className="spending-page"><header><a href="/quick-chat">{t('settings.back')}</a><LanguageSwitcher/></header><h1>{t('settings.title')}</h1><p><a href="/quick-chat?preferences=takes">{t('takes.preferences')}</a></p>
    {error&&<p role="alert">{error}</p>}{notice&&<p role="status">{t(notice)}</p>}
    {!user||!draft?<p role="status">{busy?t('settings.loading'):t('settings.login')}</p>:<>
      <form onSubmit={event=>{event.preventDefault();void save();}}><section aria-labelledby="budget-settings-title"><h2 id="budget-settings-title">{t('settings.budget')}</h2>
        {field('settings.daily',draft.daily,value=>setDraft(previous=>({...previous,daily:value})))}
        {field('settings.ask',draft.ask,value=>setDraft(previous=>({...previous,ask:value})))}
        <p>{t('settings.zero')}</p><p>{t('settings.day')}</p><p>{t('settings.usage',{amount:money(user.usage.projected_microusd)})}</p>
      </section><section aria-labelledby="assistant-settings-title"><h2 id="assistant-settings-title">{t('settings.assistants')}</h2>
        {!user.connections.length&&<p>{t('settings.noAssistants')}</p>}{user.connections.map(row=><article key={row.actor_id}><h3>{row.name} {row.revoked&&<small>{t('settings.revoked')}</small>}</h3>
          {field('settings.cap',draft.connections[row.actor_id],value=>setDraft(previous=>({...previous,connections:{...previous.connections,[row.actor_id]:value}})))}
        </article>)}
      </section><button disabled={busy}>{t(busy?'settings.saving':'settings.save')}</button></form>
      {admin&&adminDraft&&<form onSubmit={event=>{event.preventDefault();void save(true);}}><section aria-labelledby="admin-settings-title"><h2 id="admin-settings-title">{t('settings.admin')}</h2>
        {[['settings.testDaily','testDaily'],['settings.hourly','hourly'],['settings.fleetHourly','fleetHourly'],['settings.idle','idle'],['settings.runtime','runtime']].map(([label,key])=><React.Fragment key={key}>{field(label,adminDraft[key],value=>setAdminDraft(previous=>({...previous,[key]:value})))}</React.Fragment>)}
        <p>{t('settings.adminHelp')}</p></section><button disabled={busy}>{t(busy?'settings.saving':'settings.save')}</button></form>}
    </>}<button disabled={busy} onClick={()=>void load()}>{t('settings.reload')}</button></main>;
}
