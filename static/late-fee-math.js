/* Money is stored in cents; calendar dates use UTC so DST cannot add a day. */
(function (root) {
  'use strict';
  const TYPES = ['flat', 'daily', 'weekly', 'percent', 'flat_daily'];
  function number(value, name, max, integer) {
    const n = Number(value);
    if (value === '' || !Number.isFinite(n) || n < 0 || n > max || (integer && !Number.isInteger(n))) {
      throw new Error('Enter a valid ' + name + '.');
    }
    return n;
  }
  function cents(value) {
    const n = number(value, 'dollar amount (0–1,000,000)', 1000000, false);
    if (!/^\d+(\.\d{1,2})?$/.test(String(value))) throw new Error('Use dollars with no more than two decimal places.');
    return Math.round(n * 100);
  }
  function dateValue(value) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) throw new Error('Choose both dates.');
    const [y, m, d] = value.split('-').map(Number);
    if (y < 1900 || y > 9999) throw new Error('Use dates between 1900 and 9999.');
    const ms = Date.UTC(y, m - 1, d);
    if (new Date(ms).toISOString().slice(0, 10) !== value) throw new Error('Choose a valid date.');
    return ms;
  }
  function daysBetween(due, asof) {
    const days = Math.max(0, (dateValue(asof) - dateValue(due)) / 86400000);
    if (days > 3660) throw new Error('Use a date range of no more than 3,660 days.');
    return days;
  }
  function calculate(input) {
    if (!TYPES.includes(input.type)) throw new Error('Choose a fee type.');
    const tuition = cents(input.tuition), credit = cents(input.credit);
    if (credit > tuition) throw new Error('Payment or credit cannot exceed tuition.');
    const days = number(input.days, 'number of overdue days (0–3,660)', 3660, true);
    const grace = number(input.grace, 'grace period (0–365 days)', 365, true);
    const balance = tuition - credit, billable = Math.max(0, days - grace);
    const amount = input.type === 'percent' ? number(input.amount, 'percentage (0–100)', 100, false) : cents(input.amount);
    if (input.type === 'percent' && !/^\d+(\.\d{1,2})?$/.test(String(input.amount))) throw new Error('Use a percentage with no more than two decimal places.');
    const daily = input.type === 'flat_daily' ? cents(input.dailyRate) : 0;
    const cap = input.cap === '' ? null : cents(input.cap);
    let rawFee = 0;
    if (billable > 0 && balance > 0) {
      if (input.type === 'flat') rawFee = amount;
      if (input.type === 'daily') rawFee = amount * billable;
      if (input.type === 'weekly') rawFee = amount * Math.ceil(billable / 7);
      if (input.type === 'percent') rawFee = Math.round(balance * Math.round(amount * 100) / 10000);
      if (input.type === 'flat_daily') rawFee = amount + daily * billable;
    }
    const fee = cap === null ? rawFee : Math.min(rawFee, cap);
    return { tuition, credit, balance, days, grace, billable, rawFee, fee, total: balance + fee, cap, capped: fee < rawFee };
  }
  const api = { calculate, daysBetween, cents };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.LateFeeMath = api;
})(typeof window !== 'undefined' ? window : globalThis);
