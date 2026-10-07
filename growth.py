"""First-party acquisition measurement. No IPs, form values or SMS content.

Marketing events are browser estimates, not identities. Account milestones are
server-confirmed and deduplicated. Analytics failures never block the product.
"""
import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
import store

COOKIE = 'tp_growth'
_KEY = os.getenv('SECRET_KEY', '').encode() or secrets.token_bytes(32)
_ready = None
_lock = threading.Lock()
CLIENT_EVENTS = {'demo_started': {''}, 'demo_step': {'before', 'due', 'late', 'reported', 'verified', 'spanish'}, 'trial_click': {''}, 'document_created': {'invoice','receipt'}, 'video_started': {''}, 'video_completed': {''}}
STAGES = [('page_view', 'Visitors'), ('video_started', 'Walkthrough played'), ('video_completed', 'Walkthrough completed'), ('demo_started', 'Demo used'), ('download', 'Resource downloaded'),
          ('document_created', 'Document generated'), ('setup_help_requested', 'Setup help requested'), ('trial_click', 'Trial clicked'), ('signup', 'Account created'), ('checkout_started', 'Checkout opened'),
          ('checkout_completed', 'Checkout completed'), ('trial_started', 'Trial started'),
          ('first_reminder', 'First tuition reminder accepted'), ('paid_customer', 'Paid customer')]

def ensure_tables():
    global _ready
    key = (store.USE_PG, store.DATABASE_URL, store.DB_PATH)
    if _ready == key:
        return
    with _lock:
        if _ready == key:
            return
        with store.db() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS growth_visitors (visitor_id TEXT PRIMARY KEY, first_seen TEXT NOT NULL, source TEXT NOT NULL, medium TEXT NOT NULL, campaign TEXT NOT NULL, landing_path TEXT NOT NULL)')
            conn.execute('CREATE TABLE IF NOT EXISTS growth_accounts (provider_id INTEGER PRIMARY KEY, visitor_id TEXT NOT NULL)')
            conn.execute(store.pg_ddl('CREATE TABLE IF NOT EXISTS growth_events (id INTEGER PRIMARY KEY AUTOINCREMENT, visitor_id TEXT NOT NULL, provider_id INTEGER, event TEXT NOT NULL, detail TEXT NOT NULL, path TEXT NOT NULL, ts TEXT NOT NULL, dedupe_key TEXT NOT NULL UNIQUE)'))
            conn.execute('CREATE INDEX IF NOT EXISTS growth_events_visitor ON growth_events (visitor_id)')
            conn.execute('CREATE TABLE IF NOT EXISTS growth_partners (code TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, created_at TEXT NOT NULL)')
            conn.execute('CREATE TABLE IF NOT EXISTS growth_partner_visitors (visitor_id TEXT PRIMARY KEY, partner_code TEXT NOT NULL, touched_at TEXT NOT NULL)')
            conn.execute('CREATE TABLE IF NOT EXISTS growth_partner_accounts (provider_id INTEGER PRIMARY KEY, visitor_id TEXT NOT NULL, partner_code TEXT NOT NULL, bound_at TEXT NOT NULL)')
        _ready = key

def _mac(value):
    return hmac.new(_KEY, value.encode(), hashlib.sha256).hexdigest()

def cookie_value(visitor_id):
    return visitor_id + '.' + _mac(visitor_id)

def visitor_from_cookie(value):
    parts = (value or '').split('.')
    if len(parts) == 2 and re.fullmatch('[a-f0-9]{32}', parts[0]) and hmac.compare_digest(parts[1], _mac(parts[0])):
        return parts[0]
    return ''

def token(visitor_id):
    return _mac('event:' + visitor_id)

def excluded(request):
    ua = request.headers.get('user-agent', '').lower()
    return (request.headers.get('dnt') == '1' or request.headers.get('sec-gpc') == '1'
            or 'tuitionping_session' in request.cookies
            or bool(re.search(r'bot|crawler|spider|headless|preview|python|httpx|curl', ua)))

def attribution(request):
    # Only public campaign labels are accepted. Never retain arbitrary queries.
    def label(key):
        value = request.query_params.get(key, '')
        return value if re.fullmatch(r'[A-Za-z0-9_.-]{1,60}', value) else ''
    source, medium, campaign = label('utm_source'), label('utm_medium'), label('utm_campaign')
    if request.url.path == '/postcard' or request.cookies.get('tp_src') == 'postcard':
        return 'postcard', 'direct_mail', campaign
    if re.fullmatch(r'TP-[A-Z0-9]{6,20}', request.query_params.get('ref', '').upper()):
        return 'customer_referral', 'referral', campaign
    if source:
        return source, medium or 'campaign', campaign
    try:
        host = (urlsplit(request.headers.get('referer', '')).hostname or '').lower()
    except ValueError:
        host = ''
    if host in {'www.tuitionping.com', 'tuitionping.com', request.url.hostname}:
        return 'direct', 'none', ''
    if re.search(r'(^|\.)(google\.[a-z.]+|bing.com|duckduckgo.com|search.yahoo.com)$', host):
        return host, 'organic', ''
    if host and re.fullmatch(r'[a-z0-9.-]{1,60}', host):
        return host, 'referral', ''
    return 'direct', 'none', ''

