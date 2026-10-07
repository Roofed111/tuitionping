"""Public partner toolkit and anonymous partner-to-account attribution."""
import io
import re
import zipfile
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from urllib.parse import urlencode
from fastapi import Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
import store
import growth
import traffic

ROOT = Path(__file__).resolve().parent
KINDS = {'association': 'Childcare association', 'provider_group': 'Provider group', 'bookkeeper': 'Childcare bookkeeper'}

def ensure_tables():
    growth.ensure_tables()

def get_partner(code):
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{2,47}', code or ''):
        return None
    ensure_tables()
    with store.db() as conn:
        row = conn.execute('SELECT * FROM growth_partners WHERE code=?', (code,)).fetchone()
    return dict(row) if row else None

def touch(visitor, code):
    if not visitor or not get_partner(code):
        return
    with store.db() as conn:
        conn.execute('INSERT INTO growth_partner_visitors (visitor_id,partner_code,touched_at) VALUES (?,?,?) ON CONFLICT(visitor_id) DO UPDATE SET partner_code=excluded.partner_code,touched_at=excluded.touched_at',
                     (visitor, code, store.now_iso()))

def bind_account(visitor, pid):
    ensure_tables()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec='seconds')
    with store.db() as conn:
        row = conn.execute('SELECT partner_code FROM growth_partner_visitors WHERE visitor_id=? AND touched_at>=?', (visitor, cutoff)).fetchone()
        if row:
            conn.execute('INSERT INTO growth_partner_accounts (provider_id,visitor_id,partner_code,bound_at) VALUES (?,?,?,?) ON CONFLICT(provider_id) DO NOTHING',
                         (pid, visitor, row['partner_code'], store.now_iso()))

def link(code='', path='/partners'):
    base = 'https://www.tuitionping.com' + path
    if not code:
        return base
    return base + '?' + urlencode({'partner': code, 'utm_source': code, 'utm_medium': 'partner', 'utm_campaign': 'collection-kit'})

def report(days=28):
    ensure_tables()
    traffic.refresh_candidates()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec='seconds')
    with store.db() as conn:
        partners = [dict(r) for r in conn.execute('SELECT * FROM growth_partners ORDER BY created_at DESC').fetchall()]
        touches = [dict(r) for r in conn.execute("SELECT t.* FROM growth_partner_visitors t JOIN growth_visitors v ON t.visitor_id=v.visitor_id WHERE t.touched_at>=? AND v.classification IN ('HUMAN','LIKELY HUMAN')", (cutoff,)).fetchall()]
        accounts = [dict(r) for r in conn.execute("SELECT a.* FROM growth_partner_accounts a JOIN growth_visitors v ON a.visitor_id=v.visitor_id WHERE a.bound_at>=? AND v.classification IN ('HUMAN','LIKELY HUMAN')", (cutoff,)).fetchall()]
        events = [dict(r) for r in conn.execute("SELECT e.* FROM growth_events e JOIN growth_visitors v ON e.visitor_id=v.visitor_id WHERE v.classification IN ('HUMAN','LIKELY HUMAN') AND (EXISTS (SELECT 1 FROM growth_partner_visitors t WHERE t.visitor_id=e.visitor_id AND t.touched_at>=?) OR EXISTS (SELECT 1 FROM growth_partner_accounts a WHERE a.provider_id=e.provider_id AND a.bound_at>=?))", (cutoff,cutoff)).fetchall()]
    for partner in partners:
        vids = {r['visitor_id'] for r in touches if r['partner_code'] == partner['code']}
        pids = {r['provider_id'] for r in accounts if r['partner_code'] == partner['code']}
        partner.update(visitors=len(vids), demo=len({e['visitor_id'] for e in events if e['visitor_id'] in vids and e['event']=='demo_started'}),
                       downloads=len({e['visitor_id'] for e in events if e['visitor_id'] in vids and e['event']=='download'}),
                       signups=len(pids), trials=len({e['provider_id'] for e in events if e['provider_id'] in pids and e['event']=='trial_started'}),
                       activated=len({e['provider_id'] for e in events if e['provider_id'] in pids and e['event']=='first_reminder'}),
                       paid=len({e['provider_id'] for e in events if e['provider_id'] in pids and e['event']=='paid_customer'}),
                       url=link(partner['code']), kit_url=link(partner['code'], '/partners/download'), kind_label=KINDS.get(partner['kind'],partner['kind']))
    return partners

