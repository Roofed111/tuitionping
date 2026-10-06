"""Permission-based email list, trial guidance, and durable email delivery.

Contacts and delivery history live in the application's SQLite/Postgres DB.
Account creation never grants marketing permission. Campaigns only use
confirmed subscribers, and eligibility is checked again immediately before send.
"""
import csv
import hashlib
import io
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.parse import urlencode
import store

BASE = os.getenv('PUBLIC_BASE_URL', 'https://www.tuitionping.com').rstrip('/')
CONSENT = 'Send me occasional TuitionPing tuition tips, product improvements and offers. I can unsubscribe anytime.'
CONSENT_VERSION = '2026-10-06'
KIT = '/static/downloads/daycare-tuition-collection-kit.zip'
_ready = None
_lock = threading.Lock()


def now():
    return datetime.now(timezone.utc)


def stamp(dt=None):
    return (dt or now()).isoformat(timespec='seconds')


def parsed(value):
    try:
        dt = datetime.fromisoformat(value or '')
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except (ValueError, TypeError):
        return None


def ensure_tables():
    global _ready
    key = (store.USE_PG, store.DATABASE_URL, store.DB_PATH)
    if _ready == key:
        return
    with _lock:
        if _ready == key:
            return
        statements = [
            '''CREATE TABLE IF NOT EXISTS email_contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                marketing_status TEXT NOT NULL DEFAULT 'none', requested_at TEXT,
                confirmed_at TEXT, unsubscribed_at TEXT, setup_opt_out_at TEXT,
                suppressed_at TEXT, consent_text TEXT, consent_version TEXT,
                confirm_hash TEXT, confirm_expires TEXT,
                manage_token TEXT NOT NULL UNIQUE, last_request_at TEXT)''',
            '''CREATE TABLE IF NOT EXISTS email_settings (
                key TEXT PRIMARY KEY, value TEXT NOT NULL)''',
            '''CREATE TABLE IF NOT EXISTS email_subscription_state (
                provider_id INTEGER PRIMARY KEY, cancel_scheduled INTEGER NOT NULL,
                updated_at TEXT NOT NULL)''',
            '''CREATE TABLE IF NOT EXISTS email_campaigns (
                id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT NOT NULL,
                body TEXT NOT NULL, cta_path TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft', created_at TEXT NOT NULL,
                queued_at TEXT, audience_count INTEGER NOT NULL DEFAULT 0)''',
            '''CREATE TABLE IF NOT EXISTS email_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT, contact_id INTEGER NOT NULL,
                provider_id INTEGER, campaign_id INTEGER, purpose TEXT NOT NULL,
                stage TEXT NOT NULL DEFAULT '', dedupe_key TEXT NOT NULL UNIQUE,
                subject TEXT NOT NULL, html TEXT NOT NULL, headers TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL,
                due_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                first_attempt_at TEXT, last_attempt_at TEXT, sent_at TEXT,
                error TEXT NOT NULL DEFAULT '')''',
            '''CREATE TABLE IF NOT EXISTS email_rate_limits (
                key TEXT PRIMARY KEY, ts TEXT NOT NULL, count INTEGER NOT NULL)''',
            'CREATE INDEX IF NOT EXISTS email_outbox_due ON email_outbox(status, due_at)',
        ]
        with store.db() as conn:
            for sql in statements:
                conn.execute(store.pg_ddl(sql))
        _ready = key


def settings():
    ensure_tables()
    with store.db() as conn:
        data = {r['key']: r['value'] for r in conn.execute('SELECT * FROM email_settings').fetchall()}
    return {'postal_address': data.get('postal_address', ''),
            'followups_enabled': data.get('followups_enabled', '1') == '1'}


def save_settings(address, enabled):
    address = address.strip()
    if address and (len(address) < 10 or len(address) > 500):
        raise ValueError('Enter a complete mailing address, or leave it empty to pause promotional sends.')
    ensure_tables()
    with store.db() as conn:
        for k, v in [('postal_address', address), ('followups_enabled', '1' if enabled else '0')]:
            conn.execute('INSERT INTO email_settings (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value = excluded.value', (k, v))


