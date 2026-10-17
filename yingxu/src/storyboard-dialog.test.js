import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {Module} from 'node:module';
import {build} from 'esbuild';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';

const filename=fileURLToPath(new URL('./StoryboardDialog.test.cjs',import.meta.url));
const built=await build({entryPoints:[fileURLToPath(new URL('./StoryboardDialog.jsx',import.meta.url))],bundle:true,write:false,format:'cjs',platform:'node',external:['react','react-dom']});
const compiled=new Module(filename);compiled.filename=filename;compiled.paths=Module._nodeModulePaths(fileURLToPath(new URL('.',import.meta.url)));compiled._compile(built.outputFiles[0].text,filename);
const {StoryboardNameForm}=compiled.exports;
function elements(node){if(!node||typeof node!=='object')return [];return [node,...React.Children.toArray(node.props?.children).flatMap(elements)];}
const button=(form,type)=>elements(form).find(node=>node.type==='button'&&node.props.type===type);

test('the actual naming form labels its input and submits one trimmed nonempty name',()=>{
  const received=[],drafts=[];let prevented=0;
  const props={label:'人物名称',name:'  Mira  ',onName:name=>drafts.push(name),onSubmit:name=>received.push(name),onCancel:()=>assert.fail('submission must not cancel')};
  const form=StoryboardNameForm(props),input=elements(form).find(node=>node.type==='input');
  const html=renderToStaticMarkup(React.createElement(StoryboardNameForm,props));
  assert.match(html,/<label>人物名称<input/);assert.equal(input.props.required,true);assert.equal(input.props.autoFocus,true);
  input.props.onChange({target:{value:'  Mira blue  '}});assert.deepEqual(drafts,['  Mira blue  ']);
  form.props.onSubmit({preventDefault(){prevented++;}});
  assert.equal(prevented,1);assert.deepEqual(received,['Mira']);
});

test('blank drafts and pending creation cannot submit; failure text keeps the draft',()=>{
  for(const state of [{name:'   '},{name:'Mira',busy:true}]){
    const form=StoryboardNameForm({label:'人物名称',onName(){},onSubmit(){assert.fail('invalid or pending creation must not submit');},onCancel(){},...state});
    assert.equal(button(form,'submit').props.disabled,true);
    form.props.onSubmit({preventDefault(){}});
  }
  const html=renderToStaticMarkup(React.createElement(StoryboardNameForm,{label:'造型名称',name:'Red coat',error:'原记录尚未确认',onName(){},onSubmit(){},onCancel(){}}));
  assert.match(html,/value="Red coat"/);assert.match(html,/role="alert"[^>]*>原记录尚未确认/);
});

test('cancelling the actual naming form does not submit its draft',()=>{
  let cancelled=0;
  const form=StoryboardNameForm({label:'版本名称',name:'Rainy night',onName(){},onSubmit(){assert.fail('cancel must not submit');},onCancel(){cancelled++;}});
  const cancel=button(form,'button');
  assert.equal(cancel.props.children,'取消');cancel.props.onClick();assert.equal(cancelled,1);
});

test('Layout A no longer opens native JavaScript naming or destructive dialogs',async()=>{
  const source=await readFile(new URL('./StoryboardComponents.jsx',import.meta.url),'utf8');
  assert.doesNotMatch(source,/\b(?:window\.)?(?:prompt|confirm)\s*\(/);
  assert.match(source,/StoryboardNameDialog/);assert.match(source,/StoryboardDeleteDialog/);
});
