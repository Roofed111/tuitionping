const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs');
const {JSDOM}=require('jsdom');
function setup(){
  const dom=new JSDOM(fs.readFileSync('templates/embedded_calculator.html','utf8'),{url:'https://www.tuitionping.com/embed/late-fee-calculator',runScripts:'outside-only'}),w=dom.window,$=id=>w.document.getElementById('embed-'+id);
  w.fetch=()=>{throw Error('Amounts must not be sent');};w.tpTrack=()=>{throw Error('Embed loads must not be tracked');};
  for(const key of ['localStorage','sessionStorage'])Object.defineProperty(w,key,{get(){throw Error('No persistence');}});
  w.eval(fs.readFileSync('static/late-fee-math.js','utf8'));w.eval(fs.readFileSync('static/embedded-calculator.js','utf8'));
  const set=(id,value)=>{$(id).value=value;$(id).dispatchEvent(new w.Event('input',{bubbles:true}));};
  const submit=()=>$('calc-form').dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));
  return{w,$,set,submit};
}
test('widget reuses all five models and integer-cent calculations',()=>{
  const p=setup();assert.equal(p.$('total').textContent,'$1,025.00');p.set('credit','200');p.set('grace','2');
  for(const [model,amount,fee]of [['flat','25','$25.00'],['daily','5','$25.00'],['weekly','25','$25.00'],['percent','5','$40.00'],['flat_daily','25','$50.00']]){
    p.set('type',model);p.set('amount',amount);assert.equal(p.$('fee').textContent,fee);assert.equal(p.$('error').hidden,true);
  }p.set('cap','20');assert.equal(p.$('fee').textContent,'$20.00');p.set('credit','1000');assert.equal(p.$('fee').textContent,'$0.00');p.w.close();
});
test('invalid values clear stale results, hidden daily rate is excluded, submit announces a result',()=>{
  const p=setup();for(const [id,value]of [['credit','1001'],['days','-1'],['days','1.5'],['days','3661'],['amount','1.001'],['amount','1e3'],['cap','<img>']]){
    const old=p.$(id).value;p.set(id,value);assert.equal(p.$('result').hidden,true);assert.equal(p.$('total').textContent,'');assert.equal(p.$('error').hidden,false);p.set(id,old);
  }p.set('dailyRate','bad');assert.equal(p.$('result').hidden,false);p.set('type','flat_daily');assert.equal(p.$('result').hidden,true);p.set('dailyRate','5');p.submit();assert.match(p.$('announcement').textContent,/Estimated total/);p.w.close();
});
test('hostile amount text stays inert and no values are added to navigation links',()=>{
  const p=setup();const links=[...p.w.document.querySelectorAll('a')].map(a=>a.getAttribute('href'));
  p.set('tuition','<script>alert(1)</script>');assert.equal(p.$('result').hidden,true);assert.equal(p.$('error').querySelector('script'),null);
  assert.deepEqual([...p.w.document.querySelectorAll('a')].map(a=>a.getAttribute('href')),links);assert.equal(p.w.location.search,'');p.w.close();
});
test('embed snippet copy counts successful clipboard operations and offers manual fallback',async()=>{
  const dom=new JSDOM('<button id="embed-copy"></button><textarea id="embed-code">iframe example</textarea><p id="embed-copy-status"></p>',{runScripts:'outside-only'}),w=dom.window;
  const events=[];let copied;w.tpTrack=(...args)=>events.push(args);Object.defineProperty(w.navigator,'clipboard',{value:{writeText:async text=>{copied=text;}}});
  w.eval(fs.readFileSync('static/embed-code.js','utf8'));w.document.querySelector('button').click();await new Promise(resolve=>setImmediate(resolve));assert.equal(copied,'iframe example');assert.deepEqual(events,[['embed_code_copied']]);
  w.navigator.clipboard.writeText=async()=>{throw Error('Blocked');};w.document.querySelector('button').click();await new Promise(resolve=>setImmediate(resolve));assert.match(w.document.querySelector('p').textContent,/selected/);assert.equal(events.length,1);w.close();
});
