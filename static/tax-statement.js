(function () {
  'use strict';
  const $ = name => document.getElementById('stmt-' + name), form = $('form');
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
  function date(name, label, required) {
    const value = $(name).value;
    if (!value) { if (required) throw Error('Choose a valid ' + label + '.'); return null; }
    const d = new Date(value + 'T12:00:00Z');
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || isNaN(d) || d.toISOString().slice(0,10) !== value) throw Error('Choose a valid ' + label + '.');
    return {value, label: d.toLocaleDateString('en-US', {year:'numeric', month:'long', day:'numeric', timeZone:'UTC'})};
  }
  function text(name, label, required) {
    const input = $(name), value = input.value.trim();
    if (!value) { if (required) throw Error('Check ' + label + '.'); return ''; }
    if (value.length > input.maxLength) throw Error('Check ' + label + '.');
    return value;
  }
  function clear() {
    revision++; generated = false; letter = '';
    $('document').hidden = true; $('document').replaceChildren();
    $('print').disabled = true; $('copy').disabled = true; $('empty').hidden = false;
    $('status').textContent = 'Create a statement after making changes.';
  }
  function node(tag, value, cls) {
    const el = document.createElement(tag); if (value !== undefined) el.textContent = value;
    if (cls) el.className = cls; return el;
  }
  form.addEventListener('input', clear); form.addEventListener('change', clear);
  form.addEventListener('submit', function (event) {
    event.preventDefault(); clear(); $('error').hidden = true;
    try {
      const program = text('program', 'program name', true), address = text('address', 'program address', true);
      const tin = text('tin', 'EIN / tax ID', false), contact = text('contact', 'program contact', false);
      const parent = text('parent', 'parent name', true), child = text('child', 'child name', true);
      const yearValue = $('year').value.trim();
      if (!/^\d{4}$/.test(yearValue)) throw Error('Enter the four-digit tax year, for example 2025.');
      const year = Number(yearValue);
      if (year < 1990 || year > 2100) throw Error('Enter a tax year between 1990 and 2100.');
      const total = amount('total', 'Total paid');
      if (total <= 0) throw Error('Enter the total the family paid during the tax year; it must be more than $0.');
      const start = date('start', 'care period start', false), end = date('end', 'care period end', false);
      if ((start === null) !== (end === null)) throw Error('Enter both care period dates, or leave both blank.');
      if (start && start.value > end.value) throw Error('The care period must end on or after its start.');
      const issued = date('issued', 'date issued', true);
      const rows = [['Child who received care', child], ['Parent or guardian', parent]];
      if (start) rows.push(['Care period', start.label + ' – ' + end.label]);
      if (tin) rows.push(['Provider tax ID (EIN)', tin]);
      rows.push(['Total paid for care in ' + year, total]);
      const header = [program, address + (contact ? '\nQuestions: ' + contact : '')];
      const closing = ['This statement reports tuition and care payments recorded by ' + program + ' for the calendar year ' + year + '. Parents: your tax preparer may use it for the child and dependent care credit (IRS Form 2441) or dependent care FSA records. Keep your own receipts as well.',
        'This statement is not tax advice and is not a receipt for any single payment.',
        'Date issued: ' + issued.label, 'Prepared by: ______________________________\n' + program];
      const doc = $('document');
      if (sample) doc.append(node('p', 'FICTIONAL EXAMPLE — replace all sample details before sending.', 'stmt-sample'));
      header.forEach(p => doc.append(node('p', p)));
      doc.append(node('h2', 'Child care payment statement — tax year ' + year));
      const rowsBox = node('div', undefined, 'stmt-rows');
      rows.forEach(([label, n]) => {
        const isTotal = label.startsWith('Total paid');
        const p = node('p', undefined, isTotal ? 'stmt-total-line' : '');
        p.append(node('span', label), node('span', isTotal ? money(n) : n));
        rowsBox.append(p);
      });
      doc.append(rowsBox);
      closing.slice(0, 2).forEach(p => doc.append(node('p', p)));
      doc.append(node('p', closing[2]));
      doc.append(node('p', closing[3], 'stmt-sign'));
      const copyRows = rows.map(([label, n]) => label + ': ' + (label.startsWith('Total paid') ? money(n) : n));
      letter = (sample ? 'FICTIONAL EXAMPLE — replace all sample details before sending.\n\n' : '') +
        [...header, 'Child care payment statement — tax year ' + year, ...copyRows, ...closing].join('\n\n');
      generated = true; doc.hidden = false; $('empty').hidden = true; $('print').disabled = false; $('copy').disabled = false;
      $('status').textContent = 'Statement ready. Review the total and tax year against your records before printing or copying.';
      try { if (window.tpTrack) window.tpTrack('statement_created'); } catch (_) {}
    } catch (error) { $('error').textContent = error.message; $('error').hidden = false; }
  });
  $('print').addEventListener('click', () => { if (generated) window.print(); });
  $('copy').addEventListener('click', async () => {
    if (!generated) return;
    const current = revision;
    try {
      await navigator.clipboard.writeText(letter);
      if (generated && revision === current) $('status').textContent = 'Statement copied. Review before sending privately.';
    } catch (_) {
      if (!generated || revision !== current) return;
      const range = document.createRange(); range.selectNodeContents($('document'));
      const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
      $('status').textContent = 'Automatic copy is unavailable. The statement is selected; use your browser’s Copy command.';
    }
  });
  $('reset').addEventListener('click', () => { form.reset(); sample = false; clear(); $('error').hidden = true; });
  $('example').addEventListener('click', () => {
    clear(); sample = true; $('error').hidden = true;
    Object.entries({program:'Sunshine Daycare (fictional)', address:'123 Example Street, Concord, CA 94520', tin:'12-3456789 (fictional)', contact:'billing@example.com', parent:'Example Parent', child:'Example Child', year:'2025', total:'11400.00', start:'2025-01-01', end:'2025-12-31', issued:'2026-01-12'}).forEach(([key, value]) => $(key).value = value);
  });
}());
