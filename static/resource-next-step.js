(function () {
  'use strict';
  const panel = document.getElementById('resource-next-step');
  if (!panel) return;
  const kinds = {
    '/guides/tuition-reminder-templates': ['reminder'],
    '/tools/tuition-payment-tracker': ['tracker'],
    '/tools/daycare-invoice-receipt': ['invoice', 'receipt']
  }[location.pathname];
  if (!kinds) return;
  const shown = new Set(), clicked = new Set();
  let current = '';
  function track(event, kind) {
    try { if (window.tpTrack) window.tpTrack(event, kind); } catch (_) {}
  }
  // Only these fixed resource labels reach analytics. No form values, message
  // contents, amounts, family labels or document details leave the page.
  window.tpResourceNextStep = function (kind) {
    if (!kinds.includes(kind)) return;
    current = kind;
    panel.hidden = false;
    document.getElementById('resource-next-status').textContent = {
      reminder: 'Next: preview scheduled monthly reminders',
      tracker: 'Next: see how reminders fit your records',
      invoice: 'Next: follow up on an unpaid bill',
      receipt: 'Next: explore provider payment review'
    }[kind];
    const invoice = panel.querySelector('[data-next-invoice]');
    const receipt = panel.querySelector('[data-next-receipt]');
    if (invoice && receipt) {
      invoice.hidden = kind === 'receipt';
      receipt.hidden = kind !== 'receipt';
      panel.setAttribute('aria-labelledby', kind === 'receipt' ? 'resource-next-receipt-title' : 'resource-next-title');
      receipt.id = 'resource-next-receipt-title';
    }
    if (!shown.has(kind)) { shown.add(kind); track('resource_next_step', kind); }
  };
  window.tpHideResourceNextStep = function () { panel.hidden = true; current = ''; };
  document.addEventListener('click', function (event) {
    if (event.defaultPrevented || event.button > 0 || !event.target.closest) return;
    const link = event.target.closest('a[href]');
    if (!link) return;
    const url = new URL(link.href, location.href);
    if (url.origin !== location.origin) return;
    if (kinds.includes('tracker') && [
      '/static/downloads/daycare-tuition-payment-tracker.xlsx',
      '/static/downloads/daycare-tuition-collection-kit.zip'
    ].includes(url.pathname)) window.tpResourceNextStep('tracker');
    if (current && !panel.hidden && panel.contains(link) && link.dataset.resourceAction === 'demo'
        && url.pathname === '/demo' && !clicked.has(current)) {
      clicked.add(current); track('resource_demo_click', current);
    }
  });
}());
