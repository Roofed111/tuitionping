"""First-party acquisition measurement. No IPs, form values or SMS content.

Marketing events are browser estimates, not identities. Account milestones are
server-confirmed and deduplicated. Analytics failures never block the product.
"""
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
import store
import traffic
import acquisition

COOKIE = 'tp_growth'
_KEY = os.getenv('SECRET_KEY', '').encode() or secrets.token_bytes(32)
_ready = None
_lock = threading.Lock()
CLIENT_EVENTS = {'browser_verified': {'', 'webdriver'}, 'demo_started': {''}, 'demo_step': {'before', 'due', 'late', 'reported', 'verified', 'spanish'}, 'trial_click': {''}, 'document_created': {'invoice','receipt'}, 'video_started': {''}, 'video_completed': {''}}
CLIENT_EVENTS['visitor_engaged'] = {'interaction','active_reading'}
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
            conn.execute("CREATE TABLE IF NOT EXISTS growth_report_state (id INTEGER PRIMARY KEY, event_floor INTEGER NOT NULL, generation TEXT NOT NULL, reset_at TEXT NOT NULL, reset_by INTEGER)")
            conn.execute("INSERT INTO growth_report_state (id,event_floor,generation,reset_at) VALUES (1,0,'','') ON CONFLICT(id) DO NOTHING")
            traffic.ensure_schema(conn)
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
    return (request.headers.get('dnt') == '1' or request.headers.get('sec-gpc') == '1'
            or 'tuitionping_session' in request.cookies
            or bool(traffic.known_bot(request.headers.get('user-agent', ''))))

def attribution(request):
    detail = acquisition.describe(request)
    return detail['source'], detail['medium'], detail['campaign']

def register(visitor_id, source, medium, campaign, path, detail=None):
    ensure_tables()
    now = store.now_iso()
    with store.db() as conn:
        # Reporting windows limit the report, not the underlying audit records.
        conn.execute('INSERT INTO growth_visitors (visitor_id,first_seen,source,medium,campaign,landing_path,attribution_json) VALUES (?,?,?,?,?,?,?) ON CONFLICT(visitor_id) DO NOTHING', (visitor_id, now, source, medium, campaign, path, json.dumps(detail) if detail else ''))

def record(visitor_id, event, detail='', path='', provider_id=None):
    if not visitor_id:
        return
    ensure_tables()
    with store.db() as conn:
        key = f'account:{provider_id}:{event}' if provider_id else ':'.join([visitor_id, event, detail, path])
        if not provider_id:
            state = conn.execute('SELECT generation FROM growth_report_state WHERE id = 1').fetchone()
            if state['generation']:
                key = state['generation'] + ':' + key
        conn.execute('INSERT INTO growth_events (visitor_id,provider_id,event,detail,path,ts,dedupe_key) VALUES (?,?,?,?,?,?,?) ON CONFLICT(dedupe_key) DO NOTHING', (visitor_id, provider_id, event, detail, path, store.now_iso(), key))
    if provider_id:
        traffic.confirm_account_activity(visitor_id,event)

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

def reset_report(provider_id):
    """Start a new report without deleting history or business deduplication.

    Browser events may count again in the new period. Account milestone keys
    stay unchanged so webhook retries cannot turn old customers into new ones.
    """
    ensure_tables()
    with store.db() as conn:
        floor = conn.execute('SELECT COALESCE(MAX(id),0) AS n FROM growth_events').fetchone()['n']
        conn.execute('UPDATE growth_report_state SET event_floor = ?, generation = ?, reset_at = ?, reset_by = ? WHERE id = 1',
                     (floor, secrets.token_hex(16), store.now_iso(), provider_id))


