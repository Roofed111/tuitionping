const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {JSDOM} = require('jsdom');
const script = fs.readFileSync('static/growth.js', 'utf8');

function browser(options = {}) {
  const dom = new JSDOM('<meta name="tp-analytics-token" content="signed-token"><a href="/signup">Start</a>', {url:'https://www.tuitionping.com/demo', runScripts:'outside-only'});
  const w = dom.window, frames = [], calls = [];
  w.requestAnimationFrame = fn => frames.push(fn);
  w.fetch = (url, config) => { calls.push({url,config,body:JSON.parse(config.body)}); return Promise.resolve({status:204}); };
  if (options.noToken) w.document.querySelector('meta').remove();
  for (const key of ['doNotTrack','globalPrivacyControl','webdriver']) {
    if (key in options) Object.defineProperty(w.navigator, key, {value: options[key]});
  }
  w.eval(script);
  return {w, calls, frames, render() {
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