def normalize_email(email):
    email = email.strip().lower()
    if len(email) > 254 or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}", email):
        raise ValueError('Enter a valid email address.')
    local, domain = email.rsplit('@', 1)
    if local.startswith('.') or local.endswith('.') or '..' in email or any(label.startswith('-') or label.endswith('-') for label in domain.split('.')):
        raise ValueError('Enter a valid email address.')
    return email


def contact(email, name='', source='account'):
    ensure_tables()
    email = normalize_email(email)
    with store.db() as conn:
        conn.execute('INSERT INTO email_contacts (email,name,source,created_at,updated_at,manage_token) VALUES (?,?,?,?,?,?) ON CONFLICT(email) DO NOTHING',
                     (email, name.strip()[:100], source[:120], stamp(), stamp(), secrets.token_urlsafe(32)))
        return dict(conn.execute('SELECT * FROM email_contacts WHERE email = ?', (email,)).fetchone())


def by_id(cid):
    with store.db() as conn:
        row = conn.execute('SELECT * FROM email_contacts WHERE id = ?', (cid,)).fetchone()
    return dict(row) if row else None


def by_token(token):
    ensure_tables()
    if not re.fullmatch(r'[A-Za-z0-9_-]{40,80}', token):
        return None
    with store.db() as conn:
        row = conn.execute('SELECT * FROM email_contacts WHERE manage_token = ?', (token,)).fetchone()
    return dict(row) if row else None


def rate_allowed(ip):
    ensure_tables()
    dt = now()
    bucket = dt.strftime('%Y-%m-%d-%H')
    key = hashlib.sha256((os.getenv('SECRET_KEY', 'email-rate') + ip + bucket).encode()).hexdigest()
    with store.db() as conn:
        conn.execute('DELETE FROM email_rate_limits WHERE ts < ?', (stamp(dt - timedelta(days=2)),))
        conn.execute('INSERT INTO email_rate_limits (key,ts,count) VALUES (?,?,0) ON CONFLICT(key) DO NOTHING', (key, stamp(dt)))
        cur = conn.execute('UPDATE email_rate_limits SET count = count + 1 WHERE key = ? AND count < 10', (key,))
    return cur.rowcount > 0


def message(content, c, promotional=False, address=None):
    url = BASE + '/email-list/preferences?token=' + c['manage_token']
    footer = '<hr><p>TuitionPing · Hirsch Commerce LLC</p>'
    if promotional:
        address = address if address is not None else settings()['postal_address']
        footer += '<p>' + escape(address).replace('\n', '<br>') + '</p><p>Optional promotional email from TuitionPing: tips, setup guidance and product updates.</p>'
    footer += f'<p><a href="{escape(url)}">Unsubscribe from optional TuitionPing emails</a>. Account security and billing notices continue.</p>'
    headers = {'List-Unsubscribe': '<' + url + '>', 'List-Unsubscribe-Post': 'List-Unsubscribe=One-Click'}
    return '<!doctype html><html lang="en"><body>' + content + footer + '</body></html>', headers


def enqueue(c, purpose, key, subject, html, headers, provider_id=None, campaign_id=None, stage=''):
    ensure_tables()
    dedupe = hashlib.sha256(key.encode()).hexdigest()
    with store.db() as conn:
        conn.execute('INSERT INTO email_outbox (contact_id,provider_id,campaign_id,purpose,stage,dedupe_key,subject,html,headers,created_at,due_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(dedupe_key) DO NOTHING',
                     (c['id'], provider_id, campaign_id, purpose, stage, dedupe, subject, html, json.dumps(headers, sort_keys=True), stamp(), stamp()))
        return conn.execute('SELECT id FROM email_outbox WHERE dedupe_key = ?', (dedupe,)).fetchone()['id']


