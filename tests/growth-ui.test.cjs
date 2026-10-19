const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {JSDOM} = require('jsdom');
const script = fs.readFileSync('static/growth.js', 'utf8');

function browser(options = {}) {
  const dom = new JSDOM('<meta name="tp-analytics-token" content="signed-token"><a href="/signup">Start</a>', {url:'https://www.tuitionping.com/demo', runScripts:'outside-only'});
  const w = dom.window, frames = [], calls = [], handlers = {}, intervals = new Map();
  let time = 0, intervalId = 0;
  Object.defineProperty(w.document,'visibilityState',{value:'visible',configurable:true});
  w.performance.now = () => time;
  w.setInterval = fn => { intervals.set(++intervalId,fn);return intervalId; };
  w.clearInterval = id => intervals.delete(id);
  const add = w.document.addEventListener.bind(w.document);
  w.document.addEventListener = (name,fn,config) => { (handlers[name] ||= []).push(fn);add(name,fn,config); };
  w.requestAnimationFrame = fn => frames.push(fn);
  w.fetch = (url, config) => { calls.push({url,config,body:JSON.parse(config.body)}); return Promise.resolve({status:204}); };
  if (options.noToken) w.document.querySelector('meta').remove();
  for (const key of ['doNotTrack','globalPrivacyControl','webdriver']) {
    if (key in options) Object.defineProperty(w.navigator, key, {value: options[key]});
  }
  w.eval(script);
  return {w, calls, frames, activity(type,isTrusted=true) {
    (handlers[type] || []).forEach(fn=>fn({type,isTrusted,target:w.document.body}));
  }, tick(seconds) {
    for(let n=0;n<seconds;n++){ time+=1000;for(const fn of intervals.values())fn(); }
  }, render() {
    w.document.dispatchEvent(new w.Event('DOMContentLoaded'));
    while (frames.length) frames.shift()();
  }};
}

test('verification follows two render frames, uses the existing token, and sends once', () => {
  const p = browser();
  assert.equal(p.calls.length, 0);
  p.w.document.dispatchEvent(new p.w.Event('DOMContentLoaded'));
  assert.equal(p.frames.length, 1);
  p.frames.shift()();
  assert.equal(p.calls.length, 0);
  p.frames.shift()();
  assert.deepEqual(p.calls[0].body, {event:'browser_verified',detail:'',path:'/demo'});
  assert.equal(p.calls[0].config.credentials, 'same-origin');
  assert.equal(p.calls[0].config.headers['X-TP-Analytics'], 'signed-token');
  p.render();
  assert.equal(p.calls.length, 1);
  assert.equal(p.calls.filter(c => c.body.event === 'page_view').length, 0);
  p.w.close();
});

test('rendering or waiting alone never counts as engagement; trusted one-page interaction does', () => {
  const p=browser();p.render();p.tick(10);
  assert.equal(p.calls.length,1);
  p.activity('pointerdown',false);
  assert.equal(p.calls.length,1);
  p.activity('pointerdown');
  assert.deepEqual(p.calls[1].body,{event:'visitor_engaged',detail:'interaction',path:'/demo'});
  p.tick(60);p.activity('keydown');
  assert.equal(p.calls.filter(c=>c.body.event==='visitor_engaged').length,1);
  assert.equal(p.calls.filter(c=>c.body.event==='page_view').length,0);
  p.w.close();
});

test('hidden time does not qualify and early interaction waits for eight visible seconds', () => {
  const p=browser();p.render();p.activity('keydown');p.tick(7);
  assert.equal(p.calls.length,1);
  Object.defineProperty(p.w.document,'visibilityState',{value:'hidden',configurable:true});
  p.tick(60);assert.equal(p.calls.length,1);
  Object.defineProperty(p.w.document,'visibilityState',{value:'visible',configurable:true});
  p.tick(1);assert.equal(p.calls[1].body.event,'visitor_engaged');p.w.close();
});

test('scroll reading requires thirty visible seconds and webdriver cannot engage', () => {
  const p=browser();p.render();p.activity('scroll');p.tick(29);
  assert.equal(p.calls.length,1);p.tick(1);
  assert.equal(p.calls[1].body.detail,'active_reading');p.w.close();
  const automated=browser({webdriver:true});automated.render();automated.activity('pointerdown');automated.tick(45);
  assert.equal(automated.calls.length,1);automated.w.close();
});

test('privacy preferences and missing tokens skip browser verification', () => {
  for (const options of [{doNotTrack:'1'}, {globalPrivacyControl:true}, {noToken:true}]) {
    const p=browser(options);p.render();assert.equal(p.calls.length,0);assert.equal(p.w.tpTrack,undefined);p.w.close();
  }
});

test('webdriver is an explicit automation signal and existing trial events still work', () => {
  const p=browser({webdriver:true});p.render();
  assert.equal(p.calls[0].body.detail,'webdriver');
  p.w.document.querySelector('a').dispatchEvent(new p.w.MouseEvent('click',{bubbles:true}));
  assert.equal(p.calls[1].body.event,'trial_click');p.w.close();
});

test('verification works when the script loads after document completion and failures stay silent', async () => {
  const p=browser();
  p.render();p.calls.length=0;
  Object.defineProperty(p.w.document,'readyState',{value:'complete'});
  p.w.fetch=()=>Promise.reject(new Error('offline'));
  p.w.eval(script);
  while(p.frames.length)p.frames.shift()();
  await Promise.resolve();await Promise.resolve();
  assert.equal(typeof p.w.tpTrack,'function');p.w.close();
});
