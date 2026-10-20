const {test} = require('node:test');
const assert = require('node:assert/strict');
const {calculate,daysBetween,cents} = require('../static/late-fee-math.js');
const base={tuition:'1200',credit:'0',days:7,grace:0,type:'flat',amount:'25',dailyRate:'5',cap:''};
const calc=overrides=>calculate({...base,...overrides});
test('five fee models',()=>{
  assert.equal(calc({}).fee,2500);
  assert.equal(calc({type:'daily',amount:'5'}).fee,3500);
  assert.equal(calc({type:'weekly',days:8}).fee,5000);
  assert.equal(calc({type:'percent',amount:'5'}).fee,6000);
  assert.equal(calc({type:'flat_daily'}).fee,6000);
});
test('grace boundary and zero overdue days',()=>{
  for(const type of ['flat','daily','weekly','percent','flat_daily']){
    assert.equal(calc({type,days:0}).fee,0);
    assert.equal(calc({type,days:2,grace:2}).fee,0);
  }
  assert.equal(calc({type:'daily',days:3,grace:2,amount:'5'}).fee,500);
  assert.equal(calc({type:'weekly',days:9,grace:2}).fee,2500);
  assert.equal(calc({type:'weekly',days:10,grace:2}).fee,5000);
  assert.equal(calc({type:'flat_daily',days:3,grace:2}).fee,3000);
});
test('worked example and caps affect fees only',()=>{
  const r=calc({credit:'200',grace:2,type:'daily',amount:'5',cap:'20'});
  assert.equal(r.balance,100000); assert.equal(r.billable,5); assert.equal(r.rawFee,2500);
  assert.equal(r.fee,2000); assert.equal(r.total,102000); assert.equal(r.capped,true);
  assert.equal(calc({cap:'0'}).fee,0);
  assert.equal(calc({cap:'25'}).capped,false);
});
test('credit precedes fee calculation',()=>{
  assert.equal(calc({type:'percent',amount:'5',credit:'200'}).fee,5000);
  assert.equal(calc({credit:'1200'}).total,0);
  assert.equal(calc({tuition:'0'}).fee,0);
  assert.throws(()=>calc({credit:'1200.01'}));
});
test('cent precision and percentage rounding',()=>{
  assert.equal(cents('19.99'),1999);
  assert.equal(calc({type:'daily',amount:'0.10',days:3}).fee,30);
  assert.equal(calc({type:'percent',amount:'2.55',tuition:'19.99'}).fee,51);
  assert.equal(calc({type:'percent',amount:'50',tuition:'0.01'}).fee,1);
  assert.equal(calc({type:'percent',amount:'100',tuition:'0.29'}).fee,29);
});
test('calendar dates work across DST, leap years and year boundaries',()=>{
  assert.equal(daysBetween('2026-03-07','2026-03-09'),2);
  assert.equal(daysBetween('2026-10-31','2026-11-02'),2);
  assert.equal(daysBetween('2024-02-28','2024-03-01'),2);
  assert.equal(daysBetween('2025-12-31','2026-01-01'),1);
  assert.equal(daysBetween('2026-10-06','2026-10-06'),0);
  assert.equal(daysBetween('2026-10-06','2026-10-01'),0);
  assert.throws(()=>daysBetween('2026-02-29','2026-03-01'));
  assert.throws(()=>daysBetween('','2026-10-06'));
  assert.throws(()=>daysBetween('1900-01-01','2026-10-06'));
});
test('invalid or excessive input never silently becomes a fee',()=>{
  for(const values of [{tuition:''},{tuition:'-1'},{tuition:'1.001'},{tuition:'NaN'},{tuition:'1e6'},{days:1.5},{days:3661},{grace:-1},{amount:'101',type:'percent'},{type:'unknown'},{cap:'-1'}]) assert.throws(()=>calc(values));
});
test('monotonic fees and caps over a year',()=>{
  for(const type of ['flat','daily','weekly','percent','flat_daily']){
    let last=0;
    for(let days=0;days<=365;days++){
      const r=calc({type,days,grace:3,cap:'100'});
      assert(r.fee>=last && r.fee<=10000); assert.equal(r.total,r.balance+r.fee); last=r.fee;
    }
  }
});
