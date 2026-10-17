import React from 'react';
import {store} from './store.js';

// Failed module imports may remain cached for this document. Reload only after
// the current local/owner-scoped draft has actually reached browser storage.
export function reloadAfterCheckpoint(target=store,reload=()=>globalThis.location.reload()){
  if(!target.checkpoint())return false;
  reload();return true;
}

export function deferredPanel(load,label){
  const Initial=React.lazy(load);
  class DeferredPanel extends React.Component {
    state={failed:false,saveFailed:false,backupUrl:null};
    static getDerivedStateFromError(){return {failed:true};}
    reload=()=>{if(!reloadAfterCheckpoint())this.setState({saveFailed:true});};
    backup=()=>{
      if(this.state.backupUrl)URL.revokeObjectURL(this.state.backupUrl);
      const blob=new Blob([JSON.stringify(store.exportProject(),null,2)],{type:'application/json'});
      this.setState({backupUrl:URL.createObjectURL(blob)});
    };
    componentWillUnmount(){if(this.state.backupUrl)URL.revokeObjectURL(this.state.backupUrl);}
    render(){
      if(this.state.failed)return React.createElement('section',{className:'panel-load-error',role:'alert'},
        React.createElement('b',null,`${label}暂时未能打开`),
        React.createElement('p',null,'可继续使用其他步骤。检查连接后，先把当前草稿保存在此浏览器，再刷新页面重新打开。云草稿需用原账户打开项目并选择恢复；刷新不会提交生成或上传云版本。'),
        React.createElement('button',{type:'button',onClick:this.reload},'保存草稿并刷新页面'),
        this.state.saveFailed&&React.createElement('p',null,'当前或先前账户仍有未落盘草稿，已阻止刷新。请用原账户恢复后下载结构备份，并保留本页面；备份不含图片、视频、音频文件，素材仍留在原位置。'),
        React.createElement('button',{type:'button',onClick:this.backup},'准备项目结构备份（不含媒体）'),
        this.state.backupUrl&&React.createElement('a',{href:this.state.backupUrl,download:'映序项目结构备份.json'},'下载项目结构备份（不含媒体）'));
      const fallback=React.createElement('section',{className:'panel-loading',role:'status','aria-live':'polite','aria-busy':true},
        React.createElement('b',null,`正在打开${label}…`),
        React.createElement('p',null,'首次打开需要读取这部分界面；作品未改变，仍可切换步骤。'));
      return React.createElement(React.Suspense,{fallback},React.createElement(Initial,this.props));
    }
  }
  DeferredPanel.displayName='Deferred'+label;
  return DeferredPanel;
}
