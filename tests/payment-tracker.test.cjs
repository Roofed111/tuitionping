const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const XLSX = require('xlsx');
const recalculate = require('xlsx-calc');
const file = path.join(__dirname,'../static/downloads/daycare-tuition-payment-tracker.xlsx');
function workbook(values={}) {
  const wb=XLSX.readFile(file,{sheetStubs:true});
  const sheet=wb.Sheets['Tracker'];
  for(const [cell,value] of Object.entries({A6:'Test account',D6:300,C6:46200,B2:46207,...values})) {
    if(value===null) delete sheet[cell];
    else sheet[cell]={t:typeof value==='number'?'n':'s',v:value};
  }
  recalculate(wb);
  return sheet;
}
test('actual workbook example recalculates to $190 and 7 days overdue',()=>{
  const wb=XLSX.readFile(file,{sheetStubs:true});recalculate(wb);
  const s=wb.Sheets['Worked example'];assert.equal(s.I6.v,190);assert.equal(s.J6.v,7);assert.equal(s.K6.v,'Overdue');assert.equal(s.I7.v,0);assert.equal(s.K7.v,'Paid');
});
test('parent-reported payment alone cannot reduce a balance',()=>{
  const s=workbook({H6:'PAID'});assert.equal(s.I6.v,300);assert.equal(s.K6.v,'Overdue');
});
test('verified payments, credits, assessed fees and totals preserve cents',()=>{
  const s=workbook({D6:300.25,E6:10,F6:20.1,G6:100.05});assert.ok(Math.abs(s.I6.v-190.1)<1e-8);assert.ok(Math.abs(s.B3.v-190.1)<1e-8);
});
test('overpayment never creates a negative balance or overdue status',()=>{
  const s=workbook({G6:350});assert.equal(s.I6.v,0);assert.equal(s.J6.v,0);assert.equal(s.K6.v,'Paid');
});
test('blank tuition, due date and review date have explicit states',()=>{
  assert.equal(workbook({D6:null}).K6.v,'');assert.equal(workbook({C6:null}).K6.v,'Needs due date');assert.equal(workbook({B2:null}).K6.v,'Set review date');
});
test('today and future bills are distinguished',()=>{
  assert.equal(workbook({C6:46207}).K6.v,'Due today');const s=workbook({C6:46208});assert.equal(s.K6.v,'Upcoming');assert.equal(s.J6.v,0);
});