def request_email(email, name, source, optin=False, kit=True):
    """Deliver requested resources; marketing remains pending until confirmed."""
    c = contact(email, name, source)
    dt = now()
    if c['suppressed_at'] or (parsed(c['last_request_at']) and dt - parsed(c['last_request_at']) < timedelta(days=1)):
        return None
    ts = stamp(dt)
    token = secrets.token_urlsafe(32) if optin and c['marketing_status'] != 'subscribed' else ''
    with store.db() as conn:
        # Claim once per address per day, across concurrent public requests.
        claimed = conn.execute('UPDATE email_contacts SET last_request_at = ?, updated_at = ? WHERE id = ? AND (last_request_at IS NULL OR last_request_at <= ?)',
                               (ts, ts, c['id'], stamp(dt - timedelta(days=1)))).rowcount
        if not claimed:
            return None
        if token:
            conn.execute("UPDATE email_contacts SET marketing_status = 'pending', requested_at = ?, consent_text = ?, consent_version = ?, confirm_hash = ?, confirm_expires = ? WHERE id = ?",
                         (ts, CONSENT, CONSENT_VERSION, hashlib.sha256(token.encode()).hexdigest(), stamp(dt + timedelta(days=7)), c['id']))
    content = '<p>Your requested TuitionPing resources are ready.</p>' if kit else '<p>You requested TuitionPing tuition tips and product updates.</p>'
    if kit:
        content += f'<p><a href="{BASE + KIT}">Download the free tuition collection kit</a></p><p>Includes editable policies, English and Spanish reminder templates, and an Excel payment tracker. No account is required.</p>'
    if token:
        content += f'<p>To join the marketing email list, confirm your email:</p><p><a href="{BASE}/email-list/confirm?token={token}">Confirm tips and updates</a></p><p>This link expires in 7 days. You will not receive marketing emails unless you confirm.</p>'
    html, headers = message(content, c)
    return enqueue(c, 'kit' if kit else 'confirmation', f'request:{c["id"]}:{ts}',
                   'Your free TuitionPing tuition collection kit' if kit else 'Confirm your TuitionPing tips and updates', html, headers)


def confirm_contact(token):
    ensure_tables()
    if not re.fullmatch(r'[A-Za-z0-9_-]{40,80}', token):
        return False
    with store.db() as conn:
        return conn.execute("UPDATE email_contacts SET marketing_status = 'subscribed', confirmed_at = ?, unsubscribed_at = NULL, confirm_hash = NULL, confirm_expires = NULL, updated_at = ? WHERE confirm_hash = ? AND confirm_expires >= ? AND suppressed_at IS NULL",
                            (stamp(), stamp(), hashlib.sha256(token.encode()).hexdigest(), stamp())).rowcount > 0


def unsubscribe(token):
    c = by_token(token)
    if not c:
        return False
    with store.db() as conn:
        conn.execute("UPDATE email_contacts SET marketing_status = 'unsubscribed', unsubscribed_at = ?, setup_opt_out_at = ?, confirm_hash = NULL, confirm_expires = NULL, updated_at = ? WHERE id = ?",
                     (stamp(), stamp(), stamp(), c['id']))
        conn.execute("UPDATE email_outbox SET status = 'cancelled', error = 'Unsubscribed' WHERE contact_id = ? AND purpose IN ('onboarding','campaign','confirmation') AND status IN ('pending','failed')", (c['id'],))
    return True


def suppress(cid):
    ensure_tables()
    with store.db() as conn:
        conn.execute("UPDATE email_contacts SET suppressed_at = ?, marketing_status = 'suppressed', updated_at = ? WHERE id = ?", (stamp(), stamp(), cid))
        conn.execute("UPDATE email_outbox SET status = 'cancelled', error = 'Suppressed' WHERE contact_id = ? AND status IN ('pending','failed')", (cid,))


def contacts():
    ensure_tables()
    with store.db() as conn:
        return [dict(r) for r in conn.execute('SELECT c.*, (SELECT MAX(o.sent_at) FROM email_outbox o WHERE o.contact_id = c.id) AS last_sent FROM email_contacts c ORDER BY c.created_at DESC').fetchall()]


def export_csv():
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    keys = ['email', 'name', 'source', 'requested_at', 'confirmed_at', 'consent_version', 'consent_text']
    writer.writerow(keys)
    for c in contacts():
        if c['marketing_status'] == 'subscribed' and not c['suppressed_at']:
            # Do not let user-entered names/sources execute spreadsheet formulas.
            values = [str(c.get(k) or '') for k in keys]
            writer.writerow(["'" + v if v[:1] in '=+-@\t\r' else v for v in values])
    return output.getvalue()


