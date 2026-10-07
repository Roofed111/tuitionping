(function () {
  'use strict';
  const video = document.getElementById('walkthrough-video');
  const button = document.getElementById('walkthrough-play');
  const status = document.getElementById('walkthrough-status');
  if (!video || !button || !status) return;
  let started = false;
  let completed = false;
  let watched = 0;
  let lastTime = 0;
  const track = event => { if (typeof window.tpTrack === 'function') window.tpTrack(event); };
  button.hidden = false;
  button.addEventListener('click', function () {
    const playing = video.play();
    if (playing && typeof playing.catch === 'function') playing.catch(function () {
      status.textContent = 'Use the video controls to play, or download the video below.';
    });
  });
  video.addEventListener('play', function () {
    button.hidden = true;
    status.textContent = '';
    lastTime = video.currentTime;
    if (!started) { started = true; track('video_started'); }
  });
  video.addEventListener('timeupdate', function () {
    const delta = video.currentTime - lastTime;
    if (!video.seeking && !video.paused && delta > 0 && delta < 2) watched += delta;
    lastTime = video.currentTime;
  });
  video.addEventListener('seeked', function () { lastTime = video.currentTime; });
  video.addEventListener('ended', function () {
    // A jump to the end alone is not a completed viewing.
    if (!completed && Number.isFinite(video.duration) && watched >= video.duration * .85) {
      completed = true; track('video_completed');
    }
  });
  video.addEventListener('error', function () {
    button.hidden = true;
    status.textContent = 'The video could not load. Download it below or read the walkthrough.';
  });
}());
