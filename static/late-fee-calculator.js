(function () {
  'use strict';
  const $ = id => document.getElementById(id);
  const math = window.LateFeeMath;
  const form = $('lfc-form');
  const money = n => new Intl.NumberFormat('en-US', {style:'currency', currency:'USD'}).format(n / 100);
  const fields = ['tuition','credit','days','due','asof','type','amount','dailyRate','grace','cap','families','cycles'];
  const labels = {flat:'Flat fee ($)', daily:'Daily fee ($)', weekly:'Weekly fee ($)', percent:'Fee percentage (%)', flat_daily:'One-time flat fee ($)'};
  const help = {flat:'A single fee applies on the first day after the grace period.', daily:'The daily fee starts after grace ends. Grace days are not billed.', weekly:'Each started group of 7 billable days incurs one weekly fee.', percent:'A one-time percentage of unpaid tuition; no compounding.', flat_daily:'Both the flat fee and the first daily fee apply on the first day after grace ends.'};
  let current = null;
  function localDate(date) { return date.getFullYear() + '-' + String(date.getMonth()+1).padStart(2,'0') + '-' + String(date.getDate()).padStart(2,'0'); }
  function setDateDefaults() {
    const now = new Date(), due = new Date(now.getFullYear(),now.getMonth(),now.getDate()-7);
    $('due').value = localDate(due); $('asof').value = localDate(now);
  }
  function mode() { return form.querySelector('[name="timing"]:checked').value; }
  function sync() {
    const dates = mode() === 'dates', combined = $('type').value === 'flat_daily';
    $('date-fields').hidden = !dates; $('days-fields').hidden = dates;
    $('days').disabled = dates; $('due').disabled = !dates; $('asof').disabled = !dates;
    $('daily-field').hidden = !combined; $('dailyRate').disabled = !combined;
    $('amount-label').textContent = labels[$('type').value];
    $('amount').max = $('type').value === 'percent' ? '100' : '1000000';
    $('model-help').textContent = help[$('type').value];
  }
  function read() {
    const input = Object.fromEntries(fields.map(id => [id, $(id).value]));
    input.days = mode() === 'dates' ? math.daysBetween(input.due,input.asof) : input.days;
    return input;
  }
  function policy(input) {
    const rate = input.type === 'percent' ? input.amount + '%' : money(math.cents(input.amount));
    if (input.type === 'flat_daily') return rate+' once + '+money(math.cents(input.dailyRate))+' per billable day';
    return {flat:rate+' once', daily:rate+' per billable day', weekly:rate+' per started week', percent:rate+' of unpaid tuition once'}[input.type];
  }
  function explanation(input,r) {
    if (r.balance === 0) return 'No new late fee: tuition is fully paid before fees accrue.';
    if (r.billable === 0) return 'No late fee: payment is not overdue or is still within the grace period.';
    let text;
    if (input.type === 'flat') text = money(math.cents(input.amount)) + ' one-time flat fee';
    if (input.type === 'daily') text = money(math.cents(input.amount)) + ' × ' + r.billable + ' billable day(s)';
    if (input.type === 'weekly') text = money(math.cents(input.amount)) + ' × ' + Math.ceil(r.billable/7) + ' started week(s)';
    if (input.type === 'percent') text = input.amount + '% × ' + money(r.balance) + ' unpaid tuition';
    if (input.type === 'flat_daily') text = money(math.cents(input.amount)) + ' + (' + money(math.cents(input.dailyRate)) + ' × ' + r.billable + ' billable day(s))';
    return text + ' = ' + money(r.rawFee) + (r.capped ? ' before the cap.' : '.');
  }
  function row(body, cells, selected) {
    const tr = document.createElement('tr'); if (selected) tr.className = 'selected';
    cells.forEach((text,i) => { const cell = document.createElement(i === 0 ? 'th' : 'td'); if (i === 0) cell.scope='row'; cell.textContent=text; tr.appendChild(cell); });
    body.appendChild(tr);
  }
  function render(announce) {
    sync();
    try {
      for (const el of [...form.elements, $('families')]) {
        if (el.willValidate && !el.validity.valid) throw new Error('Check “' + (el.labels?.[0]?.textContent || el.name) + '”: enter a value within its allowed range.');
      }
      const input = read(), r = math.calculate(input);
      const count = Number(input.families), cycles = Number(input.cycles);
      if (!Number.isInteger(count) || count < 0 || count > 10000 || ![1,12,26,52].includes(cycles)) throw new Error('Check the repeated-lateness inputs.');
      if (!Number.isSafeInteger(r.fee*count*cycles)) throw new Error('Reduce the rate, overdue days or repeated bill count; this projection is too large.');
      current = {input,r,count,cycles};
      $('calc-error').hidden=true;
      $('total').textContent = money(r.total);
      ['tuition','credit','balance','fee','total'].forEach(key => $('r-'+key).textContent = (key==='credit' ? '−' : '') + money(r[key]));
      $('timing-summary').textContent = r.days + ' days overdue · ' + r.grace + ' grace days · ' + r.billable + ' billable days';
      $('formula').textContent = explanation(input,r);
      $('selected-policy').textContent = 'Policy: ' + policy(input) + '.';
      $('cap-note').hidden = r.cap === null;
      $('cap-note').textContent = r.capped ? 'Fee cap applied: ' + money(r.rawFee) + ' reduced to ' + money(r.fee) + '.' : 'Maximum late fee for this bill: ' + money(r.cap) + '.';
      const timeline = $('timeline'); timeline.replaceChildren();
      [...new Set([0,1,3,7,14,30,r.days])].sort((a,b)=>a-b).forEach(days => { const next = math.calculate({...input,days}); row(timeline,[days + (days===r.days ? ' · selected' : ''),money(next.fee),money(next.total)],days===r.days); });
      const comparison = $('comparison'); comparison.replaceChildren();
      row(comparison,['Your current policy',explanation(input,r),money(r.fee)],true);
      const examples = [{name:'Example: flat',type:'flat',amount:'25'}, {name:'Example: daily',type:'daily',amount:'5'}, {name:'Example: weekly',type:'weekly',amount:'25'}, {name:'Example: percentage',type:'percent',amount:'5'}, {name:'Example: flat + daily',type:'flat_daily',amount:'25',dailyRate:'5'}];
      examples.forEach(e => { const value = {...input,...e}, result = math.calculate(value); row(comparison,[e.name,explanation(value,result),money(result.fee)],false); });
      $('delayed').textContent=money(r.balance*count); $('annual').textContent=money(r.fee*count*cycles);
      $('impact-assumptions').textContent=count+' late bill(s) × '+cycles+' cycle(s), each with '+money(r.balance)+' unpaid tuition and '+money(r.fee)+' in fees after '+r.days+' overdue day(s).';
      document.querySelectorAll('.result-action').forEach(b=>b.disabled=false);
      if (announce) $('result-announcement').textContent='Total remaining due '+money(r.total)+', including '+money(r.fee)+' in late fees.';
      return true;
    } catch (error) {
      current=null; $('calc-error').textContent=error.message; $('calc-error').hidden=false;
      ['total','r-tuition','r-credit','r-balance','r-fee','r-total','delayed','annual'].forEach(id=>$(id).textContent='—');
      ['timeline','comparison'].forEach(id=>$(id).replaceChildren());
      $('formula').textContent='Correct the input to see your estimate.'; $('selected-policy').textContent=''; $('timing-summary').textContent='Calculation unavailable'; $('impact-assumptions').textContent=''; $('cap-note').hidden=true;
      document.querySelectorAll('.result-action').forEach(b=>b.disabled=true);
      return false;
    }
  }
  function summary() {
    const {input,r,count,cycles}=current;
    return ['TuitionPing · Tuition late-fee estimate', mode()==='dates' ? 'Due: '+input.due+'; calculation date: '+input.asof : 'Overdue: '+r.days+' calendar days', 'Original tuition: '+money(r.tuition),'Prior payment / credit: '+money(r.credit),'Unpaid tuition: '+money(r.balance),'Grace period: '+r.grace+' calendar days','Billable days: '+r.billable,'Policy: '+policy(input),'Fee calculation: '+explanation(input,r), 'Fee cap: '+(r.cap===null?'None':money(r.cap)), 'Late fee: '+money(r.fee),'Total remaining due: '+money(r.total),'Repeated scenario: '+count+' late bills × '+cycles+' cycles; hypothetical fees '+money(r.fee*count*cycles), 'Assumes credit before fees accrue. Calendar days; no compounding. Planning estimate, not an invoice or a determination that a fee is permitted.', 'https://www.tuitionping.com/tools/late-fee-calculator'].join('\n');
  }
  async function copy(text,status) {
    const target=$(status);
    try {
      if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(text);
      else {
        const textarea=document.createElement('textarea'); textarea.value=text; document.body.appendChild(textarea); textarea.select();
        const ok=document.execCommand('copy'); textarea.remove(); if (!ok) throw new Error('Clipboard unavailable');
      }
      target.textContent='Copied.';
    } catch (_) { target.textContent='Clipboard unavailable. Select and copy the text below.'; const area=document.createElement('textarea'); area.value=text; area.readOnly=true; area.setAttribute('aria-label','Text to copy'); target.appendChild(area); area.focus(); area.select(); }
  }
  form.addEventListener('submit',e=>{e.preventDefault(); render(true);});
  form.addEventListener('input',()=>{ $('action-status').textContent=''; render(false); });
  form.addEventListener('change',()=>render(false));
  ['families','cycles'].forEach(id=>$(id).addEventListener('input',()=>render(false)));
  $('reset').addEventListener('click',()=>{form.reset();$('families').value='5';$('cycles').value='12'; setDateDefaults(); $('action-status').textContent=''; render(true);});
  $('load-example').addEventListener('click',()=>{
    form.querySelector('[name="timing"][value="days"]').checked=true;
    Object.entries({tuition:'1200',credit:'200',days:'7',grace:'2',type:'daily',amount:'5',cap:'20',dailyRate:'5'}).forEach(([id,value])=>$(id).value=value);
    render(true); form.scrollIntoView({behavior:window.matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth',block:'start'}); $('tuition').focus({preventScroll:true});
  });
  $('copy-summary').addEventListener('click',()=>{if(render(false)) copy(summary(),'action-status');});
  $('print').addEventListener('click',()=>{if(render(false)) window.print();});
  $('share').addEventListener('click',()=>{
    if (!render(false)) return;
    const url=new URL('https://www.tuitionping.com/tools/late-fee-calculator'); url.searchParams.set('timing',mode());
    fields.forEach(id=>{ if ((id==='days' && mode()==='dates') || (['due','asof'].includes(id) && mode()==='days')) return; url.searchParams.set(id,$(id).value); });
    copy(url.toString(),'action-status');
  });
  $('export').addEventListener('click',()=>{
    if (!render(false)) return;
    const {input,r,count,cycles}=current;
    const rows=[['TuitionPing tuition late-fee estimate','Value'],['Timing mode',mode()], ...(mode()==='dates'?[['Due date',input.due],['Calculation date',input.asof]]:[]), ['Original tuition USD',(r.tuition/100).toFixed(2)],['Prior credit USD',(r.credit/100).toFixed(2)],['Unpaid tuition USD',(r.balance/100).toFixed(2)],['Fee model',input.type],['Fee amount',input.amount],['Additional daily rate USD',input.type==='flat_daily'?input.dailyRate:'Not applicable'],['Days overdue',r.days],['Grace days',r.grace],['Billable days',r.billable],['Uncapped fee USD',(r.rawFee/100).toFixed(2)],['Fee cap USD',r.cap===null?'None':(r.cap/100).toFixed(2)],['Late fee USD',(r.fee/100).toFixed(2)],['Total remaining due USD',(r.total/100).toFixed(2)],['Late bills per cycle',count],['Cycles per year',cycles],['Hypothetical annual fees USD',(r.fee*count*cycles/100).toFixed(2)],['Assumptions','Credit before fees accrue; calendar days; no compounding. Estimate only.'],['Source','https://www.tuitionping.com/tools/late-fee-calculator']];
    const csv=rows.map(row=>row.map(v=>'"'+String(v).replaceAll('"','""')+'"').join(',')).join('\r\n');
    const url=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'})); const a=document.createElement('a'); a.href=url; a.download='tuitionping-late-fee-estimate.csv'; document.body.appendChild(a); a.click(); a.remove(); setTimeout(()=>URL.revokeObjectURL(url),1000); $('action-status').textContent='CSV downloaded.';
  });
  const page='https://www.tuitionping.com/tools/late-fee-calculator';
  $('copy-link').addEventListener('click',()=>copy(page,'link-status'));
  $('copy-html').addEventListener('click',()=>copy($('link-snippet').value,'link-status'));
  setDateDefaults();
  const params=new URLSearchParams(window.location.search);
  if (['days','dates'].includes(params.get('timing'))) form.querySelector('[name="timing"][value="'+params.get('timing')+'"]').checked=true;
  fields.forEach(id=>{
    if (!params.has(id)) return;
    const el=$(id), value=params.get(id);
    if (el.tagName==='SELECT' && ![...el.options].some(o=>o.value===value)) return;
    if (el.type==='number' && value!=='' && !/^\d+(\.\d{1,2})?$/.test(value)) { el.value=''; return; }
    el.value=value;
  });
  render(false);
})();
