const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {execFileSync} = require('node:child_process');
const {JSDOM} = require('jsdom');

const rendered = new Map();
function partial(kind) {
  if (!rendered.has(kind)) rendered.set(kind, execFileSync('python3', ['-c',
    "from jinja2 import Environment, FileSystemLoader; import sys; print(Environment(loader=FileSystemLoader('templates'), autoescape=True).get_template('resource_next_step.html').render(next_step=sys.argv[1], plans={'micro':{'price':9,'families':10}}, trial_days=30))", kind], {encoding:'utf8'}));
  return rendered.get(kind);
}
function setup(kind) {
  const info = {
    reminder: ['guide_templates.html', '/guides/tuition-reminder-templates', 'reminder-generator.js'],
    tracker: ['tool_payment_tracker.html', '/tools/tuition-payment-tracker'],
    document: ['tool_invoice.html', '/tools/daycare-invoice-receipt', 'invoice.js']
  }[kind];
  const html = fs.readFileSync('templates/' + info[0], 'utf8').replace(
    /{% with next_step='[^']+' %}{% include "resource_next_step.html" %}{% endwith %}/, partial(kind));
  const dom = new JSDOM(html, {url:'https://www.tuitionping.com' + info[1], runScripts:'outside-only'});
  const w = dom.window, d = w.document, events = [], $ = id => d.getElementById(id);
  w.tpTrack = (...args) => events.push(args);
  w.fetch = () => { throw Error('Tool entries must stay in the page'); };
  Object.defineProperty(w, 'localStorage', {get() { throw Error('Tool entries must not be persisted'); }});
  w.eval(fs.readFileSync('static/resource-next-step.js', 'utf8'));
  if (info[2]) w.eval(fs.readFileSync('static/' + info[2], 'utf8'));
  // Observe clicks after the production handler, then suppress jsdom navigation.
  d.addEventListener('click', e => {if (e.target.closest('a[href]')) e.preventDefault();});
  const submit = id => $(id).dispatchEvent(new w.Event('submit', {bubbles:true, cancelable:true}));
  return {dom, w, d, events, $, submit, panel:$('resource-next-step')};
}

test('a page load, sample load and invalid reminder do not reveal a prompt or count use', () => {
  const {dom, $, submit, events, panel} = setup('reminder');
  assert.equal(panel.hidden, true); submit('reminder-form'); assert.equal(panel.hidden, true);
  $('reminder-example').click(); assert.equal(panel.hidden, true); assert.deepEqual(events, []);
  dom.window.close();
});

test('a valid reminder reveals the next step once and edits hide stale success', () => {
  const {dom, w, $, submit, events, panel} = setup('reminder');
  $('reminder-example').click(); submit('reminder-form'); assert.equal(panel.hidden, false);
  const output = $('reminder-output').value; submit('reminder-form');
  assert.equal(events.filter(e => e[0] === 'resource_next_step').length, 1);
  assert.equal($('reminder-output').value, output);
  $('reminder-amount').value = '450'; $('reminder-amount').dispatchEvent(new w.Event('input', {bubbles:true}));
  assert.equal(panel.hidden, true); submit('reminder-form'); assert.equal(panel.hidden, false);
  $('reminder-reset').click(); assert.equal(panel.hidden, true);
  assert.equal(events.filter(e => e[0] === 'resource_next_step').length, 1);
  dom.window.close();
});

test('tracker download clicks reveal one prompt and only its demo button counts that next step', () => {
  const {dom, d, w, panel, events} = setup('tracker');
  assert.equal(panel.hidden, true);
  d.querySelector('a[href="/demo"]').click(); assert.deepEqual(events, []);
  const download = d.querySelector('a[download][href$=".xlsx"]'); download.click(); download.click();
  assert.equal(panel.hidden, false);
  panel.querySelector('[data-resource-action="demo"]').click(); panel.querySelector('[data-resource-action="demo"]').click();
  assert.deepEqual(events, [['resource_next_step','tracker'], ['resource_demo_click','tracker']]);
  assert.equal(w.location.search, '');
  assert.equal(download.getAttribute('href'), '/static/downloads/daycare-tuition-payment-tracker.xlsx');
  dom.window.close();
});

test('invoice and verified receipt display their own next step after successful generation', () => {
  const {dom, w, $, submit, panel, events} = setup('document');
  $('invoice-example').click(); assert.equal(panel.hidden, true);
  submit('invoice-form'); assert.equal(panel.hidden, false); assert.equal($('invoice-document').hidden, false);
  assert.equal(panel.querySelector('[data-next-invoice]').hidden, false);
  $('document-type').value = 'receipt'; $('document-type').dispatchEvent(new w.Event('change', {bubbles:true}));
  assert.equal(panel.hidden, true); submit('invoice-form'); assert.equal(panel.hidden, true);
  $('verified').checked = true; submit('invoice-form');
  assert.equal(panel.hidden, false); assert.equal(panel.querySelector('[data-next-invoice]').hidden, true);
  assert.equal(panel.querySelector('[data-next-receipt]').hidden, false);
  assert.equal(panel.getAttribute('aria-labelledby'), panel.querySelector('[data-next-receipt]').id);
  assert.deepEqual(events.filter(e => e[0] === 'resource_next_step'), [['resource_next_step','invoice'], ['resource_next_step','receipt']]);
  assert.equal(events.some(e => ['paid_customer', 'trial_started'].includes(e[0])), false);
  $('invoice-reset').click(); assert.equal(panel.hidden, true);
  dom.window.close();
});

test('only fixed labels leave the tool, even when private details contain markup', () => {
  const {dom, w, $, submit, events, panel} = setup('reminder');
  $('reminder-example').click(); $('reminder-program').value = '<img src=x onerror=alert(1)>';
  $('reminder-payment').value = 'Private bank reference PRIVATE-ABC'; submit('reminder-form');
  w.tpResourceNextStep('PRIVATE-ABC'); w.tpResourceNextStep('receipt');
  panel.querySelector('[data-resource-action="demo"]').click();
  assert.deepEqual(events, [['reminder_generated','before'], ['resource_next_step','reminder'], ['resource_demo_click','reminder']]);
  assert.equal(w.document.querySelector('img'), null);
  assert.equal(w.location.search, ''); assert.equal(w.location.hash, '');
  assert.equal(panel.querySelector('[data-resource-action="demo"]').getAttribute('href'), '/demo');
  dom.window.close();
});

test('tools and next steps still work with missing or failed analytics', () => {
  for (const kind of ['reminder', 'document']) for (const track of [undefined, () => {throw Error('Unavailable');}]) {
    const {dom, w, $, submit, panel} = setup(kind); w.tpTrack = track;
    $(kind === 'reminder' ? 'reminder-example' : 'invoice-example').click();
    submit(kind === 'reminder' ? 'reminder-form' : 'invoice-form'); assert.equal(panel.hidden, false);
    assert.equal($(kind === 'reminder' ? 'reminder-error' : 'invoice-error').hidden, true);
    dom.window.close();
  }
});
