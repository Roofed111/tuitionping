"""Public setup requests and a private support inbox; no customer messaging."""
import hashlib
import os
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.parse import quote, urlencode
from fastapi import Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
import store
import email_engagement
import growth

BASE = os.getenv('PUBLIC_BASE_URL', 'https://www.tuitionping.com').rstrip('/')
RECIPIENT = 'rob@tuitionping.com'
CONSENT = 'TuitionPing support may email me about this setup request.'
FAMILIES = {'1-10':'1–10 families', '11-25':'11–25 families', '26-75':'26–75 families', '76+':'76+ families', 'unsure':'Not sure yet'}
STAGES = {'exploring':'Exploring TuitionPing', 'account':'Already have an account'}
TOPICS = {'start':'Choosing a plan and getting started', 'csv':'Preparing or importing a CSV', 'messages':'Program details and reminder settings', 'test':'Previewing and sending a test text', 'payments':'Understanding PAID replies and payment confirmation'}
STATUSES = {'new':'New', 'contacted':'Replied', 'working':'Helping', 'complete':'Complete', 'closed':'Not proceeding'}
_ready = None
_lock = threading.Lock()

def now():
    return datetime.now(timezone.utc)

def stamp(dt=None):
    return (dt or now()).isoformat(timespec='seconds')

def ensure_tables():
    global _ready
    key = (store.USE_PG, store.DATABASE_URL, store.DB_PATH)
    if _ready == key:
        return
    with _lock:
        if _ready == key:
            return
        with store.db() as conn:
            conn.execute(store.pg_ddl('''CREATE TABLE IF NOT EXISTS setup_help_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT, request_key TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL, email TEXT NOT NULL, program TEXT NOT NULL,
                families TEXT NOT NULL, stage TEXT NOT NULL, topic TEXT NOT NULL,
                note TEXT NOT NULL, consent_text TEXT NOT NULL, created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'new', admin_note TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL, notification_status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, first_attempt_at TEXT,
                due_at TEXT NOT NULL, notified_at TEXT)'''))
            conn.execute('''CREATE TABLE IF NOT EXISTS setup_help_limits (
                key TEXT PRIMARY KEY, ts TEXT NOT NULL, count INTEGER NOT NULL)''')
        _ready = key

def rate_allowed(ip):
    ensure_tables()
    dt = now()
    key = hashlib.sha256((os.getenv('SECRET_KEY', 'setup-help-rate') + ip + dt.strftime('%Y-%m-%d-%H')).encode()).hexdigest()
    with store.db() as conn:
        conn.execute('DELETE FROM setup_help_limits WHERE ts < ?', (stamp(dt-timedelta(days=2)),))
        conn.execute('INSERT INTO setup_help_limits (key,ts,count) VALUES (?,?,0) ON CONFLICT(key) DO NOTHING', (key,stamp(dt)))
        changed = conn.execute('UPDATE setup_help_limits SET count=count+1 WHERE key=? AND count<5', (key,))
    return changed.rowcount > 0

def create(data):
    ensure_tables()
    values = {k: str(data.get(k, '')).strip() for k in ['name','email','program','families','stage','topic','note','request_key','contact_permission']}
    for key, maximum in [('name',100),('program',150),('note',1000)]:
        if len(values[key]) > maximum or (key == 'name' and not values[key]):
            raise ValueError('Enter your name and keep entries within the displayed limits.')
    values['email'] = email_engagement.normalize_email(values['email'])
    if values['families'] not in FAMILIES or values['stage'] not in STAGES or values['topic'] not in TOPICS:
        raise ValueError('Choose a family count, account stage and setup topic.')
    if values['contact_permission'] != 'on':
        raise ValueError('Confirm that TuitionPing support may email you about this request.')
    if not re.fullmatch(r'[a-f0-9]{32}', values['request_key']):
        raise ValueError('Reload this page and submit the form again.')
    with store.db() as conn:
        cur = conn.execute('''INSERT INTO setup_help_requests
            (request_key,name,email,program,families,stage,topic,note,consent_text,created_at,updated_at,due_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(request_key) DO NOTHING''',
            tuple(values[k] for k in ['request_key','name','email','program','families','stage','topic','note']) + (CONSENT,stamp(),stamp(),stamp()))
        row = conn.execute('SELECT id FROM setup_help_requests WHERE request_key=?',(values['request_key'],)).fetchone()
    return row['id'], cur.rowcount > 0

def report(offset=0):
    ensure_tables()
    offset = max(0, min(offset, 1000000))
    with store.db() as conn:
        rows = [dict(r) for r in conn.execute('SELECT * FROM setup_help_requests ORDER BY id DESC LIMIT 101 OFFSET ?', (offset,)).fetchall()]
        counts = {r['status']:r['n'] for r in conn.execute('SELECT status,COUNT(*) AS n FROM setup_help_requests GROUP BY status').fetchall()}
    for row in rows:
        row['reply_url'] = 'mailto:' + quote(row['email'],safe='@') + '?' + urlencode({'subject':'Your TuitionPing setup request'})
    return {'requests':rows[:100], 'counts':counts, 'recipient':RECIPIENT, 'offset':offset,
            'previous':max(0,offset-100), 'next':offset+100 if len(rows)>100 else None}

def update(rid, status, note):
    if status not in STATUSES or len(note) > 2000:
        raise ValueError('Choose a valid status and keep your note under 2,000 characters.')
    ensure_tables()
    with store.db() as conn:
        cur = conn.execute('UPDATE setup_help_requests SET status=?,admin_note=?,updated_at=? WHERE id=?',(status,note.strip(),stamp(),rid))
    return cur.rowcount > 0

