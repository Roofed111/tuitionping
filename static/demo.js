(function () {
  'use strict';
  const initial = [
    {id:'jordan', name:'Jordan', amount:350, due:'Oct 11', language:'en', stage:'before', status:'Upcoming'},
    {id:'morales', name:'Morales', amount:1200, due:'Oct 8', language:'es', stage:'due', status:'Due today'},
    {id:'carter', name:'Carter', amount:980, due:'Oct 5', language:'en', stage:'late', status:'3 days overdue'},
    {id:'lee', name:'Lee', amount:850, due:'Oct 8', language:'en', stage:'due', status:'Verified paid', verified:true}
  ];
  let families, messages;
  let started = false;
  const el = id => document.getElementById(id);
  const track = (event, detail) => {if(window.tpTrack) window.tpTrack(event, detail);};
  const family = () => families.find(f => f.id === el('demo-family').value);
  function text(f) {
    if(f.verified) return 'Payment verified in this sample. Scheduled tuition reminders stop for this period.';
    const es = el('demo-language').value === 'es';
    const date = es ? ({'Oct 11':'11 de octubre','Oct 8':'8 de octubre','Oct 5':'5 de octubre'})[f.due] : f.due;
    const amount = '$' + f.amount.toLocaleString('en-US');
    if(es) {
      if(f.stage === 'before') return `Sunshine Daycare: Le recordamos que su pago de ${amount} vence el ${date}. Gracias.`;
      if(f.stage === 'due') return `Sunshine Daycare: Su pago de ${amount} vence hoy, ${date}. Responda PAID cuando haya enviado el pago.`;
      return `Sunshine Daycare: Aún no hemos verificado su pago de ${amount}, que venció el ${date}. Responda PAID si ya lo envió para que podamos revisarlo.`;
    }
    if(f.stage === 'before') return `Sunshine Daycare: Friendly reminder that tuition of ${amount} is due on ${date}. Thank you!`;
    if(f.stage === 'due') return `Sunshine Daycare: Tuition of ${amount} is due today (${date}). Reply PAID once you have sent it.`;
    return `Sunshine Daycare: We have not yet verified tuition of ${amount}, due ${date}. Reply PAID if you have sent it so we can review our records.`;
  }
  function addMessage(title, body) {messages.push({title,body});}
  function render() {
    const f = family();
    el('demo-roster').replaceChildren();
    families.forEach(item => {
      const tr=document.createElement('tr');
      [item.name,`$${item.amount.toLocaleString('en-US')} / ${item.due}`, item.verified ? 'Verified paid' : item.reported ? 'Reported paid — review' : item.status].forEach(value=>{const td=document.createElement('td');td.textContent=value;tr.appendChild(td);});
      el('demo-roster').appendChild(tr);
    });
    el('demo-context').textContent=`${f.name} family · $${f.amount.toLocaleString('en-US')} · Due ${f.due}.`;
    el('demo-preview').textContent=text(f);
    el('demo-preview').lang=el('demo-language').value;
    el('demo-send').disabled=!!(f.sent || f.verified || f.reported);
    el('demo-reply').disabled=!!(!f.sent || f.reported || f.verified);
    el('demo-verify').disabled=!!(!f.reported || f.verified);
    el('demo-review').textContent=families.filter(x=>x.reported && !x.verified).length;
    el('demo-verified').textContent=families.filter(x=>x.verified).length;
    el('demo-log').replaceChildren();
    messages.forEach(m=>{const li=document.createElement('li'),b=document.createElement('b'),p=document.createElement('span');b.textContent=m.title;p.textContent=m.body;li.append(b,p);el('demo-log').appendChild(li);});
    el('demo-empty').hidden=messages.length > 0;
  }
  function interaction() {if(!started){track('demo_started');started=true;}}
  function reset() {
    families=initial.map(f=>Object.assign({},f)); messages=[];
    el('demo-family').replaceChildren();
    families.forEach(f=>{const option=document.createElement('option');option.value=f.id;option.textContent=f.name+' family';el('demo-family').appendChild(option);});
    el('demo-language').value=family().language;
    el('demo-feedback').textContent='Start with a reminder. Every action here is simulated.';render();
  }
  el('demo-family').addEventListener('change',()=>{interaction();el('demo-language').value=family().language;el('demo-feedback').textContent=family().verified?'This sample payment is already verified. Choose another family to try a reminder.':'Preview the reminder, then work through the three steps.';render();});
  el('demo-language').addEventListener('change',()=>{interaction();if(el('demo-language').value==='es')track('demo_step','spanish');render();});
  el('demo-send').addEventListener('click',()=>{const f=family();if(f.sent || f.verified)return;interaction();addMessage(`Simulated reminder · ${f.name}`,text(f));f.sent=true;track('demo_step',f.stage);el('demo-feedback').textContent='Reminder simulated. Now try a parent replying PAID.';render();});
  el('demo-reply').addEventListener('click',()=>{const f=family();if(!f.sent || f.reported || f.verified)return;f.reported=true;addMessage(`Simulated parent reply · ${f.name}`,'PAID');addMessage('Simulated acknowledgment','Payment reported. The provider still needs to verify receipt against their payment records.');track('demo_step','reported');el('demo-feedback').textContent='Payment reported, not verified. Review your sample records and verify the payment next.';render();});
  el('demo-verify').addEventListener('click',()=>{const f=family();if(!f.reported || f.verified)return;f.verified=true;addMessage(`Sample provider verification · ${f.name}`,'Payment verified. Tuition reminders stop for this sample period.');track('demo_step','verified');el('demo-feedback').textContent='Sample payment verified. In your real account, verify only after confirming that the payment arrived.';render();});
  el('demo-reset').addEventListener('click',reset);
  reset();el('demo-app').hidden=false;
}());