def note_subscription(provider_id, sub):
    ensure_tables()
    cancelled = bool(sub.get('cancel_at_period_end') or sub.get('cancel_at') or sub.get('status') == 'canceled')
    with store.db() as conn:
        conn.execute('INSERT INTO email_subscription_state (provider_id,cancel_scheduled,updated_at) VALUES (?,?,?) ON CONFLICT(provider_id) DO UPDATE SET cancel_scheduled = excluded.cancel_scheduled, updated_at = excluded.updated_at', (provider_id, int(cancelled), stamp()))


def onboarding_stage(provider, dt=None, verify_billing=False):
    """Choose at most one useful next step from current account state."""
    dt = dt or now()
    p = dict(provider)
    if p.get('suspended') or not p.get('email_verified'):
        return None
    created = parsed(p.get('created_at'))
    if not created:
        return None
    sub = store.get_subscription(p['id'])
    sub = dict(sub) if sub else {}
    status = sub.get('status') or 'none'
    import billing
    if verify_billing and status == 'trialing' and billing.stripe_configured() and sub.get('stripe_subscription_id'):
        try:
            live = billing._as_dict(billing._stripe_lib().Subscription.retrieve(sub['stripe_subscription_id']))
            billing._sync_from_subscription(p['id'], live)
            sub = dict(store.get_subscription(p['id']))
            status = sub.get('status') or 'none'
        except Exception:
            return None  # Do not send trial guidance from unconfirmed billing state.
    ensure_tables()
    with store.db() as conn:
        cancellation = conn.execute('SELECT cancel_scheduled FROM email_subscription_state WHERE provider_id = ?', (p['id'],)).fetchone()
    if cancellation and cancellation['cancel_scheduled']:
        return None
    if status in ('active', 'past_due', 'canceled', 'cancelled', 'expired', 'unpaid', 'paused'):
        return None
    if status in ('none', 'incomplete'):
        return 'checkout' if timedelta(days=1) <= dt - created <= timedelta(days=7) else None
    end = parsed(sub.get('trial_ends_at'))
    if status != 'trialing' or not end or end <= dt:
        return None
    if timedelta(0) < end - dt <= timedelta(days=3):
        return 'trial_ending'
    started = end - timedelta(days=billing.TRIAL_DAYS)
    if dt - started < timedelta(days=1):
        return None
    if not store.count_families_for_provider(p['id']):
        return 'families'
    with store.db() as conn:
        sent = conn.execute("SELECT 1 FROM reminder_log r JOIN families f ON f.id = r.family_id JOIN classrooms c ON c.id = f.classroom_id JOIN locations l ON l.id = c.location_id WHERE l.provider_id = ? LIMIT 1", (p['id'],)).fetchone()
    if not sent and dt - started >= timedelta(days=3):
        return 'first_reminder'
    return None


