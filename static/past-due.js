(function () {
  'use strict';
  const $ = name => document.getElementById('notice-' + name), form = $('form');
  if (!form) return;
  const MAX = 100000000; // $1,000,000; monetary arithmetic uses integer cents.
  const money = n => new Intl.NumberFormat('en-US', {style:'currency', currency:'USD'}).format(n === 0 ? 0 : n / 100);
  let sample = false, generated = false, letter = '', revision = 0;
  function amount(name, label) {
    const value = $(name).value.trim();
    if (!/^\d{1,9}(\.\d{1,2})?$/.test(value)) throw Error(label + ': use a nonnegative dollar amount with at most two decimals.');
    const parts = value.split('.'), n = Number(parts[0]) * 100 + Number((parts[1] || '').padEnd(2, '0'));
    if (!Number.isSafeInteger(n) || n > MAX) throw Error(label + ': amount is too large.');
    return n;
  }
  function date(name, label) {
    const value = $(name).value, d = new Date(value + 'T12:00:00Z');
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || isNaN(d) || d.toISOString().slice(0,10) !== value) throw Error('Choose a valid ' + label + '.');
    return {value, label:d.toLocaleDateString('en-US', {year:'numeric', month:'long', day:'numeric', timeZone:'UTC'})};
  }
  function text(name, label) {
    const input = $(name), value = input.value.trim();
    if (!value || value.length > input.maxLength) throw Error('Check ' + label + '.');
    return value;
  }
  function clear() {
    revision++; generated = false; letter = '';
    $('document').hidden = true; $('document').replaceChildren();
    $('print').disabled = true; $('copy').disabled = true; $('empty').hidden = false;
    $('status').textContent = 'Create a letter after making changes.';
    if (window.tpHideResourceNextStep) window.tpHideResourceNextStep();
  }
  function node(tag, value, cls) {
    const el = document.createElement(tag); if (value !== undefined) el.textContent = value;
    if (cls) el.className = cls; return el;
  }
  form.addEventListener('input', clear); form.addEventListener('change', clear);
  form.addEventListener('submit', function (event) {
    event.preventDefault(); clear(); $('error').hidden = true;
    try {
      const program = text('program', 'program name'), family = text('family', 'family label');
      const contact = text('contact', 'program contact'), instructions = text('instructions', 'payment instructions');
      const issued = date('issued', 'letter date'), due = date('due', 'original due date');
      const start = date('start', 'period start'), end = date('end', 'period end'), response = date('response', 'response date');
      if (start.value > end.value) throw Error('The billing period must end on or after its start.');
      if (due.value >= issued.value) throw Error('Use an original due date before the letter date for a past-due notice.');
      if (response.value < issued.value) throw Error('Choose a response date on or after the letter date.');
      const tuition = amount('tuition', 'Tuition'), fees = amount('fees', 'Fees');
      const credits = amount('credits', 'Credits'), payments = amount('payments', 'Verified payments');
      const total = tuition + fees, balance = total - credits - payments;
      if (total > MAX) throw Error('Total charges must not exceed $1,000,000.');
      if (balance <= 0) throw Error('There is no outstanding balance. Check your credits and verified payments; do not send a past-due notice for a paid bill.');
      if (!['friendly', 'firm'].includes($('tone').value)) throw Error('Choose a letter tone.');
      const paragraphs = [program, 'Past-due tuition notice', issued.label, 'Dear ' + family + ',',
        $('tone').value === 'firm' ? 'Our records still show an outstanding tuition balance. Please review the account summary below.' : 'This is a friendly reminder that our records show an outstanding tuition balance. Thank you for taking a moment to review the details.',
        'Billing period: ' + start.label + ' – ' + end.label + '\nOriginal due date: ' + due.label];
      const rows = [['Tuition charges', tuition], ['Fees already assessed', fees], ['Approved credits', -credits], ['Verified payments received', -payments], ['Outstanding balance', balance]];
      const closing = ['Please pay the remaining ' + money(balance) + ' or contact us by ' + response.label + ' to discuss the balance.',
        'Payment instructions:\n' + instructions,
        'If you have already sent payment or believe this balance is incorrect, please contact us so we can check our records before any further follow-up.',
        'Questions or payment update: ' + contact, 'Thank you,\n' + program,
        'This notice requests payment; it is not a receipt or proof of payment.'];
      const doc = $('document');
      if (sample) doc.append(node('p', 'FICTIONAL EXAMPLE — replace all sample details before sending.', 'notice-sample'));
      paragraphs.forEach((p, i) => doc.append(node(i === 1 ? 'h2' : 'p', p)));
      const totals = node('div', undefined, 'notice-totals');
      rows.forEach(([label, n], i) => { const p = node('p', undefined, i === 4 ? 'notice-balance' : ''); p.append(node('span', label), node('span', money(n))); totals.append(p); });
      doc.append(totals); closing.forEach(p => doc.append(node('p', p)));
      letter = (sample ? 'FICTIONAL EXAMPLE — replace all sample details before sending.\n\n' : '') + [...paragraphs, ...rows.map(([label, n]) => label + ': ' + money(n)), ...closing].join('\n\n');
      generated = true; doc.hidden = false; $('empty').hidden = true; $('print').disabled = false; $('copy').disabled = false;
      $('status').textContent = 'Letter ready. Review the balance and dates before copying or printing.';
      try { if (window.tpTrack) window.tpTrack('notice_created'); } catch (_) {}
      if (window.tpResourceNextStep) window.tpResourceNextStep('notice');
    } catch (error) { $('error').textContent = error.message; $('error').hidden = false; }
  });
  $('print').addEventListener('click', () => { if (generated) window.print(); });
  $('copy').addEventListener('click', async () => {
    if (!generated) return;
    const current = revision;
    try {
      await navigator.clipboard.writeText(letter);
      if (generated && revision === current) $('status').textContent = 'Letter copied. Review before sending privately.';
    } catch (_) {
      if (!generated || revision !== current) return;
      const range = document.createRange(); range.selectNodeContents($('document'));
      const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
      $('status').textContent = 'Automatic copy is unavailable. The letter is selected; use your browser’s Copy command.';
    }
  });
  $('reset').addEventListener('click', () => { form.reset(); sample = false; clear(); $('error').hidden = true; });
  $('example').addEventListener('click', () => {
    clear(); sample = true; $('error').hidden = true;
    Object.entries({program:'Sunshine Daycare (fictional)', family:'Example Family', contact:'billing@example.com', issued:'2026-10-10', due:'2026-10-05', start:'2026-10-01', end:'2026-10-31', tuition:'800.00', fees:'25.00', credits:'25.00', payments:'200.00', response:'2026-10-15', instructions:'Fictional example: use your usual agreed payment method and include the billing period.', tone:'friendly'}).forEach(([key, value]) => $(key).value = value);
  });
}());
