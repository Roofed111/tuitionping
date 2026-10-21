const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs');
const {JSDOM}=require('jsdom');
const catalog=[{key:'micro',name:'Micro',price:9,families:10,locations:1},{key:'starter',name:'Starter',price:19,families:30,locations:1},{key:'growth',name:'Growth',price:39,families:75,locations:null},{key:'multisite',name:'Multi-site',price:59,families:200,locations:null}];
function setup(plans=catalog){
  const html=fs.readFileSync('templates/solution_finder.html','utf8').replace('{{ catalog | tojson }}',JSON.stringify(plans)).replace('{{ trial_days | tojson }}','30');
  const dom=new JSDOM(html,{url:'https://www.tuitionping.com/find-your-tuition-solution',runScripts:'outside-only'}),w=dom.window,$=id=>w.document.getElementById('finder-'+id),events=[];
  w.tpTrack=(...args)=>events.push(args);w.fetch=()=>{throw Error('No answers may be sent');};
  for(const name of ['localStorage','sessionStorage'])Object.defineProperty(w,name,{get(){throw Error('No answer storage');}});
  w.eval(fs.readFileSync('static/solution-finder.js','utf8'));
  const submit=()=> $('form').dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));
  function choose(frequency='monthly',count='10',job='automate'){
    $('reset').click();w.document.querySelector('[name=frequency][value='+frequency+']').checked=true;submit();$('families').value=count;submit();$('job').value=job;submit();
  }
  return{w,$,events,submit,choose};
}
test('three steps validate input, support back/reset, and clear stale recommendations',()=>{
  const p=setup();assert.equal(p.$('result').hidden,true);p.submit();assert.equal(p.$('error').hidden,false);assert.deepEqual(p.events,[]);
  p.choose();assert.equal(p.$('result').hidden,false);p.$('job').dispatchEvent(new p.w.Event('change',{bubbles:true}));assert.equal(p.$('result').hidden,true);
  p.$('back').click();assert.match(p.$('progress').textContent,/Step 2/);p.$('families').value='-1';p.submit();assert.equal(p.$('error').hidden,false);
  p.$('reset').click();assert.match(p.$('progress').textContent,/Step 1/);assert.equal(p.$('families').value,'');p.w.close();
});
test('monthly plan boundaries follow catalog limits, prices and location disclosures',()=>{
  const p=setup();for(const [count,name]of [['0','Micro'],['10','Micro'],['11','Starter'],['30','Starter'],['31','Growth'],['75','Growth'],['76','Multi-site'],['200','Multi-site']]){
    p.choose('monthly',count);assert.match(p.$('result-heading').textContent,new RegExp(name));
    assert.match(p.$('result-actions').innerHTML,/\/signup\?plan=/);
    assert.match(p.$('result-note').textContent,/requires a card/);
    assert.match(p.$('result-fit').textContent,Number(count)<=30?/one location/:/multiple locations/);
  }p.choose('monthly','201');assert.match(p.$('result-heading').textContent,/larger roster/);assert.doesNotMatch(p.$('result-actions').innerHTML,/signup/);p.w.close();
});
test('changed catalog is used without hardcoded price or capacity decisions',()=>{
  const p=setup(catalog.map(plan=>plan.key==='micro'?{...plan,price:12,families:11}:plan));p.choose('monthly','11');assert.match(p.$('result-heading').textContent,/Micro/);assert.match(p.$('result-description').textContent,/\$12\/month for up to 11/);p.w.close();
});
test('weekly, fortnightly and term schedules get manual resources without a subscription claim',()=>{
  const p=setup();for(const f of ['weekly','fortnightly','other']){p.choose(f,'15');assert.match(p.$('result-description').textContent,/currently schedules monthly/);assert.doesNotMatch(p.$('result-actions').innerHTML,/signup/);assert.match(p.$('result-actions').innerHTML,/reminder-generator/);}p.w.close();
});
test('all manual tasks route to their distinct tool and payment collection routes to comparisons',()=>{
  const p=setup();for(const [job,path]of Object.entries({tracker:'/tools/tuition-payment-tracker',invoice:'/tools/daycare-invoice-receipt',reminder:'/guides/tuition-reminder-templates#reminder-generator',past_due:'/tools/daycare-past-due-letter',late_fee:'/tools/late-fee-calculator',tax_statement:'/tools/daycare-tax-statement',payments:'/compare/brightwheel'})){
    p.choose('weekly','18',job);assert.equal(p.$('result-actions').querySelector('a').getAttribute('href'),path);assert.doesNotMatch(p.$('result-actions').innerHTML,/signup/);
  }p.w.close();
});
test('only fixed recommendation labels are measured and answers never enter URLs or storage',()=>{
  const p=setup();p.choose('monthly','37','tracker');p.choose('monthly','37','tracker');
  assert.equal(p.events.filter(e=>e[0]==='solution_recommended').length,1);
  p.w.document.addEventListener('click',e=>e.preventDefault());const a=p.$('result-actions').querySelector('a');a.click();a.click();assert.equal(p.events.filter(e=>e[0]==='solution_action_click').length,1);
  assert.doesNotMatch(JSON.stringify(p.events),/37|monthly/);assert.equal(p.w.location.search,'');p.w.close();
});
test('invalid counts and missing tasks cannot create a result, analytics failure does not break recommendations',()=>{
  const p=setup();for(const count of ['','-1','1.5','1e2','10001','<img>']){p.choose('monthly',count);assert.equal(p.$('result').hidden,true);}
  p.choose('monthly','1','');assert.equal(p.$('result').hidden,true);p.w.tpTrack=()=>{throw Error('Unavailable');};p.choose();assert.equal(p.$('result').hidden,false);p.w.close();
});