def register(visitor_id, source, medium, campaign, path):
    ensure_tables()
    now = store.now_iso()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat(timespec='seconds')
    with store.db() as conn:
        # Bounded retention, also removes account-to-browser links after 90 days.
        conn.execute('DELETE FROM growth_events WHERE ts < ?', (cutoff,))
        conn.execute('DELETE FROM growth_accounts WHERE visitor_id IN (SELECT visitor_id FROM growth_visitors WHERE first_seen < ?)', (cutoff,))
        conn.execute('DELETE FROM growth_visitors WHERE first_seen < ?', (cutoff,))
        conn.execute('DELETE FROM growth_partner_accounts WHERE bound_at < ? OR visitor_id NOT IN (SELECT visitor_id FROM growth_visitors)', (cutoff,))
        conn.execute('DELETE FROM growth_partner_visitors WHERE touched_at < ? OR visitor_id NOT IN (SELECT visitor_id FROM growth_visitors)', (cutoff,))
        conn.execute('INSERT INTO growth_visitors (visitor_id,first_seen,source,medium,campaign,landing_path) VALUES (?,?,?,?,?,?) ON CONFLICT(visitor_id) DO NOTHING', (visitor_id, now, source, medium, campaign, path))

def record(visitor_id, event, detail='', path='', provider_id=None):
    if not visitor_id:
        return
    ensure_tables()
    with store.db() as conn:
        key = f'account:{provider_id}:{event}' if provider_id else ':'.join([visitor_id, event, detail, path])
        conn.execute('INSERT INTO growth_events (visitor_id,provider_id,event,detail,path,ts,dedupe_key) VALUES (?,?,?,?,?,?,?) ON CONFLICT(dedupe_key) DO NOTHING', (visitor_id, provider_id, event, detail, path, store.now_iso(), key))

def bind_account(visitor_id, provider_id):
    if not visitor_id:
        return
    ensure_tables()
    with store.db() as conn:
        conn.execute('INSERT INTO growth_accounts (provider_id,visitor_id) VALUES (?,?) ON CONFLICT(provider_id) DO NOTHING', (provider_id, visitor_id))
    try:
        import partner_resources
        partner_resources.bind_account(visitor_id, provider_id)
    except Exception:
        logging.getLogger(__name__).warning('Partner attribution could not be recorded')
    milestone(provider_id, 'signup')

def milestone(provider_id, event):
    try:
        ensure_tables()
        with store.db() as conn:
            row = conn.execute('SELECT visitor_id FROM growth_accounts WHERE provider_id = ?', (provider_id,)).fetchone()
        if row:
            record(row['visitor_id'], event, provider_id=provider_id)
    except Exception:
        logging.getLogger(__name__).warning('Conversion milestone could not be recorded')

def needs_milestone(provider_id, event):
    try:
        ensure_tables()
        with store.db() as conn:
            linked = conn.execute('SELECT 1 FROM growth_accounts WHERE provider_id = ?', (provider_id,)).fetchone()
            recorded = conn.execute('SELECT 1 FROM growth_events WHERE dedupe_key = ?', (f'account:{provider_id}:{event}',)).fetchone()
        return bool(linked and not recorded)
    except Exception:
        return False

def report(days=28):
    ensure_tables()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec='seconds')
    with store.db() as conn:
        visitors = [dict(r) for r in conn.execute('SELECT * FROM growth_visitors WHERE first_seen >= ?', (cutoff,)).fetchall()]
        events = [dict(r) for r in conn.execute('SELECT e.* FROM growth_events e JOIN growth_visitors v ON e.visitor_id = v.visitor_id WHERE v.first_seen >= ?', (cutoff,)).fetchall()]
    event_sets = {}
    for e in events:
        event_sets.setdefault(e['event'], set()).add(e['visitor_id'])
    account_stages = {'signup','checkout_started','checkout_completed','trial_started','first_reminder','paid_customer'}
    stages = [{'event': event, 'label': label, 'count': (len({e['provider_id'] for e in events if e['event'] == event}) if event in account_stages else len(event_sets.get(event, set())))} for event, label in STAGES]
    def groups(keys):
        buckets = {}
        for v in visitors:
            k = tuple(v[x] for x in keys)
            buckets.setdefault(k, set()).add(v['visitor_id'])
        def accounts(ids, event):
            return len({e['provider_id'] for e in events if e['event'] == event and e['visitor_id'] in ids})
        return [{'key': k, 'visitors': len(ids), 'demo': len(ids & event_sets.get('demo_started', set())),
                 'help': len(ids & event_sets.get('setup_help_requested', set())), 'signups': accounts(ids, 'signup'), 'trials': accounts(ids, 'trial_started'),
                 'paid': accounts(ids, 'paid_customer')} for k, ids in sorted(buckets.items(), key=lambda p: -len(p[1]))]
    return {'stages': stages, 'sources': groups(['source', 'medium', 'campaign']), 'pages': groups(['landing_path']), 'days': days}
