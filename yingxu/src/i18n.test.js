import test from 'node:test';
import assert from 'node:assert/strict';
import {catalogs} from './i18n/catalogs.js';
import {DEFAULT_LOCALE,LOCALE_STORAGE_KEY,readLocale,rememberLocale,translate} from './i18n/locale.js';

test('English is the first-visit default; browser and individual accounts remember their choice',()=>{
  const records=new Map(),storage={getItem:key=>records.get(key)??null,setItem:(key,value)=>records.set(key,value)};
  assert.equal(DEFAULT_LOCALE,'en');assert.equal(readLocale(storage),'en');
  rememberLocale(storage,'zh','one');assert.equal(readLocale(storage,'one'),'zh');
  rememberLocale(storage,'en','two');assert.equal(readLocale(storage,'one'),'zh');
  assert.equal(readLocale(storage,'two'),'en');assert.equal(readLocale(storage),'en');
  assert.equal(readLocale({getItem(){throw Error('blocked');}}),'en');
  assert.doesNotThrow(()=>rememberLocale({setItem(){throw Error('blocked');}},'zh','one'));
  assert.throws(()=>rememberLocale(storage,'fr'),/unsupported_locale/);
  records.set(LOCALE_STORAGE_KEY,'invalid');assert.equal(readLocale(storage),'en');
});

test('Both catalogs contain the same keys and named values; user input is interpolated only once',()=>{
  assert.deepEqual(Object.keys(catalogs.en).sort(),Object.keys(catalogs.zh).sort());
  const variables=text=>[...text.matchAll(/\{([A-Za-z]\w*)\}/g)].map(match=>match[1]).sort();
  for(const key of Object.keys(catalogs.en))assert.deepEqual(variables(catalogs.en[key]),variables(catalogs.zh[key]),key);
  assert.equal(translate('en','settings.usage',{amount:'{privatePrompt}'}),'Admission exposure: {privatePrompt}');
});