def report(days=28, kind='human'):
    ensure_tables()
    traffic.refresh_candidates()
    traffic.refresh_account_engagement()
    kind = kind if kind in ('engaged','browser','unconfirmed','human', 'automated', 'all', 'unknown') else 'engaged'
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec='seconds')
    with store.db() as conn:
        state = conn.execute('SELECT * FROM growth_report_state WHERE id = 1').fetchone()
        if state['generation']:
            # Use activity in the new reporting period, including returning
            # browsers whose first-touch attribution was recorded before reset.
            visitors = [dict(r) for r in conn.execute('SELECT DISTINCT v.* FROM growth_visitors v JOIN growth_events e ON e.visitor_id = v.visitor_id WHERE e.id > ? AND e.ts >= ?', (state['event_floor'], cutoff)).fetchall()]
            events = [dict(r) for r in conn.execute('SELECT e.* FROM growth_events e JOIN growth_visitors v ON e.visitor_id = v.visitor_id WHERE e.id > ? AND e.ts >= ?', (state['event_floor'], cutoff)).fetchall()]
        else:
            visitors = [dict(r) for r in conn.execute('SELECT * FROM growth_visitors WHERE first_seen >= ?', (cutoff,)).fetchall()]
            events = [dict(r) for r in conn.execute('SELECT e.* FROM growth_events e JOIN growth_visitors v ON e.visitor_id = v.visitor_id WHERE v.first_seen >= ?', (cutoff,)).fetchall()]
    # Anonymous hit-only records belong in Visitors, not the acquisition funnel.
    event_ids = {e['visitor_id'] for e in events}
    raw_visitors = [v for v in visitors if v['visitor_id'] in event_ids]
    quality = {}
    for v in raw_visitors:
        if v['classification'] not in traffic.HUMAN_TYPES: continue
        key = (v['source'],v['medium'],v['campaign'])
        group = quality.setdefault(key, {'key':key,'label':acquisition.source_label(key[0],key[1]),'visitors':0,'engaged':0,'browser':0,'unconfirmed':0,'ids':set()})
        group['visitors'] += 1
        group['engaged'] += int(traffic.eligible('engaged',v))
        group['browser'] += int(traffic.eligible('browser',v))
        group['unconfirmed'] += int(traffic.eligible('unconfirmed',v))
        group['ids'].add(v['visitor_id'])
    for group in quality.values():
        ids = group.pop('ids')
        group['demo'] = len({e['visitor_id'] for e in events if e['visitor_id'] in ids and e['event']=='demo_started'})
        group['signups'] = len({e['provider_id'] for e in events if e['visitor_id'] in ids and e['event']=='signup' and e['provider_id'] is not None})
    visitors = [v for v in raw_visitors if traffic.eligible(kind, v)]
    eligible = {v['visitor_id'] for v in visitors}
    events = [e for e in events if e['visitor_id'] in eligible]
    identities = {v['visitor_id']: traffic.metric_id(v) for v in visitors}
    event_sets = {}
    for e in events:
        event_sets.setdefault(e['event'], set()).add(e['visitor_id'])
    account_stages = {'signup','checkout_started','checkout_completed','trial_started','first_reminder','paid_customer'}
    stages = [{'event': event, 'label': (('Engaged visitors' if kind == 'engaged' else 'All likely people' if kind == 'human' else label) if event == 'page_view' else label),
               'count': (len({e['provider_id'] for e in events if e['event'] == event and e['provider_id'] is not None})
                         if event in account_stages else (len({identities[vid] for vid in event_sets.get(event,set())}) if event == 'page_view' else len(event_sets.get(event, set()))))} for event, label in STAGES]
    def groups(keys):
        buckets = {}
        for v in visitors:
            k = tuple(v[x] for x in keys)
            buckets.setdefault(k, set()).add(v['visitor_id'])
        def accounts(ids, event):
            return len({e['provider_id'] for e in events if e['event'] == event and e['visitor_id'] in ids and e['provider_id'] is not None})
        return [{'key': k, 'label':acquisition.source_label(k[0],k[1]) if len(k)==3 else '', 'details': sorted({acquisition.display(v)['term'] for v in visitors if v['visitor_id'] in ids and acquisition.display(v)['term']}), 'visitors': len({identities[vid] for vid in ids}), 'demo': len(ids & event_sets.get('demo_started', set())),
                 'help': len(ids & event_sets.get('setup_help_requested', set())), 'signups': accounts(ids, 'signup'), 'trials': accounts(ids, 'trial_started'),
                 'paid': accounts(ids, 'paid_customer')} for k, ids in sorted(buckets.items(), key=lambda p: -len(p[1]))]
    # Visitor rates use the same eligible browser cohort for both sides. Two
    # accounts on one browser cannot turn a visitor conversion rate above 100%.
    denominator = {identities[vid] for vid in event_sets.get('page_view',set())}
    signed_up = {identities[e['visitor_id']] for e in events if e['event'] == 'signup' and e['provider_id'] is not None} & denominator
    paid = {identities[e['visitor_id']] for e in events if e['event'] == 'paid_customer' and e['provider_id'] is not None} & denominator
    return {'stages': stages, 'sources': groups(['source', 'medium', 'campaign']), 'pages': groups(['landing_path']),
            'source_quality':sorted(quality.values(),key=lambda g:(-g['engaged'],-g['visitors'],g['key'])),
            'days': days, 'reset_at': state['reset_at'], 'kind': kind,
            'rate_denominator': len(denominator), 'converting_visitors': len(signed_up), 'paid_visitors': len(paid),
            'conversion_rate': round(100 * len(signed_up) / len(denominator), 2) if denominator else None,
            'paid_conversion_rate': round(100 * len(paid) / len(denominator), 2) if denominator else None,
            'automated_visitors': len({traffic.metric_id(v) for v in raw_visitors if v['classification'] in traffic.BOT_TYPES}),
            'engaged_visitors': sum(traffic.eligible('engaged',v) for v in raw_visitors),
            'browser_only_visitors': sum(traffic.eligible('browser',v) for v in raw_visitors),
            'unconfirmed_visitors': sum(traffic.eligible('unconfirmed',v) for v in raw_visitors),
            'unknown_visitors': sum(v['classification'] == 'UNKNOWN' for v in raw_visitors)}
