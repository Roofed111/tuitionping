(function () {
  'use strict';
  const el = id => document.getElementById(id);
  const form = el('reminder-form');
  if (!form) return;
  const stages = ['before', 'due', 'late'];
  const languages = ['en', 'es'];
  const fields = ['program', 'amount', 'due', 'stage', 'payment', 'language'];
  const clean = value => value.trim().replace(/\s+/g, ' ');

  function money(value) {
    const raw = value.trim();
    if (!/^(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d{1,2})?$/.test(raw)) throw new Error('Enter a positive dollar amount with no more than two decimal places.');
    const parts = raw.replace(/,/g, '').split('.');
    const cents = Number(parts[0]) * 100 + Number((parts[1] || '').padEnd(2, '0'));
    if (!Number.isSafeInteger(cents) || cents <= 0 || cents > 100000000) throw new Error('Enter an unpaid balance greater than $0 and no more than $1,000,000.');
    return new Intl.NumberFormat('en-US', {style:'currency', currency:'USD'}).format(cents / 100);
  }

  function calendarDate(value, language) {
    const parts = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
    if (!parts) throw new Error('Choose a valid tuition due date.');
    const year = Number(parts[1]), month = Number(parts[2]), day = Number(parts[3]);
    const date = new Date(year, month - 1, day, 12);
    if (year < 1900 || date.getFullYear() !== year || date.getMonth() !== month - 1 || date.getDate() !== day) throw new Error('Choose a valid tuition due date.');
    return new Intl.DateTimeFormat(language === 'es' ? 'es-US' : 'en-US', {year:'numeric', month:'long', day:'numeric'}).format(date);
  }

  function invalidate() {
    if (window.tpHideResourceNextStep) window.tpHideResourceNextStep();
    el('reminder-output').value = '';
    el('reminder-copy').disabled = true;
    el('reminder-count').textContent = 'Generate a reminder to preview your current details.';
    el('reminder-status').textContent = '';
    el('reminder-error').textContent = '';
    el('reminder-error').hidden = true;
  }

  form.addEventListener('submit', function (event) {
    event.preventDefault();
    invalidate();
    try {
      const program = clean(el('reminder-program').value);
      const payment = clean(el('reminder-payment').value);
      const stage = el('reminder-stage').value, language = el('reminder-language').value;
      if (!program || program.length > 80) throw new Error('Enter a program name of 1 to 80 characters.');
      if (payment.length > 180) throw new Error('Keep payment instructions to 180 characters or fewer.');
      if (!stages.includes(stage) || !languages.includes(language)) throw new Error('Choose a reminder type and message language from the list.');
      const amount = money(el('reminder-amount').value);
      const due = calendarDate(el('reminder-due').value, language);
      const english = {
        before: `${program}: Friendly reminder that tuition of ${amount} is due on ${due}.`,
        due: `${program}: Tuition of ${amount} is due on ${due}.`,
        late: `${program}: Our records show ${amount} still owed for tuition due on ${due}.`
      };
      const spanish = {
        before: `${program}: Le recordamos que el pago de matrícula de ${amount} vence el ${due}.`,
        due: `${program}: El pago de matrícula de ${amount} vence el ${due}.`,
        late: `${program}: Nuestros registros muestran un saldo pendiente de ${amount} por la matrícula que venció el ${due}.`
      };
      const closing = stage === 'late'
        ? (language === 'es' ? 'Si ya pagó, avísenos para que podamos revisar nuestros registros. Gracias.' : 'If you already paid, please let us know so we can check our records. Thank you.')
        : (language === 'es' ? 'Si tiene alguna pregunta sobre el pago, comuníquese con nosotros. Gracias.' : 'Please contact us if you have a payment question. Thank you.');
      const message = [(language === 'es' ? spanish : english)[stage], payment, closing].filter(Boolean).join('\n');
      el('reminder-output').value = message;
      el('reminder-output').lang = language;
      el('reminder-count').textContent = Array.from(message).length + ' characters · Review before sending.';
      el('reminder-copy').disabled = false;
      el('reminder-status').textContent = 'Reminder drafted. No text has been sent.';
      // Only a fixed event and reminder stage enter existing analytics.
      // Program names, balances, dates and message contents stay in this page.
      try { if (window.tpTrack) window.tpTrack('reminder_generated', stage); } catch (_) {}
      if (window.tpResourceNextStep) window.tpResourceNextStep('reminder');
    } catch (error) {
      el('reminder-error').textContent = error.message;
      el('reminder-error').hidden = false;
    }
  });

  fields.forEach(function (field) {
    ['input', 'change'].forEach(function (event) { el('reminder-' + field).addEventListener(event, invalidate); });
  });

  el('reminder-copy').addEventListener('click', async function () {
    if (el('reminder-copy').disabled || !el('reminder-output').value) return;
    const message = el('reminder-output').value;
    try {
      await navigator.clipboard.writeText(message);
      if (!el('reminder-copy').disabled && el('reminder-output').value === message) el('reminder-status').textContent = 'Reminder copied. Review it before sending privately.';
    } catch (_) {
      if (el('reminder-copy').disabled || el('reminder-output').value !== message) return;
      el('reminder-output').focus();
      el('reminder-output').select();
      el('reminder-status').textContent = 'Message selected. Copy it using your browser or keyboard.';
    }
  });

  el('reminder-example').addEventListener('click', function () {
    form.reset();
    const date = new Date();
    date.setDate(date.getDate() + 3);
    el('reminder-program').value = 'Sunshine Daycare';
    el('reminder-amount').value = '300.00';
    el('reminder-due').value = date.getFullYear() + '-' + String(date.getMonth() + 1).padStart(2,'0') + '-' + String(date.getDate()).padStart(2,'0');
    el('reminder-payment').value = 'Please use the payment method in your tuition agreement.';
    invalidate();
    el('reminder-status').textContent = 'Fictional sample loaded. Select Generate reminder to preview it.';
  });

  el('reminder-reset').addEventListener('click', function () {
    form.reset(); invalidate(); el('reminder-program').focus();
  });
  el('reminder-generator-app').hidden = false;
}());
