import React,{useEffect,useState,useSyncExternalStore} from 'react';
import {store} from './store.js';
import './persistence-warning.css';

export default function PersistenceWarning(){
 useSyncExternalStore(store.subscribe,store.getState,store.getState);
 const risk=store.getPersistenceRisk(),[url,setUrl]=useState('');
 useEffect(()=>()=>{if(url)URL.revokeObjectURL(url);},[url]);
 if(!risk.current&&!risk.otherCloudDrafts)return null;
 const prepare=()=>setUrl(URL.createObjectURL(new Blob([JSON.stringify(store.exportProject(),null,2)],{type:'application/json'})));
 return <section className="persistence-warning" role="alert" aria-label="未落盘草稿保护"><b>有草稿尚未写入此浏览器，请先保留本页</b><p>{risk.current?'当前作品的最新修改只在本页内存，关闭或强制刷新可能丢失。':'之前云账户仍有仅在本页内存的草稿，请用原账户打开对应项目并恢复后备份；当前作品的备份不包含它。'} 项目结构备份不含图片、视频或音频文件，原素材不会被删除。</p><div><button onClick={()=>{if(store.checkpoint())store.notify('草稿已保存到此浏览器，可以安全刷新；云版本仍需按需保存。');}}>重试保存到此浏览器</button>{risk.current&&<button onClick={prepare}>准备当前作品结构备份</button>}{url&&<a href={url} download="映序-未落盘项目结构.json">下载结构备份（不含媒体）</a>}</div>{url&&<p>备份包含点击“准备”时的内容；继续编辑后请重新准备。</p>}</section>;
}
