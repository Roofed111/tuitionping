const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {JSDOM} = require('jsdom');

function setup() {
  const dom = new JSDOM(fs.readFileSync('templates/guide_templates.html','utf8'), {url:'https://www.tuitionping.com/guides/tuition-reminder-templates',runScripts:'outside-only'});
  const w=dom.window, d=w.document, $=id=>d.getElementById('reminder-'+id), events=[];
  w.tpTrack=(...args)=>events.push(args);
  w.fetch=()=>{throw Error('The generator must not send form values over the network');};
  Object.defineProperty(w,'localStorage',{get(){throw Error('Form entries must not be persisted');}});
  assert.equal($('generator-app').hidden,true);
  w.eval(fs.readFileSync('static/reminder-generator.js','utf8'));
  const set=(id,value)=>{$(id).value=value;$(id).dispatchEvent(new w.Event('input',{bubbles:true}));};
  const submit=()=>$('form').dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));
  const valid=()=>{set('program','Little Oaks');set('amount','300.00');set('due','2026-10-23');set('stage','before');set('language','en');};
  return {dom,w,$,events,set,submit,valid};
}

test('progressive enhancement and sample loading do not claim tool use',()=>{
  const {dom,$,events}=setup();assert.equal($('generator-app').hidden,false);assert.equal($('copy').disabled,true);
  $('example').click();assert.equal($('program').value,'Sunshine Daycare');assert.equal($('output').value,'');assert.deepEqual(events,[]);dom.window.close();
});

test('all reminder stages use the selected amount and explicit due date',()=>{
  const {dom,$,set,valid,submit,events}=setup();valid();
  for(const stage of ['before','due','late']){set('stage',stage);submit();assert.equal($('error').hidden,true);assert.match($('output').value,/Little Oaks/);assert.match($('output').value,/\$300\.00/);assert.match($('output').value,/October 23, 2026/);assert.doesNotMatch($('output').value,/today|verified payment received/i);}
  assert.match($('output').value,/If you already paid/);assert.deepEqual(events,[['reminder_generated','before'],['reminder_generated','due'],['reminder_generated','late']]);dom.window.close();
});

test('Spanish output keeps entered payment instructions and uses a Spanish date',()=>{
  const {dom,$,set,valid,submit}=setup();valid();set('language','es');set('stage','late');set('payment','Pague con el método acordado.');submit();
  assert.equal($('output').lang,'es');assert.match($('output').value,/23 de octubre de 2026/);assert.match($('output').value,/Si ya pagó/);assert.match($('output').value,/Pague con el método acordado/);dom.window.close();
});

test('money validation rejects invalid balances and preserves valid cents',()=>{
  const {dom,$,set,valid,submit,events}=setup();valid();
  for(const amount of ['','0','-1','1.001','1e3','1,00','NaN','Infinity','1000000.01']){set('amount',amount);submit();assert.equal($('error').hidden,false,amount);assert.equal($('copy').disabled,true);assert.equal($('output').value,'');}
  assert.deepEqual(events,[]);set('amount','1,200.05');submit();assert.match($('output').value,/\$1,200\.05/);dom.window.close();
});

test('date-only formatting preserves calendar dates across time zones and leap days',()=>{
  const original=process.env.TZ;
  try {for(const tz of ['UTC','America/Los_Angeles','Pacific/Auckland']){process.env.TZ=tz;const {dom,$,set,valid,submit}=setup();valid();for(const [date,text] of [['2028-02-29','February 29, 2028'],['2026-03-08','March 8, 2026'],['2026-11-01','November 1, 2026']]){set('due',date);submit();assert.ok($('output').value.includes(text),tz+' '+date);}set('due','2026-02-29');submit();assert.equal($('error').hidden,false);dom.window.close();}}
  finally {if(original===undefined)delete process.env.TZ;else process.env.TZ=original;}
});

test('editing any input clears the stale draft and disables copying',()=>{
  const {dom,$,set,valid,submit}=setup();valid();
  for(const field of ['program','amount','due','stage','payment','language']){submit();assert.equal($('copy').disabled,false);set(field,$(field).value);assert.equal($('output').value,'',field);assert.equal($('copy').disabled,true,field);}
  submit();$('reset').click();assert.equal($('program').value,'');assert.equal($('output').value,'');assert.equal($('copy').disabled,true);dom.window.close();
});

test('private values stay out of URLs and analytics; hostile text is inert',()=>{
  const {dom,w,$,set,valid,submit,events}=setup();valid();set('program','<img src=x onerror=alert(1)>');set('payment','Private transfer reference ABC-PRIVATE');submit();
  assert.match($('output').value,/<img src=x/);assert.equal(w.document.querySelector('img'),null);assert.equal(w.location.search,'');assert.equal(w.location.hash,'');assert.deepEqual(events,[['reminder_generated','before']]);
  set('program','x'.repeat(81));submit();assert.equal($('error').hidden,false);set('program','Valid daycare');set('payment','x'.repeat(181));submit();assert.equal($('error').hidden,false);dom.window.close();
});

test('copy succeeds and has a manual selection fallback',async()=>{
  const {dom,w,$,valid,submit}=setup();valid();submit();let copied='';Object.defineProperty(w.navigator,'clipboard',{value:{writeText:async text=>{copied=text;}}});
  $('copy').click();await new Promise(setImmediate);assert.equal(copied,$('output').value);assert.match($('status').textContent,/copied/);
  w.navigator.clipboard.writeText=async()=>{throw Error('Permission denied')};$('copy').click();await new Promise(setImmediate);assert.equal(w.document.activeElement,$('output'));assert.equal($('output').selectionEnd,copied.length);assert.match($('status').textContent,/selected/);dom.window.close();
});

test('clipboard completion cannot restore a stale copied status after editing',async()=>{
  const {dom,w,$,set,valid,submit}=setup();valid();submit();let finish;Object.defineProperty(w.navigator,'clipboard',{value:{writeText:()=>new Promise(resolve=>{finish=resolve})}});$('copy').click();set('amount','100');finish();await new Promise(setImmediate);assert.equal($('output').value,'');assert.equal($('status').textContent,'');dom.window.close();
});

test('analytics outages do not prevent drafting or copying',()=>{
  for(const broken of [undefined,()=>{throw Error('Analytics unavailable')}]){const {dom,w,$,valid,submit}=setup();w.tpTrack=broken;valid();submit();assert.equal($('copy').disabled,false);assert.match($('output').value,/Little Oaks/);dom.window.close();}
});
