import test from 'node:test';
import assert from 'node:assert/strict';
import {selectShot,emptySelection} from './storyboard-selection.js';
test('range uses visible order, preserves toggle and drops removed identities',()=>{
 const ids=['a','b','c','d'];let s=selectShot(emptySelection(),ids,'b',{toggle:true});
 s=selectShot(s,ids,'d',{range:true});assert.deepEqual(s.ids,['b','c','d']);
 s=selectShot(s,ids,'c',{toggle:true});assert.deepEqual(s.ids,['b','d']);
 s=selectShot(s,['d','a'],'a',{range:true});assert.deepEqual(s.ids,['a']);
 assert.deepEqual(selectShot(s,['d','a'],'foreign'),s);
});
