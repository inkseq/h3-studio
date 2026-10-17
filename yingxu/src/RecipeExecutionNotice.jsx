import React from 'react';
import {recipeExecutionStatus} from './recipe-execution-model.js';

export default function RecipeExecutionNotice({recipe,project,shot}){
  const status=recipeExecutionStatus(recipe,{project,shot});
  return <section className="cloud-warning" aria-label="当前模式的云端执行范围" aria-live="polite"><b>{status.title}</b><p>{status.detail}</p>{status.issues.length>0&&<ul>{status.issues.map(issue=><li key={issue}>{issue}</li>)}</ul>}{status.summary.length>0&&<details><summary>查看当前可提交的输入和参数范围</summary><ul>{status.summary.map(item=><li key={item}>{item}</li>)}</ul></details>}</section>;
}
