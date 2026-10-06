(function () {
  'use strict';
  const meta = document.querySelector('meta[name="tp-analytics-token"]');
  if (!meta || navigator.doNotTrack === '1' || navigator.globalPrivacyControl) return;
  window.tpTrack = function (event, detail) {
    fetch('/analytics/event', {method:'POST', credentials:'same-origin', keepalive:true,
      headers:{'Content-Type':'application/json', 'X-TP-Analytics':meta.content},
      body:JSON.stringify({event:event, detail:detail || '', path:location.pathname})}).catch(function () {});
  };
  document.addEventListener('click', function (e) {
    const link = e.target.closest('a[href]');
    if (link && new URL(link.href, location.href).pathname === '/signup') window.tpTrack('trial_click');
  });
}());
