import React,{useEffect,useState} from 'react';
import {Server} from 'lucide-react';
import {createOperatorClient} from './operator-client.js';

/** Navigation discovery only; the server authorizes every operator request. */
export default function OperatorEntry({account,className=''}){
  const [visible,setVisible]=useState(false);
  useEffect(()=>{let active=true;setVisible(false);if(!account)return;const client=createOperatorClient();client.setAccount(account);client.me().then(value=>{if(active&&(value.username||value.account)===account&&value.operator_capacity?.view===true)setVisible(true);}).catch(()=>{});return()=>{active=false;client.reset();};},[account]);
  return visible?<a className={className} href="/operator"><Server size={15}/>GPU 控制台 ↗</a>:null;
}
