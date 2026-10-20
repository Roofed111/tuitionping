/* DOM tests; run with jsdom installed separately as described in tests/README.md. */
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {readFileSync}=require('node:fs');
const {join}=require('node:path');
const {JSDOM}=require('jsdom');
const root=join(__dirname,'..');
function setup(query='') {
  const dom=new JSDOM(readFileSync(join(root,'templates/tool_late_fee_calc.html'),'utf8'), {url:'https://www.tuitionping.com/tools/late-fee-calculator'+query,runScripts:'outside-only'});
  const {window:w}=dom;
  w.matchMedia=()=>({matches:true});
  w.HTMLElement.prototype.scrollIntoView=()=>{};
  w.eval(readFileSync(join(root,'static/late-fee-math.js'),'utf8'));
  w.eval(readFileSync(join(root,'static/late-fee-calculator.js'),'utf8'));
  const $=id=>w.document.getElementById(id);
  const set=(id,value)=>{ $(id).value=value; $(id).dispatchEvent(new w.Event('input',{bubbles:true})); };
  return {dom,w,$,set};
}
test('initial state, comparisons, and worked example',()=>{
  const {dom,$}=setup();
  assert.equal($('total').textContent,'$1,225.00');
  assert.equal($('comparison').children.length,6);
  assert.equal($('timeline').children.length,6);
  $('load-example').click();
  assert.equal($('total').textContent,'$1,020.00');
  assert.equal($('r-fee').textContent,'$20.00');
  assert.match($('cap-note').textContent,/reduced/);
  assert.equal($('annual').textContent,'$1,200.00');
  $('reset').click(); assert.equal($('total').textContent,'$1,225.00');
  dom.window.close();
});
test('changing models enables only relevant controls',()=>{
  const {dom,$,set}=setup();
  set('type','flat_daily'); assert.equal($('dailyRate').disabled,false);
  assert.equal($('total').textContent,'$1,260.00');
  set('type','percent'); set('amount','2.55');
  assert.equal($('dailyRate').disabled,true); assert.equal($('total').textContent,'$1,230.60');
  set('type','weekly'); set('amount','25'); set('days','8');
  assert.equal($('r-fee').textContent,'$50.00');
  dom.window.close();
});
test('invalid entries clear stale results and block result actions',()=>{
  const {dom,$,set}=setup();
  set('credit','1201'); assert.equal($('calc-error').hidden,false);
  assert.equal($('total').textContent,'—'); assert.equal($('print').disabled,true);
  set('credit','0'); assert.equal($('calc-error').hidden,true);
  set('days','1.5'); assert.equal($('total').textContent,'—');
  set('days','7'); set('tuition',''); assert.equal($('total').textContent,'—');
  set('tuition','1200'); set('cap','0'); assert.equal($('r-fee').textContent,'$0.00');
  set('families',''); assert.equal($('total').textContent,'—');
  dom.window.close();
});
test('date mode excludes inactive day input and handles early payment',()=>{
  const {dom,w,$,set}=setup();
  const radio=w.document.querySelector('[name="timing"][value="dates"]'); radio.checked=true; radio.dispatchEvent(new w.Event('change',{bubbles:true}));
  assert.equal($('days').disabled,true); assert.equal($('due').disabled,false);
  set('due','2026-03-07'); set('asof','2026-03-09');
  assert.match($('timing-summary').textContent,/^2 days/);
  set('asof','2026-03-06'); assert.equal($('r-fee').textContent,'$0.00');
  set('asof',''); assert.equal($('total').textContent,'—');
  dom.window.close();
});
test('shared URL hydrates cents, cap, credit, timing and projection',()=>{
  const {dom,$}=setup('?timing=dates&tuition=1200&credit=200&due=2026-10-01&asof=2026-10-08&type=daily&amount=5&grace=2&cap=20&families=3&cycles=52');
  assert.equal($('total').textContent,'$1,020.00');
  assert.equal($('annual').textContent,'$3,120.00');
  dom.window.close();
});
test('hostile or malformed query input is not HTML and cannot produce NaN',()=>{
  const {dom,w,$}=setup('?tuition=%3Cscript%3Ealert(1)%3C%2Fscript%3E&timing=evil&type=evil');
  assert.equal($('total').textContent,'—'); assert.equal(w.document.querySelectorAll('script').length,3);
  assert.doesNotMatch(w.document.body.textContent,/NaN|Infinity/);
  dom.window.close();
});
test('copy summary, calculation share, page link and CSV preserve values',async()=>{
  const {dom,w,$}=setup();
  let copied='',printed=false,downloaded='',csvBlob;
  Object.defineProperty(w.navigator,'clipboard',{value:{writeText:async text=>{copied=text;}}});
  w.print=()=>{printed=true;};
  w.URL.createObjectURL=blob=>{csvBlob=blob;return 'blob:test';}; w.URL.revokeObjectURL=()=>{};
  w.HTMLAnchorElement.prototype.click=function(){downloaded=this.download;};
  $('load-example').click(); $('copy-summary').click(); await Promise.resolve();
  assert.match(copied,/Total remaining due: \$1,020.00/); assert.match(copied,/Policy: \$5.00 per billable day/);
  $('share').click(); await Promise.resolve();
  const url=new URL(copied); assert.equal(url.searchParams.get('credit'),'200'); assert.equal(url.searchParams.get('cap'),'20');
  const restored=setup(url.search); assert.equal(restored.$('total').textContent,'$1,020.00'); restored.dom.window.close();
  $('copy-link').click(); await Promise.resolve(); assert.equal(copied,'https://www.tuitionping.com/tools/late-fee-calculator');
  $('copy-html').click(); await Promise.resolve(); assert.match(copied,/^<a href=/);
  $('print').click(); assert.equal(printed,true);
  $('export').click(); assert.equal(downloaded,'tuitionping-late-fee-estimate.csv'); assert.equal(csvBlob.type,'text/csv;charset=utf-8');
  const reader=new w.FileReader(); const csv=await new Promise(resolve=>{reader.onload=()=>resolve(reader.result);reader.readAsText(csvBlob);});
  assert.match(csv,/"Total remaining due USD","1020.00"/);
  dom.window.close();
});
