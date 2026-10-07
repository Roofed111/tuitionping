(function () {
  'use strict';
  const $ = id => document.getElementById(id);
  const form = $('invoice-form');
  const money = cents => new Intl.NumberFormat('en-US', {style:'currency',currency:'USD'}).format(cents === 0 ? 0 : cents / 100);
  const MAX = 100000000; // $1,000,000 total, with all arithmetic in integer cents.
  let sample = false, generated = false;
  function cents(value, label) {
    if (!/^\d{1,9}(\.\d{1,2})?$/.test(String(value).trim())) throw new Error(label + ': enter a nonnegative dollar amount with up to two decimals.');
    const parts = String(value).trim().split('.');
    const n = Number(parts[0]) * 100 + Number((parts[1] || '').padEnd(2, '0'));
    if (!Number.isSafeInteger(n) || n > MAX) throw new Error(label + ': amount is too large.');
    return n;
  }
  function date(value, label) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) throw new Error('Choose ' + label + '.');
    const d = new Date(value + 'T12:00:00Z');
    if (isNaN(d) || d.toISOString().slice(0,10) !== value) throw new Error('Choose a valid ' + label + '.');
    return d.toLocaleDateString('en-US', {year:'numeric',month:'short',day:'numeric',timeZone:'UTC'});
  }
  function text(id, label, required) {
    const el = $(id), value = el.value.trim();
    if ((required && !value) || value.length > el.maxLength && el.maxLength > 0) throw new Error('Check ' + label + '.');
    return value;
  }
  function clearOutput() {
    generated = false; $('invoice-document').hidden = true; $('invoice-document').replaceChildren();
    $('invoice-print').disabled = true; $('invoice-empty').hidden = false;
    $('invoice-status').textContent = 'Create a document after making changes.';
  }
  function el(tag, value, className) {
    const node = document.createElement(tag); if (value !== undefined) node.textContent = value;
    if (className) node.className = className; return node;
  }
  let rowId = 0;
  function addLine(description='', qty='1', price='') {
    const box = $('line-items'); if (box.children.length >= 12) return;
    const row = el('div',undefined,'invoice-line'); rowId++;
    [['Description','description',description,'text'],['Quantity','quantity',qty,'number'],['Unit price ($)','price',price,'text']].forEach(([label,key,value,type]) => {
      const wrap = el('label', label); if (key === 'description') wrap.className='invoice-description';
      const input=el('input'); input.dataset.field=key; input.id='charge-'+rowId+'-'+key; input.value=value; input.type=type; input.required=true;
      if(key==='description') input.maxLength=180;
      if(key==='quantity'){input.min='1';input.max='10000';input.step='1';}
      if(key==='price') input.inputMode='decimal'; wrap.append(input); row.append(wrap);
    });
    const remove=el('button','Remove','btn btn-ghost btn-sm invoice-remove');remove.type='button'; remove.setAttribute('aria-label','Remove charge '+rowId);
    remove.addEventListener('click',()=>{if(box.children.length > 1){row.remove();clearOutput();syncRows();}});row.append(remove);box.append(row);syncRows();
  }
  function syncRows(){const count=$('line-items').children.length;$('add-item').disabled=count>=12;document.querySelectorAll('.invoice-remove').forEach(b=>b.disabled=count===1);}
  function typeChanged(){const receipt=$('document-type').value==='receipt';$('receipt-fields').hidden=!receipt;$('due-field').hidden=receipt;$('due').required=!receipt;['received','received-date','method','verified'].forEach(id=>$(id).required=receipt);clearOutput();}
  function meta(box, label, value){const p=el('p');p.append(el('strong',label),document.createTextNode(value));box.append(p);}
  function total(box,label,value,balance){const p=el('p',undefined,balance?'invoice-balance':'');p.append(el('span',label),el('span',money(value)));box.append(p);}
  form.addEventListener('submit', event => {
    event.preventDefault();clearOutput();$('invoice-error').hidden=true;
    try {
      const receipt=$('document-type').value==='receipt';
      const program=text('program','program name',true),family=text('family','family label',true),number=text('document-number','document number',true);
      const issued=date($('issued').value,'issue date'),start=date($('period-start').value,'period start'),end=date($('period-end').value,'period end');
      if($('period-start').value > $('period-end').value) throw new Error('The billing period must end on or after its start.');
      let due='',receivedDate='';
      if(receipt){receivedDate=date($('received-date').value,'payment date');if(!$('verified').checked) throw new Error('Verify that the payment was received before creating a receipt.');}
      else{due=date($('due').value,'due date');if($('due').value < $('issued').value)throw new Error('The due date must be on or after the issue date.');}
      const lines=Array.from($('line-items').children).map(row=>{
        const description=row.querySelector('[data-field="description"]').value.trim();
        const q=row.querySelector('[data-field="quantity"]').value;
        if(!description || description.length>180 || !/^\d{1,5}$/.test(q) || Number(q)<1 || Number(q)>10000)throw new Error('Each charge needs a description and a whole quantity from 1 to 10,000.');
        const unit=cents(row.querySelector('[data-field="price"]').value,'Unit price'),amount=unit*Number(q);
        if(amount>MAX)throw new Error('A charge exceeds the $1,000,000 limit.');
        return {description,quantity:Number(q),unit,amount};
      });
      const subtotal=lines.reduce((n,line)=>n+line.amount,0),credits=cents($('credits').value,'Credits'),previous=cents($('previous').value,'Previous payments');
      if(subtotal>MAX)throw new Error('Total charges exceed the $1,000,000 limit.');
      if(credits>subtotal)throw new Error('Credits cannot exceed the charges on this document.');
      const bill=subtotal-credits,received=receipt?cents($('received').value,'Payment received'):0;
      if(receipt && received<=0)throw new Error('A receipt needs an amount received greater than zero.');
      if(previous+received>bill)throw new Error('Verified payments exceed this bill. Check the amount or record an overpayment separately.');
      const method=receipt?text('method','payment method / reference',true):'',note=text('note','note',false),contact=text('contact','contact',false);
      const doc=$('invoice-document'),head=el('div',undefined,'invoice-doc-head'),left=el('div'),right=el('div');
      left.append(el('h2',receipt?'Payment receipt':'Tuition invoice'),el('p',program));if(contact)left.append(el('p',contact));
      if(sample)left.append(el('p','Fictional example — not a real bill or payment','invoice-sample'));
      right.append(el('p',number),el('p','Issued '+issued));head.append(left,right);doc.append(head);
      const metadata=el('div',undefined,'invoice-doc-meta');meta(metadata,'Family / account',family);meta(metadata,'Service period',start+' – '+end);
      meta(metadata,receipt?'Payment received on':'Payment due',receipt?receivedDate:due);if(receipt)meta(metadata,'Method / reference',method);doc.append(metadata);
      const table=el('table',undefined,'invoice-doc-table'),thead=el('thead'),tr=el('tr');['Description','Qty','Rate','Amount'].forEach(h=>{const th=el('th',h);th.scope='col';tr.append(th);});thead.append(tr);table.append(thead);
      const tbody=el('tbody');lines.forEach(line=>{const r=el('tr');[line.description,String(line.quantity),money(line.unit),money(line.amount)].forEach(v=>r.append(el('td',v)));tbody.append(r);});table.append(tbody);doc.append(table);
      const totals=el('div',undefined,'invoice-totals');total(totals,'Charges',subtotal);total(totals,'Credits',-credits);total(totals,'Bill total',bill);total(totals,'Previous verified payments',previous);if(receipt)total(totals,'Received this time',received);total(totals,'Remaining balance',bill-previous-received,true);doc.append(totals);
      if(note)doc.append(el('p',note,'invoice-doc-note'));
      doc.append(el('p',receipt?'Payment recorded as verified by the provider. This document is not independent bank confirmation.':'Payment requested; this invoice does not establish that money was received.','invoice-doc-footer'));
      doc.append(el('p','Created with the free TuitionPing document generator · tuitionping.com','invoice-doc-footer'));
      doc.hidden=false;$('invoice-empty').hidden=true;$('invoice-print').disabled=false;generated=true;$('invoice-status').textContent='Document ready. Print or choose Save as PDF.';
      if(window.tpTrack)window.tpTrack('document_created',receipt?'receipt':'invoice');
    } catch(error){$('invoice-error').textContent=error.message;$('invoice-error').hidden=false;}
  });
  form.addEventListener('input',clearOutput);form.addEventListener('change',clearOutput);
  $('document-type').addEventListener('change',typeChanged);
  $('add-item').addEventListener('click',()=>{addLine();clearOutput();});
  $('invoice-print').addEventListener('click',()=>{if(generated)window.print();});
  $('invoice-reset').addEventListener('click',()=>{form.reset();sample=false;$('line-items').replaceChildren();addLine();typeChanged();$('invoice-error').hidden=true;});
  $('invoice-example').addEventListener('click',()=>{
    form.reset();sample=true;$('document-type').value='invoice';
    Object.entries({program:'Fictional Sunny Sprouts',family:'Example account A',contact:'Fictional example only', 'document-number':'EXAMPLE-001',issued:'2026-10-01','period-start':'2026-10-01','period-end':'2026-10-31',due:'2026-10-05',credits:'25.00',previous:'200.00',received:'300.00','received-date':'2026-10-06',method:'Example payment reference',note:'Fictional amounts. Replace these details before using a real document.'}).forEach(([id,value])=>$(id).value=value);
    $('line-items').replaceChildren();addLine('Monthly tuition','1','800.00');addLine('Fee already assessed by provider','1','25.00');typeChanged();$('invoice-error').hidden=true;
  });
  addLine('Tuition');typeChanged();
}());
