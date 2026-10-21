const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {execFileSync} = require('node:child_process');
const {JSDOM} = require('jsdom');
const panel = execFileSync('python3', ['-c', "from jinja2 import Environment, FileSystemLoader; print(Environment(loader=FileSystemLoader('templates'),autoescape=True).get_template('resource_next_step.html').render(next_step='notice',plans={'micro':{'price':9,'families':10}},trial_days=30))"], {encoding:'utf8'});
function setup() {
  const html = fs.readFileSync('templates/tool_past_due.html', 'utf8').replace(/{% with next_step='notice' %}{% include "resource_next_step.html" %}{% endwith %}/, panel);
  const dom = new JSDOM(html, {url:'https://www.tuitionping.com/tools/daycare-past-due-letter',runScripts:'outside-only'});
  const w = dom.window, $ = id => w.document.getElementById('notice-' + id), events = [];
  w.tpTrack = (...args) => events.push(args);
  w.fetch = () => {throw Error('Entries must stay in this tab');};
  for (const key of ['localStorage', 'sessionStorage']) Object.defineProperty(w,key,{get() {throw Error('No persistence');}});
  w.eval(fs.readFileSync('static/resource-next-step.js','utf8'));
  w.eval(fs.readFileSync('static/past-due.js','utf8'));
  const submit = () => $('form').dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));
  const set = (key,value) => {$(key).value=value;$(key).dispatchEvent(new w.Event('input',{bubbles:true}));};
  return {dom,w,$,events,submit,set,prompt:w.document.getElementById('resource-next-step')};
}
test('example generates exact remaining balance, request not receipt, and fixed-label next step', () => {
  const p=setup();assert.equal(p.$('print').disabled,true);p.$('example').click();assert.deepEqual(p.events,[]);
  p.submit();const text=p.$('document').textContent;
  assert.match(text,/FICTIONAL EXAMPLE/);assert.match(text,/Outstanding balance\$600.00/);
  assert.match(text,/not a receipt/);assert.match(text,/October 5, 2026/);
  assert.equal(p.prompt.hidden,false);assert.equal(p.$('error').hidden,true);
  assert.deepEqual(p.events,[['notice_created'],['resource_next_step','notice']]);
  p.submit();assert.equal(p.events.filter(e=>e[0]==='resource_next_step').length,1);
  p.w.document.addEventListener('click',e=>e.preventDefault());
  p.prompt.querySelector('[data-resource-action="demo"]').click();
  assert.deepEqual(p.events.at(-1),['resource_demo_click','notice']);p.w.close();
});
test('invalid amounts, paid bills, overpayments and invalid date order cannot create notices', () => {
  const p=setup();
  for (const [key,value] of [['tuition','NaN'],['fees','-1'],['credits','1.001'],['tuition','1000001'],['tuition','1e3'],['payments','800'],['credits','900'],['start','2026-11-01'],['due','2026-10-10'],['response','2026-10-09'],['due','']]) {
    p.$('example').click();p.set(key,value);p.submit();
    assert.equal(p.$('document').hidden,true,key+value);assert.equal(p.$('print').disabled,true);
    assert.equal(p.$('error').hidden,false);assert.equal(p.prompt.hidden,true);
  }
  assert.deepEqual(p.events,[]);p.w.close();
});
test('integer cents and calendar dates stay stable across daylight-saving boundaries',()=>{
  const p=setup();p.$('example').click();
  for(const [key,value] of Object.entries({tuition:'0.30',fees:'0',credits:'0',payments:'0.10',start:'2026-10-01',end:'2026-11-30',due:'2026-10-31',issued:'2026-11-01',response:'2026-11-02'})) p.set(key,value);
  p.submit();assert.equal(p.$('error').hidden,true);assert.match(p.$('document').textContent,/Outstanding balance\$0.20/);
  assert.match(p.$('document').textContent,/November 1, 2026/);assert.doesNotMatch(p.$('document').textContent,/-\$0.00|NaN|Infinity/);p.w.close();
});
test('edits and reset remove stale letters, copy and print availability',()=>{
  const p=setup();p.$('example').click();p.submit();p.set('tuition','900');
  assert.equal(p.$('document').hidden,true);assert.equal(p.$('document').textContent,'');assert.equal(p.$('copy').disabled,true);assert.equal(p.prompt.hidden,true);
  p.submit();let prints=0;p.w.print=()=>prints++;p.$('print').click();assert.equal(prints,1);
  p.$('reset').click();assert.equal(p.$('program').value,'');assert.equal(p.$('print').disabled,true);assert.equal(p.prompt.hidden,true);p.w.close();
});
test('private hostile input stays inert and only fixed-label events leave page; copy fallback works',async()=>{
  const p=setup();p.$('example').click();p.set('program','<img src=x onerror=alert(1)>');p.set('instructions','<script>private()</script>');p.submit();
  assert.equal(p.$('document').querySelector('img,script'),null);assert.match(p.$('document').textContent,/<script>private/);
  assert.doesNotMatch(JSON.stringify(p.events),/private|<img|600/);assert.equal(p.w.location.search,'');
  p.$('copy').click();await Promise.resolve();assert.match(p.$('status').textContent,/selected/);assert.match(p.w.getSelection().toString(),/<script>private/);p.w.close();
});
test('clipboard output is plain text and late promises do not resurrect stale status',async()=>{
  const p=setup();p.$('example').click();p.submit();let copied,done;
  Object.defineProperty(p.w.navigator,'clipboard',{value:{writeText:text=>{copied=text;return new Promise(resolve=>done=resolve);}}});
  p.$('copy').click();assert.match(copied,/Outstanding balance: \$600.00/);p.set('family','Changed');done();await Promise.resolve();
  assert.match(p.$('status').textContent,/after making changes/);p.w.close();
});
test('real form works without sample watermark or analytics and does not send requests',()=>{
  const p=setup();delete p.w.tpTrack;
  const values={program:'My Program',family:'Family A',contact:'office@example.com',issued:'2026-10-10',due:'2026-10-01',start:'2026-10-01',end:'2026-10-31',tuition:'100',instructions:'Use your usual payment method.',response:'2026-10-12'};
  for(const [key,value] of Object.entries(values))p.set(key,value);
  p.submit();assert.equal(p.$('error').hidden,true);assert.doesNotMatch(p.$('document').textContent,/FICTIONAL EXAMPLE/);assert.equal(p.$('print').disabled,false);p.w.close();
});