def package(code=''):
    url = link(code)
    resources = [('Collection workflow', '/guides/tuition-collection'), ('Invoice and receipt generator', '/tools/daycare-invoice-receipt'),
                 ('Payment tracker', '/tools/tuition-payment-tracker'), ('Reminder templates', '/guides/tuition-reminder-templates'),
                 ('Interactive demo', '/demo')]
    # All distributed resource links return to the partner landing first, so a
    # new browser establishes attribution before following the chosen tool.
    links = [(label, url + ('&' if code else '?') + 'resource=' + path.rsplit('/', 1)[-1]) for label, path in resources]
    tutorial_link = url + ('&' if code else '?') + 'resource=invoice-tutorial'
    links.append(('Two-minute invoice and receipt video tutorial', tutorial_link))
    readme = f'''TUITIONPING - PARTNER RESOURCE PACKAGE

Share this link with providers: {url}
Use Start-here.html for a printable handout, or copy the ready-to-share text
below. A tracked partner link is available in TuitionPing Admin > Partners.
Keep your assigned link when sharing; a generic link has no partner credit.
Do not put private names, emails or family data into link parameters.

WHAT PROVIDERS GET
- A bill-to-receipt workflow and monthly collection checklist
- A browser-only invoice / verified-payment receipt generator (print to PDF)
- A two-minute video showing a fictional invoice and partial-payment receipt
- Editable Word and PDF reminder messages in English and Spanish
- An Excel payment tracker and an editable late-fee policy
- A fictional interactive TuitionPing demo; it sends no texts

HOW TO SHARE
Video tutorial and free tool: {tutorial_link}
YouTube viewing link: https://youtu.be/B17VIKwHMJI
Share the assigned tutorial link first to preserve partner attribution.
Associations: include the newsletter paragraph or handout in your resources.
Provider groups: share the short post or use the 15-minute session outline.
Bookkeepers: share with clients who need clearer bills and payment records.
You may share these included resources free with providers. Keep the source
credit and describe any modifications as yours. Redistribution is not an
endorsement by your organization, and this kit includes no referral payout.

Providers customize their own policies and verify their own payments.
TuitionPing schedules reminder texts; it does not process tuition payments.
The product trial requires a card at checkout. The free tools do not.
Only verified marketing opt-ins join the separate email tips list.

ATTRIBUTION
The latest registered partner landing within 30 days before signup is saved
for that new account. Admin > Partners reports visitors, demo use, downloads,
accounts, trials, first accepted reminders and paid subscriptions. This is
browser attribution, not proof a partner caused a sale. DNT/GPC, blocked
cookies, shared devices and a different signup browser can affect counts.
Reports use a 28- or 90-day window; audit history is retained. Human and likely
human traffic counts in customer reports. First-touch Conversions remains
separate. Never promise a result or invent provider success statistics.
'''
    copy = f'''NEWSLETTER PARAGRAPH
Make tuition collection easier to follow. This free TuitionPing resource
package includes editable payment policies, reminder texts, an Excel payment
tracker, and a tool to create tuition invoices or verified-payment receipts.
No account is needed for the tools. You can also explore a fictional daycare
demo to see how scheduled reminders and payment confirmation work.
Get the free resources: {url}
Watch the two-minute invoice and receipt tutorial: {tutorial_link}

SHORT GROUP POST
Chasing tuition? Start with a clear bill, a friendly reminder, and a record
of verified payments. Here is a free collection toolkit for daycare owners:
{url}
Need a walkthrough? Watch the free invoice and receipt tutorial: {tutorial_link}

BOOKKEEPER CLIENT NOTE
These tools can help organize tuition bills and payment documentation. Use
the receipt generator only after confirming the amount received, and keep
the saved document with the client's payment records. Free toolkit: {url}
Show clients the two-minute invoice and partial-payment receipt tutorial:
{tutorial_link}

15-MINUTE PROVIDER SESSION
0-3 min: Write down tuition amount, service period, due date and how to pay.
3-6 min: Create an invoice using fictional data; check charges and credits.
Use the two-minute video as a walkthrough: {tutorial_link}
6-9 min: Choose a reminder message and check the due date and language.
9-12 min: Reconcile a sample partial payment; issue a verified receipt.
12-15 min: Walk through the fictional demo; show PAID versus confirmed.
Do not screen-share real family records or text actual parents in a workshop.
'''
    checklist = '''MONTHLY TUITION COLLECTION CHECKLIST
Before billing: Confirm period, due date, agreed tuition and payment method.
Prepare bill: List assessed charges and approved credits; give it a number.
Before reminder: Check verified payments; calculate the balance remaining.
Follow up: Send the right message privately, in the family's chosen language.
After PAID report: Check amount, date, reference and destination in your records.
After verification: Create a receipt and update the ledger. Keep partial
payments separate so remaining balances stay clear.
End of cycle: Reconcile documents to received payments and resolve exceptions.
Keep real family records private; store saved PDFs with restricted access.

WORKED EXAMPLE
$800 tuition + $25 assessed fee - $25 approved credit = $800 bill.
$200 earlier verified payment + $300 verified today = $500 paid.
Today's receipt records $300 received; remaining balance is $300.
A parent reporting PAID alone does not reduce that $300 balance.
'''
    html = '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Free daycare tuition collection toolkit</title><style>body{font:17px/1.6 system-ui,sans-serif;max-width:760px;margin:50px auto;padding:24px;color:#17342b}h1{font-size:32px;line-height:1.2}a{color:#077a5c;overflow-wrap:anywhere}.box{border:1px solid #b7d8cb;border-radius:12px;padding:22px;margin:24px 0}small{font-size:13px}@media print{body{margin:0;max-width:none}}</style><body><p>TuitionPing · Free provider resources</p><h1>A clear bill. A friendly reminder. A verified receipt.</h1><p>Build a repeatable tuition collection process without changing your payment method.</p><div class="box"><h2>Get the free collection toolkit</h2><p><a href="' + escape(url,quote=True) + '">' + escape(url) + '</a></p><ul>' + ''.join('<li><a href="'+escape(target,quote=True)+'">'+escape(label)+'</a></li>' for label,target in links) + '</ul></div><h2>Use the bill-to-receipt workflow</h2><ol><li>Agree on the period, amount, due date and payment method.</li><li>Create a clear invoice and record assessed charges and credits.</li><li>Send reminders only when your records show a balance.</li><li>Verify each payment before issuing a receipt.</li><li>Reconcile your ledger at the end of the billing cycle.</li></ol><p><small>Free tools need no account. The interactive demo uses fictional families. TuitionPing schedules reminder texts; it does not collect tuition or independently verify payments.</small></p></body></html>'
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('READ-ME.txt',readme)
        archive.writestr('Ready-to-share-copy.txt',copy)
        archive.writestr('Monthly-collection-checklist.txt',checklist)
        archive.writestr('Start-here.html',html)
        for name in ['daycare-late-fee-policy.pdf','daycare-late-fee-policy.docx','daycare-tuition-reminders.pdf','daycare-tuition-reminders.docx','daycare-tuition-payment-tracker.xlsx']:
            archive.write(ROOT / 'static' / 'downloads' / name, name)
    return data.getvalue()