def onboarding_copy(stage, provider, sub=None):
    name = escape((provider.get('name') or 'there').strip().split()[0])
    if stage == 'checkout':
        return 'Your TuitionPing account setup is unfinished', f'<p>Hi {name},</p><p>Your account was created, but a completed checkout has not been confirmed. The real 30-day trial starts after Stripe confirms checkout.</p><p><a href="{BASE}/billing">Review your plan and finish checkout</a></p><p>Checkout collects a card. $0 is charged at the start of the trial; the selected plan is charged after the trial unless you cancel. If you decided not to continue, no action is needed.</p>'
    if stage == 'families':
        return 'Next setup step: add your first daycare family', f'<p>Hi {name},</p><p>Your trial is running, but your account has no families yet.</p><p><a href="{BASE}/dashboard#add-location">Open your setup checklist</a>. Add a location and classroom, then enter a family or import your roster. Use your agreed amount and due date, choose English or Spanish, and obtain permission for tuition texts before adding a phone number.</p><p>You keep your existing payment method. A PAID reply is a report; confirm receipt in the dashboard after checking your records.</p>'
    if stage == 'first_reminder':
        return 'Check when your first tuition reminder is scheduled', f'<p>Hi {name},</p><p>You added families, but your account has not recorded its first scheduled tuition reminder yet.</p><p><a href="{BASE}/dashboard">Check Today\'s reminders and your setup checklist</a>. Confirm amounts, due dates, phone numbers and permission to text. Reminders are scheduled three days before the due date, on the due date, and three and seven days overdue. Quiet hours and opt-outs apply.</p><p>A future due date may mean nothing is scheduled today. Do not change a family\'s due date just to trigger a text. You can preview your wording with the dashboard\'s test-text tool.</p>'
    end = parsed((sub or {}).get('trial_ends_at'))
    date_text = end.strftime('%B %d, %Y at %H:%M UTC') if end else 'soon'
    import billing
    price = billing.PLANS.get((sub or {}).get('plan'), {}).get('price')
    price_text = f' The standard monthly plan price is ${price}; discounts and annual billing may change your amount.' if price else ''
    return 'Your TuitionPing trial ends soon', f'<p>Hi {name},</p><p>Your trial is scheduled to end on {date_text}. Your selected plan renews after the trial unless you cancel.{price_text}</p><p><a href="{BASE}/billing">Review your exact billing terms or cancel</a> in Billing before the trial ends.</p><p>If you are still setting up, <a href="{BASE}/dashboard">open your setup checklist</a>. Reply if you need help.</p>'


def queue_onboarding():
    cfg = settings()
    if not cfg['followups_enabled'] or not cfg['postal_address']:
        return 0
    store.ensure_email_columns()
    count = 0
    for raw in store.all_providers():
        p = dict(raw)
        stage = onboarding_stage(p, verify_billing=True)
        if not stage:
            continue
        try:
            c = contact(p['email'], p['name'])
        except ValueError:
            continue  # One malformed legacy address cannot block other accounts.
        if c['setup_opt_out_at'] or c['unsubscribed_at'] or c['suppressed_at']:
            continue
        # No more than one optional account guidance message in 48 hours.
        with store.db() as conn:
            already = conn.execute("SELECT 1 FROM email_outbox WHERE provider_id = ? AND purpose = 'onboarding' AND stage = ? LIMIT 1", (p['id'], stage)).fetchone()
            recent = conn.execute("SELECT 1 FROM email_outbox WHERE contact_id = ? AND purpose = 'onboarding' AND (status IN ('pending','sending') OR sent_at >= ?) LIMIT 1", (c['id'], stamp(now() - timedelta(hours=48)))).fetchone()
        if already or recent:
            continue
        sub = store.get_subscription(p['id'])
        subject, content = onboarding_copy(stage, p, dict(sub) if sub else {})
        html, headers = message(content, c, promotional=True, address=cfg['postal_address'])
        enqueue(c, 'onboarding', f'onboarding:{p["id"]}:{stage}', subject, html, headers, provider_id=p['id'], stage=stage)
        count += 1
    return count


def create_campaign(subject, body, cta_path):
    subject, body = subject.strip(), body.strip()
    if not subject or len(subject) > 160 or '\n' in subject or '\r' in subject:
        raise ValueError('Use a subject of 1–160 characters on one line.')
    if not body or len(body) > 12000:
        raise ValueError('Enter a message of 1–12,000 characters.')
    if cta_path not in ('/demo', '/guides', '/guide', '/signup?plan=micro', '/support'):
        raise ValueError('Choose one of the listed destinations.')
    ensure_tables()
    with store.db() as conn:
        return store._insert_and_get_id(conn, 'INSERT INTO email_campaigns (subject,body,cta_path,created_at) VALUES (?,?,?,?)', (subject, body, cta_path, stamp()))


def campaign(cid):
    ensure_tables()
    with store.db() as conn:
        row = conn.execute('SELECT * FROM email_campaigns WHERE id = ?', (cid,)).fetchone()
    return dict(row) if row else None


