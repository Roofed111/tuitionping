(function () {
  'use strict';
  const $=id=>document.getElementById('finder-'+id), form=$('form');
  if (!form) return;
  const catalog=JSON.parse($('catalog').textContent), trial=JSON.parse($('trial').textContent);
  const steps=[...form.querySelectorAll('[data-finder-step]')], shown=new Set(), clicked=new Set();
  let step=0, started=false, current='';
  const track=(event,detail='')=>{try{if(window.tpTrack)window.tpTrack(event,detail);}catch(_){}};
  const clear=()=>{current='';$('result').hidden=true;$('result-actions').replaceChildren();};
  function show(index,focus=true){clear();step=index;steps.forEach((el,i)=>el.hidden=i!==step);$('back').hidden=step===0;$('next').textContent=step===2?'Show recommendation':'Continue';$('progress').textContent='Step '+(step+1)+' of 3';$('error').hidden=true;if(focus)steps[step].querySelector('legend').focus();}
  function validate(index){
    if(index===0&&!['monthly','weekly','fortnightly','other'].includes(form.querySelector('[name=frequency]:checked')?.value))throw Error('Choose how often tuition is due.');
    if(index===1&&(!/^\d{1,5}$/.test($('families').value.trim())||Number($('families').value)>10000))throw Error('Enter a whole family count from 0 through 10,000.');
    if(index===2&&!['automate','tracker','invoice','reminder','past_due','late_fee','tax_statement','payments'].includes($('job').value))throw Error('Choose the task you need help with.');
  }
  function recommend(){
    const frequency=form.querySelector('[name=frequency]:checked').value, count=Number($('families').value), job=$('job').value;
    const resources={
      tracker:['tracker','Start with the free payment tracker','Record bills, credits and payments you have verified. The workbook calculates remaining balances and flags bills that need follow-up.','Open the free tracker','/tools/tuition-payment-tracker'],
      invoice:['invoice','Create a clear invoice or receipt','Build a bill or record a payment you have verified, including partial payments and the remaining balance.','Open the invoice and receipt tool','/tools/daycare-invoice-receipt'],
      reminder:['manual_reminders','Draft your next reminder text','Enter the amount and actual due date, review the wording, then copy the reminder in English or Spanish.','Open the reminder generator','/guides/tuition-reminder-templates#reminder-generator'],
      past_due:['past_due','Create a written past-due notice','Show the billing period, credits, verified payments and balance still due in a printable letter.','Create a past-due letter','/tools/daycare-past-due-letter'],
      late_fee:['late_fee','Calculate your tuition late-fee estimate','Apply the fee model, grace period, credits and cap from your written terms.','Open the late-fee calculator','/tools/late-fee-calculator'],
      tax_statement:['tax_statement','Prepare a year-end payment statement','Create a printable summary using payments you have verified against your records.','Open the statement tool','/tools/daycare-tax-statement']
    };
    let result,fit='',note='The free tool needs no account; review its output before using it.';
    if(job==='automate'){
      if(frequency!=='monthly'){
        result=['manual_reminders','Use the manual reminder tools for this schedule','The subscription currently schedules monthly due-day texts. Use the reminder generator with each bill’s actual date and the tracker for your '+count+'-family roster.','Draft a reminder','/guides/tuition-reminder-templates#reminder-generator'];
        fit='Weekly, every-two-week, term-based and mixed billing schedules are not automated by the current subscription.';
      }else{
        const plan=catalog.find(p=>p.families>=count);
        if(plan){
          result=['reminder_subscription',plan.name+' fits your monthly roster',plan.name+' is $'+plan.price+'/month for up to '+plan.families+' families. You entered '+count+' families. Preview the reminder, parent PAID report and provider confirmation.','Try the interactive demo','/demo'];
          fit=plan.locations===1?'This plan covers one location. If you manage multiple locations, compare Growth and Multi-site before choosing.':'This plan supports multiple locations within its total family limit.';
          note='The demo uses fictional families and sends no texts. The real '+trial+'-day trial requires a card; your selected subscription starts afterward unless you cancel.';
          result.plan=plan;
        }else{
          result=['large_roster','Check the fit for your larger roster','Your '+count+'-family roster exceeds the largest published plan’s '+catalog[catalog.length-1].families+'-family limit. Ask support about your needs before starting a subscription.','Ask about setup and fit','/setup-help'];
          fit='The free payment tracker is also available for manual records; review its workbook capacity before using it.';
        }
      }
    }else if(job==='payments'){
      result=['payment_platform','Compare tools that collect payments','You need payment collection or autopay. TuitionPing handles reminder texts and payment reports; broader billing platforms can be assessed against your collection requirements.','Compare billing platform scope','/compare/brightwheel'];
      fit='Keep your billing frequency and '+count+'-family roster in mind when checking platform support and quotes.';
    }else{result=resources[job];fit='This manual tool can help with your '+count+'-family roster regardless of billing frequency. Its own capacity and instructions still apply.';}
    current=result[0];$('result-heading').textContent=result[1];$('result-description').textContent=result[2];$('result-fit').textContent=fit;$('result-note').textContent=note;
    function link(label,path,primary){const a=document.createElement('a');a.textContent=label;a.href=path;a.className=primary?'btn':'btn btn-ghost';a.dataset.solutionAction=current;$('result-actions').append(a);}
    link(result[3],result[4],true);
    if(result.plan){link('Start '+result.plan.name+' trial','/signup?plan='+encodeURIComponent(result.plan.key),false);link('Compare all plans and locations','/pricing',false);}
    else if(current==='manual_reminders'){link('Open the free tracker','/tools/tuition-payment-tracker',false);}
    else if(current==='payment_platform'){link('See the Procare comparison','/compare/procare',false);}
    else{link('Browse free tools','/guides',false);}
    $('result').hidden=false;$('result-heading').focus();
    if(!shown.has(current)){shown.add(current);track('solution_recommended',current);}
  }
  form.addEventListener('submit',event=>{event.preventDefault();try{validate(step);if(!started){started=true;track('solution_finder_started');}if(step<2)show(step+1);else{[0,1,2].forEach(validate);clear();recommend();}}catch(error){clear();$('error').textContent=error.message;$('error').hidden=false;}});
  form.addEventListener('input',()=>{clear();$('error').hidden=true;});form.addEventListener('change',()=>{clear();$('error').hidden=true;});
  $('back').addEventListener('click',()=>show(Math.max(0,step-1)));
  $('reset').addEventListener('click',()=>{form.reset();show(0);});
  $('result-actions').addEventListener('click',event=>{const link=event.target.closest('a[data-solution-action]');if(!event.defaultPrevented&&link&&current&&!clicked.has(current)){clicked.add(current);track('solution_action_click',current);}});
  $('controls').hidden=false;show(0,false);
}());
