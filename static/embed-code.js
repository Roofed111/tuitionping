(function () {
  'use strict';
  const button=document.getElementById('embed-copy'), area=document.getElementById('embed-code'), status=document.getElementById('embed-copy-status');
  if (!button || !area || !status) return;
  button.addEventListener('click',async()=>{
    try {
      await navigator.clipboard.writeText(area.value); status.textContent='Embed code copied.';
      try { if (window.tpTrack) window.tpTrack('embed_code_copied'); } catch (_) {}
    } catch (_) { area.focus(); area.select(); status.textContent='The code is selected. Use your browser’s Copy command.'; }
  });
}());
