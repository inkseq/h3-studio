const MAX_MICROS = 9000000000000000n;
export function microsToDollars(value) {
  if (!Number.isSafeInteger(value) || value < 0 || BigInt(value) > MAX_MICROS)
    throw Error('invalid_money');
  const n = BigInt(value),
    fraction = String(n % 1000000n)
      .padStart(6, '0')
      .replace(/0+$/, '');
  return String(n / 1000000n) + (fraction ? '.' + fraction : '');
}
export function dollarsToMicros(value) {
  if (typeof value !== 'string' || !/^(?:0|[1-9]\d*)(?:\.\d{1,6})?$/.test(value))
    throw Error('invalid_money');
  const [whole, fraction = ''] = value.split('.'),
    n = BigInt(whole) * 1000000n + BigInt(fraction.padEnd(6, '0'));
  if (n > MAX_MICROS) throw Error('invalid_money');
  return Number(n);
}

// Display only: retain exact microdollars for editing and all API contracts.
export function formatMoneyMicros(value) {
  if (!Number.isSafeInteger(value)) return '—';
  const signed = BigInt(value),
    amount = signed < 0n ? -signed : signed;
  if (amount > MAX_MICROS) return '—';
  if (amount > 0n && amount < 10000n) return signed < 0n ? '>-$0.01' : '<$0.01';
  const cents = (amount + 5000n) / 10000n;
  return (
    (signed < 0n ? '-' : '') +
    '$' +
    String(cents / 100n) +
    '.' +
    String(cents % 100n).padStart(2, '0')
  );
}
