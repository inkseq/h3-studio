import React from 'react';
import './generation-availability.css';
import {MODE_LABELS,modeAvailability} from './generation-availability.js';

export default function GenerationAvailability({snapshot,capabilities,settings,operator=false,compact=false,onRefresh,checking=false}){
  const current=modeAvailability(snapshot,capabilities,settings);
  return <section className={'qc-availability '+(compact?'qc-availability-compact':'')} aria-label="当前生成容量" role="status">
    <div className="qc-availability-modes">{(compact?[current.mode]:['fl','ref']).filter(Boolean).map(mode=>{const item=modeAvailability(snapshot,capabilities,settings,mode);return <span key={mode} className={'qc-availability-mode '+(item.available?'available':'unavailable')} aria-current={mode===current.mode?'true':undefined}><b>{MODE_LABELS[mode]}</b><span>{item.label}</span></span>;})}</div>
    <p>{current.message}</p>
    <div className="qc-availability-actions">{onRefresh&&<button type="button" disabled={checking} onClick={onRefresh}>{checking?'核对中…':'刷新状态'}</button>}{operator&&!current.available&&<a href="/operator">打开 GPU 控制台 ↗</a>}</div>
  </section>;
}
