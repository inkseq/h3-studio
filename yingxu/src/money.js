const MAX_MICROS=9000000000000000n;
export function microsToDollars(value){
 if(!Number.isSafeInteger(value)||value<0||BigInt(value)>MAX_MICROS)throw Error('invalid_money');
 const n=BigInt(value),fraction=String(n%1000000n).padStart(6,'0').replace(/0+$/,'');
 return String(n/1000000n)+(fraction?'.'+fraction:'');
}
export function dollarsToMicros(value){
 if(typeof value!=='string'||!/^(?:0|[1-9]\d*)(?:\.\d{1,6})?$/.test(value))throw Error('invalid_money');
 const [whole,fraction='']=value.split('.'),n=BigInt(whole)*1000000n+BigInt(fraction.padEnd(6,'0'));
 if(n>MAX_MICROS)throw Error('invalid_money');return Number(n);
}