def notify(sender, active, limit=10, only_id=None):
    """Durable support notifications with atomic claims and stable keys.

    A stale claim is retried with the same Resend key within its 24-hour window;
    older uncertain sends require provider review and are never blindly resent.
    """
    ensure_tables()
    if not active:
        return {'accepted':0,'pending':True}
    dt = now()
    with store.db() as conn:
        conn.execute("UPDATE setup_help_requests SET notification_status='uncertain' WHERE notification_status IN ('pending','sending') AND first_attempt_at IS NOT NULL AND first_attempt_at<?",(stamp(dt-timedelta(hours=23)),))
        conn.execute("UPDATE setup_help_requests SET notification_status='pending' WHERE notification_status='sending' AND due_at<=?",(stamp(dt),))
        query = "SELECT * FROM setup_help_requests WHERE notification_status='pending' AND due_at<=?"
        args = [stamp(dt)]
        if only_id is not None:
            query += ' AND id=?';args.append(only_id)
        rows = [dict(r) for r in conn.execute(query+' ORDER BY id LIMIT ?',tuple(args+[limit])).fetchall()]
    accepted = 0
    for row in rows:
        with store.db() as conn:
            claimed = conn.execute("UPDATE setup_help_requests SET notification_status='sending', attempts=attempts+1,first_attempt_at=COALESCE(first_attempt_at,?),due_at=? WHERE id=? AND notification_status='pending' AND due_at<=?",(stamp(dt),stamp(dt+timedelta(minutes=5)),row['id'],stamp(dt)))
        if not claimed.rowcount:
            continue
        details = [('Name',row['name']),('Email',row['email']),('Program',row['program'] or 'Not provided'),('Families',FAMILIES[row['families']]),('Stage',STAGES[row['stage']]),('Help requested',TOPICS[row['topic']]),('Note',row['note'] or 'None')]
        body = '<h1>New TuitionPing setup-help request</h1>' + ''.join('<p><strong>'+escape(k)+':</strong> '+escape(v).replace('\n','<br>')+'</p>' for k,v in details)
        body += '<p><a href="'+escape(BASE+'/admin/setup-help')+'">Open the private setup inbox and follow-up guide</a></p>'
        ok = False
        try:
            ok = bool(sender(RECIPIENT,'New TuitionPing setup-help request',body,idempotency_key='setup-help-'+row['request_key']))
        except Exception:
            pass
        with store.db() as conn:
            conn.execute('UPDATE setup_help_requests SET notification_status=?,notified_at=?,due_at=? WHERE id=? AND notification_status=?',('accepted' if ok else 'pending',stamp() if ok else None,stamp(now()+timedelta(minutes=15)),row['id'],'sending'))
        accepted += int(ok)
    return {'accepted':accepted}

def register(app, templates, require_admin, sender, email_active):
    def page(request, template, data=None, status=200):
        return templates.TemplateResponse(request,template,{'request':request,'provider':None,'families_options':FAMILIES,'stage_options':STAGES,'topics':TOPICS,'statuses':STATUSES,**(data or {})},status_code=status,
            headers={'Cache-Control':'private, no-store','X-Robots-Tag':'noindex','Referrer-Policy':'no-referrer'})

    @app.get('/setup-help',response_class=HTMLResponse)
    def form(request: Request):
        return page(request,'setup_help.html',{'values':{},'request_key':secrets.token_hex(16)})

    @app.post('/setup-help',response_class=HTMLResponse)
    async def submit(request: Request):
        data = dict(await request.form())
        if str(data.get('website','')).strip():
            return page(request,'setup_help.html',{'sent':True})
        ip = request.headers.get('x-forwarded-for',request.client.host if request.client else '').split(',')[0].strip()
        if not rate_allowed(ip):
            return page(request,'setup_help.html',{'error':'Too many requests. Try again later, or contact TuitionPing support.','values':data,'request_key':secrets.token_hex(16)},429)
        try:
            rid, created = create(data)
        except ValueError as exc:
            return page(request,'setup_help.html',{'error':str(exc),'values':data,'request_key':secrets.token_hex(16)},400)
        if created:
            visitor = getattr(request.state,'growth_visitor','')
            if visitor:
                try:
                    growth.record(visitor,'setup_help_requested',path='/setup-help')
                except Exception:
                    pass
            try:
                notify(sender,email_active(),only_id=rid)
            except Exception:
                pass  # The saved request is always available in the admin inbox.
        return page(request,'setup_help.html',{'sent':True})

    @app.get('/admin/setup-help',response_class=HTMLResponse)
    def inbox(request: Request, offset: int = 0):
        _, redirect = require_admin(request)
        if redirect:
            return redirect
        return page(request,'admin_setup_help.html',{'report':report(offset),'email_active':email_active()})

    @app.post('/admin/setup-help/update')
    def save(request: Request, request_id: int = Form(...), status: str = Form(...), admin_note: str = Form('')):
        _, redirect = require_admin(request)
        if redirect:
            return redirect
        try:
            found = update(request_id,status,admin_note)
        except ValueError as exc:
            return HTMLResponse(str(exc),status_code=400)
        if not found:
            return HTMLResponse('Request not found.',status_code=404)
        return RedirectResponse('/admin/setup-help?saved=1',status_code=303)

    @app.post('/admin/setup-help/notifications')
    def retry(request: Request):
        _, redirect = require_admin(request)
        if redirect:
            return redirect
        notify(sender,email_active())
        return RedirectResponse('/admin/setup-help',status_code=303)
