import test from 'node:test';
import assert from 'node:assert/strict';
import {microsToDollars,dollarsToMicros} from './money.js';
test('exact accepted money round trips without floating point fractional arithmetic',()=>{
 for(const n of [0,1,2345678,8999999999999999,9000000000000000])assert.equal(dollarsToMicros(microsToDollars(n)),n);
 assert.equal(microsToDollars(8999999999999999),'8999999999.999999');
 for(const v of ['-1','0.0000001','1e3',true,1,null,'9000000000.000001'])assert.throws(()=>dollarsToMicros(v));
});
