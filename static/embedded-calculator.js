(function () {
  'use strict';
  const $ = id => document.getElementById('embed-' + id), form = $('calc-form'), math = window.LateFeeMath;
  if (!form || !math) return;
  const money = n => new Intl.NumberFormat('en-US', {style:'currency',currency:'USD'}).format(n === 0 ? 0 : n/100);
  function calculate(announce) {
    $('result').hidden = true; $('error').hidden = true; $('announcement').textContent = '';
    try {
      const input = Object.fromEntries(['tuition','credit','days','grace','type','amount','dailyRate','cap'].map(id=>[id,$(id).value.trim()]));
      if (!/^\d{1,4}$/.test(input.days) || !/^\d{1,3}$/.test(input.grace)) throw Error('Enter whole numbers for overdue days and grace days.');
      const r = math.calculate(input);
      $('balance').textContent = money(r.balance); $('fee').textContent = money(r.fee); $('total').textContent = money(r.total);
      $('explanation').textContent = r.days + ' overdue day(s), less ' + r.grace + ' grace day(s): ' + r.billable + ' billable day(s).' + (r.capped ? ' The fee cap reduced the late fee.' : '') + (input.type === 'weekly' ? ' Each started group of seven billable days incurs one weekly fee.' : '');
      $('result').hidden = false;
      if (announce) $('announcement').textContent = 'Estimated total remaining due: ' + money(r.total) + ', including ' + money(r.fee) + ' in late fees.';
    } catch (error) {
      ['balance','fee','total','explanation'].forEach(id=>$(id).textContent='');
      $('error').textContent = error.message; $('error').hidden = false;
    }
  }
  function sync() {
    const kind = $('type').value;
    $('daily-field').hidden = kind !== 'flat_daily'; $('dailyRate').disabled = kind !== 'flat_daily';
    $('rate-label').textContent = {flat:'One-time fee ($)',daily:'Daily fee ($)',weekly:'Fee per started week ($)',percent:'One-time percentage (%)',flat_daily:'One-time flat fee ($)'}[kind] || 'Fee amount';
  }
  form.addEventListener('submit', event=>{event.preventDefault();sync();calculate(true);});
  form.addEventListener('input',()=>{sync();calculate(false);});
  form.addEventListener('change',()=>{sync();calculate(false);});
  sync(); calculate(false);
}());
