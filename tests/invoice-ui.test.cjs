const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {JSDOM}=require('jsdom');
function setup(){
 const dom=new JSDOM(fs.readFileSync('templates/tool_invoice.html','utf8'),{url:'https://www.tuitionping.com/tools/daycare-invoice-receipt',runScripts:'outside-only'});
 const w=dom.window,$=id=>w.document.getElementById(id),events=[];
 w.tpTrack=(...args)=>events.push(args);
 // Any network call or storage write from the document tool is a failure.
 w.fetch=()=>{throw new Error('Document fields must not be transmitted');};
 w.Storage.prototype.setItem=()=>{throw new Error('Document fields must not be persisted');};
 w.eval(fs.readFileSync('static/invoice.js','utf8'));
 const set=(id,value)=>{$(id).value=value;$(id).dispatchEvent(new w.Event('input',{bubbles:true}));};
 const submit=()=>{$('invoice-form').dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));};
 return {dom,w,$,set,submit,events};
}
test('initial state and example invoice use exact cents and retain example label',()=>{
 const {dom,$,submit,events}=setup();assert.equal($('invoice-print').disabled,true);
 $('invoice-example').click();submit();
 assert.equal($('invoice-error').hidden,true);assert.equal($('invoice-document').hidden,false);
 assert.match($('invoice-document').textContent,/Remaining balance\$600.00/);
 assert.match($('invoice-document').textContent,/Fictional example/);
 assert.deepEqual(events,[['document_created','invoice']]);dom.window.close();
});
test('partial-payment receipt needs verification and records only this payment',()=>{
 const {dom,w,$,set,submit,events}=setup();$('invoice-example').click();set('document-type','receipt');$('document-type').dispatchEvent(new w.Event('change'));
 submit();assert.match($('invoice-error').textContent,/Verify/);assert.equal($('invoice-print').disabled,true);
 $('verified').checked=true;submit();assert.equal($('invoice-error').hidden,true);
 assert.match($('invoice-document').textContent,/Received this time\$300.00/);assert.match($('invoice-document').textContent,/Remaining balance\$300.00/);
 assert.equal(events.at(-1)[1],'receipt');dom.window.close();
});
test('stale results disappear on input, reset, type and added/removed rows',()=>{
 const {dom,$,set,submit}=setup();$('invoice-example').click();submit();
 set('credits','10.00');assert.equal($('invoice-document').hidden,true);assert.equal($('invoice-print').disabled,true);assert.equal($('invoice-document').children.length,0);
 submit();assert.match($('invoice-document').textContent,/Remaining balance\$615.00/);
 $('add-item').click();assert.equal($('invoice-document').hidden,true);
 $('line-items').lastElementChild.querySelector('button').click();submit();assert.equal($('invoice-document').hidden,false);
 $('invoice-reset').click();assert.equal($('program').value,'');assert.equal($('line-items').children.length,1);assert.equal($('invoice-print').disabled,true);dom.window.close();
});
test('reject invalid money, overcredits, overpayments, zero receipts and reversed dates',()=>{
 const {dom,w,$,set,submit}=setup();
 for(const [field,value] of [['credits','900'],['credits','-1'],['credits','1.001'],['credits','NaN'],['credits','Infinity'],['previous','900'],['period-end','2026-09-30'],['due','2026-09-30']]){
  $('invoice-example').click();set(field,value);submit();assert.equal($('invoice-document').hidden,true,field+' '+value);assert.equal($('invoice-print').disabled,true);assert.equal($('invoice-error').hidden,false);
 }
 $('invoice-example').click();set('document-type','receipt');$('document-type').dispatchEvent(new w.Event('change'));$('verified').checked=true;set('received','0');submit();assert.match($('invoice-error').textContent,/greater than zero/);
 set('received','601');submit();assert.match($('invoice-error').textContent,/exceed/);dom.window.close();
});
test('integer quantities and cents calculations have no floating point artifacts',()=>{
 const {dom,w,$,submit}=setup();$('invoice-example').click();const row=$('line-items').children[0];
 row.querySelector('[data-field="quantity"]').value='3';row.querySelector('[data-field="price"]').value='0.10';
 $('line-items').children[1].querySelector('button').click();$('credits').value='0';$('previous').value='0';submit();assert.match($('invoice-document').textContent,/Remaining balance\$0.30/);
 row.querySelector('[data-field="quantity"]').value='1.5';submit();assert.equal($('invoice-document').hidden,true);assert.match($('invoice-error').textContent,/whole quantity/);
 row.querySelector('[data-field="quantity"]').value='10000';row.querySelector('[data-field="price"]').value='1000000';submit();assert.equal($('invoice-document').hidden,true);dom.window.close();
});
test('text is escaped, sensitive data never goes into URL or analytics, print works',()=>{
 const {dom,w,$,set,submit,events}=setup();$('invoice-example').click();
 const hostile='<img src=x onerror=alert(1)>';set('family',hostile);set('note','Private payment reference ABC-123');submit();
 assert.equal($('invoice-document').querySelector('img'),null);assert.match($('invoice-document').textContent,/<img src=x/);
 assert.equal(w.location.href,'https://www.tuitionping.com/tools/daycare-invoice-receipt');assert.deepEqual(events,[['document_created','invoice']]);
 let printed=0;w.print=()=>printed++;$('invoice-print').click();assert.equal(printed,1);set('program','Changed');$('invoice-print').click();assert.equal(printed,1);dom.window.close();
});
test('line count bounded and last row cannot be removed',()=>{
 const {dom,$}=setup();for(let i=0;i<20;i++)$('add-item').click();assert.equal($('line-items').children.length,12);assert.equal($('add-item').disabled,true);
 while($('line-items').children.length>1)$('line-items').lastElementChild.querySelector('button').click();assert.equal($('line-items').querySelector('button').disabled,true);dom.window.close();
});
test('real documents can be created from blank entries without example watermark',()=>{
 const {dom,$,set,submit}=setup();Object.entries({program:'Sample daycare',family:'Private account', 'document-number':'INV-001',issued:'2026-10-01','period-start':'2026-10-01','period-end':'2026-10-31',due:'2026-10-05'}).forEach(([id,value])=>set(id,value));
 $('line-items').querySelector('[data-field="price"]').value='100.00';submit();assert.equal($('invoice-error').hidden,true);assert.doesNotMatch($('invoice-document').textContent,/Fictional example/);assert.match($('invoice-document').textContent,/Remaining balance\$100.00/);dom.window.close();
});