def campaign_content(camp):
    paragraphs = ''.join('<p>' + escape(p).replace('\n', '<br>') + '</p>' for p in camp['body'].split('\n\n') if p.strip())
    dest = BASE + camp['cta_path']
    dest += ('&' if '?' in dest else '?') + urlencode({'utm_source': 'tuitionping', 'utm_medium': 'email', 'utm_campaign': f'campaign_{camp["id"]}'})
    return paragraphs + f'<p><a href="{escape(dest)}">Explore TuitionPing</a></p>'


def queue_campaign(cid, expected_audience=None):
    cfg = settings()
    if not cfg['postal_address']:
        raise ValueError('Save a valid mailing address before sending marketing emails.')
    camp = campaign(cid)
    if not camp or camp['status'] != 'draft':
        return 0
    audience = [c for c in contacts() if c['marketing_status'] == 'subscribed' and not c['suppressed_at']]
    if expected_audience is not None and len(audience) != expected_audience:
        raise ValueError('The audience changed. Reload the preview before sending.')
    if not audience:
        raise ValueError('There are no confirmed marketing subscribers yet.')
    # Snapshot the audience once. A retry cannot add new subscribers later.
    with store.db() as conn:
        claimed = conn.execute("UPDATE email_campaigns SET status = 'queued', queued_at = ?, audience_count = ? WHERE id = ? AND status = 'draft'", (stamp(), len(audience), cid)).rowcount
        if not claimed:
            return 0
        for c in audience:
            html, headers = message(campaign_content(camp), c, promotional=True, address=cfg['postal_address'])
            key = hashlib.sha256(f'campaign:{cid}:{c["id"]}'.encode()).hexdigest()
            conn.execute('INSERT INTO email_outbox (contact_id,campaign_id,purpose,dedupe_key,subject,html,headers,created_at,due_at) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(dedupe_key) DO NOTHING',
                         (c['id'], cid, 'campaign', key, camp['subject'], html, json.dumps(headers, sort_keys=True), stamp(), stamp()))
    return len(audience)


def cancel_campaign(cid):
    with store.db() as conn:
        conn.execute("UPDATE email_campaigns SET status = 'cancelled' WHERE id = ?", (cid,))
        conn.execute("UPDATE email_outbox SET status = 'cancelled', error = 'Campaign cancelled' WHERE campaign_id = ? AND status IN ('pending','failed')", (cid,))


def eligible(row):
    c = by_id(row['contact_id'])
    if not c or c['suppressed_at']:
        return False, 'Contact suppressed'
    if row['purpose'] == 'campaign':
        camp = campaign(row['campaign_id'])
        if c['marketing_status'] != 'subscribed' or not camp or camp['status'] == 'cancelled':
            return False, 'Unsubscribed or campaign cancelled'
    if row['purpose'] == 'onboarding':
        p = store.get_provider(row['provider_id'])
        if c['setup_opt_out_at'] or c['unsubscribed_at'] or not p or onboarding_stage(p, verify_billing=True) != row['stage']:
            return False, 'Account progressed or guidance unsubscribed'
    if row['purpose'] == 'confirmation' and c['unsubscribed_at'] and c['marketing_status'] != 'pending':
        return False, 'Unsubscribed'
    return True, ''


