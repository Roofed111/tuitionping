const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {JSDOM} = require('jsdom');

function player() {
  const dom = new JSDOM('<video id="walkthrough-video"></video><button id="walkthrough-play" hidden>Play</button><p id="walkthrough-status"></p>', {runScripts:'outside-only'});
  const {window:w} = dom, v=w.document.querySelector('video'), button=w.document.querySelector('button');
  let paused=true, seeking=false;
  Object.defineProperty(v,'duration',{value:48});
  Object.defineProperty(v,'paused',{get:()=>paused});
  Object.defineProperty(v,'seeking',{get:()=>seeking});
  const events=[];
  w.tpTrack=e=>events.push(e);
  const emit=e=>v.dispatchEvent(new w.Event(e));
  v.play=()=>{paused=false;emit('play');return Promise.resolve();};
  w.eval(fs.readFileSync('static/walkthrough.js','utf8'));
  return {w,v,button,events,emit,status:w.document.querySelector('p'),
    pause:()=>{paused=true;},seek:time=>{seeking=true;v.currentTime=time;emit('timeupdate');seeking=false;emit('seeked');}};
}

test('user playback works and engagement events occur once after a full viewing',()=>{
  const p=player();
  assert.equal(p.button.hidden,false);
  p.button.click();
  assert.equal(p.button.hidden,true);
  for(let t=.5;t<=48;t+=.5){p.v.currentTime=t;p.emit('timeupdate');}
  p.pause();p.emit('ended');p.emit('ended');p.emit('play');
  assert.deepEqual(p.events,['video_started','video_completed']);
  p.w.close();
});

test('seeking directly to the end does not claim a completed viewing',()=>{
  const p=player();p.button.click();p.seek(48);p.pause();p.emit('ended');
  assert.deepEqual(p.events,['video_started']);p.w.close();
});

test('playback remains usable without analytics and errors retain download/transcript guidance',async()=>{
  const p=player();delete p.w.tpTrack;p.button.click();p.emit('error');
  assert.match(p.status.textContent,/Download it below or read/);
  p.v.play=()=>Promise.reject(new Error('Playback blocked'));p.button.click();
  await Promise.resolve();await Promise.resolve();
  assert.match(p.status.textContent,/Use the video controls/);p.w.close();
});