def register(app, templates, require_admin):
    @app.get('/partners', response_class=HTMLResponse)
    def partners(request: Request, partner: str = '', resource: str = ''):
        selected = get_partner(partner)
        paths = {'tuition-collection':'/guides/tuition-collection', 'daycare-invoice-receipt':'/tools/daycare-invoice-receipt',
                 'tuition-payment-tracker':'/tools/tuition-payment-tracker', 'tuition-reminder-templates':'/guides/tuition-reminder-templates', 'demo':'/demo',
                 'invoice-tutorial':'/tools/daycare-invoice-receipt#invoice-tutorial'}
        return templates.TemplateResponse(request, 'partners.html', {'request':request,'provider':None,'partner':selected,
            'share_url':link(selected['code'] if selected else ''), 'kit_url':link(selected['code'] if selected else '', '/partners/download'),
            'suggested_path':paths.get(resource),'suggested_label':resource.replace('-',' ') if resource in paths else ''})

    @app.get('/partners/download')
    def download(request: Request, partner: str = ''):
        selected = get_partner(partner)
        code = selected['code'] if selected else ''
        response = Response(package(code),media_type='application/zip',headers={
            'Content-Disposition':'attachment; filename="tuitionping-partner-kit'+('-'+code if code else '')+'.zip"',
            'Cache-Control':'private, no-store','X-Robots-Tag':'noindex'})
        visitor = getattr(request.state,'growth_visitor','')
        if visitor and not growth.excluded(request):
            try:
                growth.record(visitor,'download','partner-kit','/partners/download')
            except Exception:
                pass
        return response

    @app.get('/admin/partners', response_class=HTMLResponse)
    def admin(request: Request, days: int = 28, error: str = ''):
        provider, redirect = require_admin(request)
        if redirect:
            return redirect
        response = templates.TemplateResponse(request,'admin_partners.html',{'request':request,'provider':provider,
            'partners':report(90 if days==90 else 28),'days':90 if days==90 else 28,'kinds':KINDS,'error':error})
        response.headers.update({'Cache-Control':'private, no-store','X-Robots-Tag':'noindex'})
        return response

    @app.post('/admin/partners')
    def create(request: Request, name: str = Form(''), code: str = Form(''), kind: str = Form('')):
        _, redirect = require_admin(request)
        if redirect:
            return redirect
        name, code = name.strip(), code.strip().lower()
        if not name or len(name)>120 or not re.fullmatch(r'[a-z0-9][a-z0-9-]{2,47}',code) or kind not in KINDS:
            return RedirectResponse('/admin/partners?error=invalid',status_code=303)
        ensure_tables()
        with store.db() as conn:
            existing = conn.execute('SELECT code FROM growth_partners WHERE code=?',(code,)).fetchone()
            if existing:
                return RedirectResponse('/admin/partners?error=duplicate',status_code=303)
            conn.execute('INSERT INTO growth_partners (code,name,kind,created_at) VALUES (?,?,?,?) ON CONFLICT(code) DO NOTHING',(code,name,kind,store.now_iso()))
        return RedirectResponse('/admin/partners',status_code=303)