def deliver_due(sender, active=True, limit=20, only_id=None):
    ensure_tables()
    result = {'accepted': 0, 'failed': 0, 'cancelled': 0, 'paused': 0}
    if not active:
        return {**result, 'paused': 1}
    dt = now()
    with store.db() as conn:
        # Retry crashed workers only inside Resend's 24-hour idempotency window.
        conn.execute("UPDATE email_outbox SET status = 'failed', due_at = ?, error = 'Worker interrupted; retry with same idempotency key' WHERE status = 'sending' AND last_attempt_at < ? AND first_attempt_at >= ?", (stamp(dt), stamp(dt - timedelta(minutes=20)), stamp(dt - timedelta(hours=23))))
        conn.execute("UPDATE email_outbox SET status = 'uncertain', error = 'Delivery unknown; idempotency window expired. Review in email provider before any resend.' WHERE status IN ('sending','failed') AND first_attempt_at < ?", (stamp(dt - timedelta(hours=23)),))
        sql = "SELECT * FROM email_outbox WHERE status IN ('pending','failed') AND due_at <= ? AND attempts < 3"
        params = [stamp(dt)]
        if only_id is not None:
            sql += ' AND id = ?'
            params.append(only_id)
        sql += " ORDER BY CASE purpose WHEN 'kit' THEN 0 WHEN 'confirmation' THEN 0 WHEN 'onboarding' THEN 1 ELSE 2 END, id LIMIT ?"
        params.append(min(max(limit, 1), 100))
        rows = [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]
    cfg = settings()
    attempted = 0
    for row in rows:
        if row['purpose'] in ('onboarding', 'campaign') and (not cfg['postal_address'] or (row['purpose'] == 'onboarding' and not cfg['followups_enabled'])):
            result['paused'] += 1
            continue
        ok, reason = eligible(row)
        if not ok:
            with store.db() as conn:
                conn.execute("UPDATE email_outbox SET status = 'cancelled', error = ? WHERE id = ? AND status IN ('pending','failed')", (reason, row['id']))
            result['cancelled'] += 1
            continue
        with store.db() as conn:
            claimed = conn.execute("UPDATE email_outbox SET status = 'sending', attempts = attempts + 1, first_attempt_at = COALESCE(first_attempt_at, ?), last_attempt_at = ? WHERE id = ? AND status IN ('pending','failed') AND attempts < 3", (stamp(dt), stamp(dt), row['id'])).rowcount
        if not claimed:
            continue
        # Recheck opt-outs/progress after taking the delivery claim.
        ok, reason = eligible(row)
        c = by_id(row['contact_id'])
        if not ok:
            with store.db() as conn:
                conn.execute("UPDATE email_outbox SET status = 'cancelled', error = ? WHERE id = ?", (reason, row['id']))
            result['cancelled'] += 1
            continue
        try:
            if attempted:
                time.sleep(0.15)  # Pace a batch below the provider's default rate.
            attempted += 1
            accepted = bool(sender(c['email'], row['subject'], row['html'], headers=json.loads(row['headers']), idempotency_key='tp-email-' + row['dedupe_key']))
        except Exception:
            accepted = False
        with store.db() as conn:
            conn.execute('UPDATE email_outbox SET status = ?, sent_at = ?, due_at = ?, error = ? WHERE id = ?',
                         ('sent' if accepted else 'failed', stamp() if accepted else None, stamp(dt + timedelta(hours=1)), '' if accepted else 'Email provider did not confirm acceptance; up to three attempts within 23 hours.', row['id']))
        result['accepted' if accepted else 'failed'] += 1
    with store.db() as conn:
        conn.execute("UPDATE email_campaigns SET status = 'finished' WHERE status = 'queued' AND NOT EXISTS (SELECT 1 FROM email_outbox o WHERE o.campaign_id = email_campaigns.id AND (o.status IN ('pending','sending') OR (o.status = 'failed' AND o.attempts < 3)))")
    return result


def run(sender, active=True):
    queued = queue_onboarding() if active else 0
    return {'queued_guidance': queued, **deliver_due(sender, active)}


def report():
    ensure_tables()
    with store.db() as conn:
        campaigns = [dict(r) for r in conn.execute('SELECT * FROM email_campaigns ORDER BY id DESC LIMIT 100').fetchall()]
        outbox = [dict(r) for r in conn.execute('SELECT o.id,o.purpose,o.stage,o.status,o.attempts,o.created_at,o.sent_at,o.error,c.email FROM email_outbox o JOIN email_contacts c ON c.id = o.contact_id ORDER BY o.id DESC LIMIT 100').fetchall()]
        for camp in campaigns:
            camp['delivery'] = {r['status']: r['n'] for r in conn.execute('SELECT status, COUNT(*) AS n FROM email_outbox WHERE campaign_id = ? GROUP BY status', (camp['id'],)).fetchall()}
    cs = contacts()
    return {'contacts': cs, 'campaigns': campaigns, 'outbox': outbox,
            'confirmed': sum(c['marketing_status'] == 'subscribed' and not c['suppressed_at'] for c in cs),
            'pending': sum(c['marketing_status'] == 'pending' for c in cs), 'settings': settings()}
