(function () {
  'use strict';
  const meta = document.querySelector('meta[name="tp-analytics-token"]');
  if (!meta || navigator.doNotTrack === '1' || navigator.globalPrivacyControl) return;
  window.tpTrack = function (event, detail) {
    return fetch('/analytics/event', {method:'POST', credentials:'same-origin', keepalive:true,
      headers:{'Content-Type':'application/json', 'X-TP-Analytics':meta.content},
      body:JSON.stringify({event:event, detail:detail || '', path:location.pathname})}).catch(function () {});
  };
  let verified = false;
  let interacted = false, scrolled = false, engaged = false, visibleMs = 0, clock = 0, timer;
  function activity(e) {
    if (!e.isTrusted || document.visibilityState !== 'visible') return;
    if (e.type === 'scroll') scrolled = true;
    else interacted = true;
    checkEngagement();
  }
  function checkEngagement() {
    if (!verified || engaged || document.visibilityState !== 'visible' || navigator.webdriver === true) return;
    const signal = interacted && visibleMs >= 8000 ? 'interaction' : scrolled && visibleMs >= 30000 ? 'active_reading' : '';
    if (!signal) return;
    engaged = true;
    clearInterval(timer);
    window.tpTrack('visitor_engaged', signal);
    ['pointerdown','keydown','scroll'].forEach(function (name) { document.removeEventListener(name,activity); });
  }
  function confirmRender() {
    requestAnimationFrame(function () { requestAnimationFrame(function () {
      if (verified) return;
      verified = true;
      window.tpTrack('browser_verified', navigator.webdriver === true ? 'webdriver' : '');
      clock = performance.now();
      timer = setInterval(function () {
        const current = performance.now();
        if (document.visibilityState === 'visible') visibleMs += Math.min(1000, Math.max(0,current-clock));
        clock = current;
        checkEngagement();
      },1000);
    }); });
  }
  ['pointerdown','keydown','scroll'].forEach(function (name) { document.addEventListener(name,activity,{passive:true}); });
  document.addEventListener('visibilitychange',function () { clock = performance.now(); });
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', confirmRender, {once:true});
  } else {
    confirmRender();
  }
  document.addEventListener('click', function (e) {
    const link = e.target.closest('a[href]');
    if (link && new URL(link.href, location.href).pathname === '/signup') window.tpTrack('trial_click');
  });
}());
