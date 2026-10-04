"""TuitionPing data layer — SQLite locally, Postgres in production.

Backend is chosen by environment: if DATABASE_URL is set (Railway Postgres),
everything runs on Postgres via psycopg; otherwise it falls back to the local
SQLite file. The rest of the app is backend-agnostic: queries are written with
? placeholders (translated to %s on Postgres) and rows behave like dicts.

Tables:
  providers      - account holders (email + password hash)
  sessions       - login session tokens (cookie based)
  locations     - a provider's sites ("Sunny Sprouts Daycare")
  classrooms          - individual tuitionals inside a location ("Classroom A")
  families        - one family per classroom: name, phone, tuition, due day
  message_log    - every SMS in/out, with status ("sent (demo)" in demo mode)
  reminder_log   - which reminder stages already fired (dedupe: never text twice)
  templates      - each provider's editable message wording
  subscriptions  - plan, status, trial end date
"""
import hashlib
import hmac
import os
import secrets
import sqlite3
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tuitionping.db")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_PG = bool(DATABASE_URL)

SCHEMA = """
CREATE TABLE IF NOT EXISTS providers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    provider_id INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS locations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    address TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS classrooms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id INTEGER NOT NULL,
    label TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS families (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    classroom_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    tuition_amount REAL NOT NULL,
    due_day INTEGER NOT NULL,          -- day of month tuition is due (1-31)
    paid_period TEXT,                  -- "YYYY-MM" of the tuition period marked paid
    opted_out INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS message_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id INTEGER NOT NULL,
    family_id INTEGER,
    direction TEXT NOT NULL,           -- "out" or "in"
    body TEXT NOT NULL,
    status TEXT NOT NULL,              -- e.g. "sent (demo)", "received"
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reminder_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    family_id INTEGER NOT NULL,
    period TEXT NOT NULL,              -- "YYYY-MM" the reminder belongs to
    stage TEXT NOT NULL,               -- before / due / late3 / late7
    sent_at TEXT NOT NULL,
    UNIQUE(family_id, period, stage)   -- never send the same reminder twice
);
CREATE TABLE IF NOT EXISTS templates (
    provider_id INTEGER PRIMARY KEY,
    tpl_before TEXT NOT NULL,
    tpl_due TEXT NOT NULL,
    tpl_late3 TEXT NOT NULL,
    tpl_late7 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id INTEGER NOT NULL UNIQUE,
    plan TEXT NOT NULL,                -- starter / growth / portfolio
    status TEXT NOT NULL,              -- trialing / active / canceled
    trial_ends_at TEXT,
    updated_at TEXT NOT NULL
);
"""

DEFAULT_TEMPLATES = {
    "tpl_before": "TuitionPing: Hi {name}, friendly reminder that tuition of ${amount} for {location} is due on {due_date}. Thanks! {pay_link} Reply STOP to opt out.",
    "tpl_due": "TuitionPing: Hi {name}, just a reminder that tuition of ${amount} for {location} is due today ({due_date}). Thanks! {pay_link} Reply STOP to opt out.",
    "tpl_late3": "TuitionPing: Hi {name}, we haven't received tuition of ${amount} for {location} yet (was due {due_date}). Please send it when you can — reply PAID once sent. {pay_link} Reply STOP to opt out.",
    "tpl_late7": "TuitionPing: Hi {name}, tuition of ${amount} for {location} is now 7 days overdue (due {due_date}). Please remit right away to avoid a late fee — reply PAID once sent. {pay_link} Reply STOP to opt out.",
}


# Postgres variant of the schema: the only SQLite-ism in the DDL is
# AUTOINCREMENT, which becomes SERIAL. (INSERT OR IGNORE is handled in code.)
SCHEMA_PG = SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")


class _PGConn:
    """Thin wrapper around a psycopg connection so the rest of the code can
    keep writing ? placeholders and calling conn.execute(sql, params)."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        # ? never appears inside a string literal in our SQL, so a plain
        # replace is safe; data values travel in params, untouched.
        return self._conn.execute(sql.replace("?", "%s"), params)

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


@contextmanager
def db():
    """One short-lived connection per call. Commits on success."""
    if USE_PG:
        import psycopg
        from psycopg.rows import dict_row
        conn = _PGConn(psycopg.connect(DATABASE_URL, row_factory=dict_row,
                                       connect_timeout=10))
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _insert_and_get_id(conn, sql, params):
    """Run an INSERT and return the new row's id, on either backend."""
    if USE_PG:
        return conn.execute(sql + " RETURNING id", params).fetchone()["id"]
    return conn.execute(sql, params).lastrowid


def init_db():
    with db() as conn:
        if USE_PG:
            for stmt in (s.strip() for s in SCHEMA_PG.split(";")):
                if stmt:
                    conn.execute(stmt)
        else:
            conn.executescript(SCHEMA)
    _pg_backfill_id_sequences()


def now_iso():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- passwords ---
def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()
    return f"{salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, digest = stored.split("$")
    except ValueError:
        return False
    check = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()
    return hmac.compare_digest(check, digest)


# ---------------------------------------------------------------- providers --
def ensure_referral_columns():
    """referral_code: this provider's 'give a month, get a month' code.
    referred_by: provider_id of whoever referred them (nullable)."""
    _ensure_columns("providers", ("referral_code", "referred_by"),
                    lambda col: f"ALTER TABLE providers ADD COLUMN {col} "
                                + ("TEXT" if col == "referral_code" else "INTEGER"))


def ensure_attribution_columns():
    """heard_about: signup dropdown answer ('postcard', 'walkin', ...).
    signup_source: 'postcard' when the visitor arrived via the /postcard
    QR landing page (cookie attribution), else ''."""
    _ensure_columns("providers", ("heard_about", "signup_source"),
                    lambda col: f"ALTER TABLE providers ADD COLUMN {col} TEXT")


def ensure_postcard_visits():
    with db() as conn:
        conn.execute(pg_ddl(
            """CREATE TABLE IF NOT EXISTS postcard_visits (
                   id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
                   ip_hash TEXT NOT NULL, ua TEXT NOT NULL)"""))


def log_postcard_visit(ip: str, ua: str):
    """Record one hit on the /postcard QR landing page. IP is hashed."""
    ensure_postcard_visits()
    ip_hash = hashlib.sha256((ip or "").encode()).hexdigest()[:16]
    with db() as conn:
        conn.execute("INSERT INTO postcard_visits (ts, ip_hash, ua) VALUES (?,?,?)",
                     (now_iso(), ip_hash, (ua or "")[:200]))


def count_postcard_visits() -> int:
    ensure_postcard_visits()
    with db() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM postcard_visits").fetchone()
        return row["c"] if row else 0


def admin_attribution_list():
    """Providers newest-first with attribution info + subscription status."""
    ensure_attribution_columns()
    with db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT p.id, p.name, p.email, p.company, p.created_at,"
            " p.heard_about, p.signup_source,"
            " s.plan AS sub_plan, s.status AS sub_status"
            " FROM providers p LEFT JOIN subscriptions s ON s.provider_id = p.id"
            " ORDER BY p.created_at DESC").fetchall()]


def generate_referral_code() -> str:
    """Unique, human-friendly code like TP-7K2Q9M (no ambiguous chars)."""
    import random
    ensure_referral_columns()
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    with db() as conn:
        for _ in range(20):
            code = "TP-" + "".join(random.choice(alphabet) for _ in range(6))
            exists = conn.execute("SELECT 1 FROM providers WHERE referral_code = ?",
                                  (code,)).fetchone()
            if not exists:
                return code
    raise RuntimeError("could not generate a unique referral code")


def backfill_referral_codes():
    """Give every existing provider a code (runs once; cheap no-op after)."""
    ensure_referral_columns()
    with db() as conn:
        rows = conn.execute("SELECT id FROM providers WHERE referral_code IS NULL"
                            " OR referral_code = ''").fetchall()
        import random
        alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
        for r in rows:
            for _ in range(20):
                code = "TP-" + "".join(random.choice(alphabet) for _ in range(6))
                if not conn.execute("SELECT 1 FROM providers WHERE referral_code = ?",
                                    (code,)).fetchone():
                    conn.execute("UPDATE providers SET referral_code = ? WHERE id = ?",
                                 (code, r["id"]))
                    break


def get_provider_by_referral_code(code):
    ensure_referral_columns()
    code = (code or "").strip().upper()
    if not code:
        return None
    with db() as conn:
        return conn.execute("SELECT * FROM providers WHERE referral_code = ?",
                            (code,)).fetchone()


def set_referred_by(provider_id, referrer_id):
    ensure_referral_columns()
    with db() as conn:
        conn.execute("UPDATE providers SET referred_by = ? WHERE id = ?",
                     (referrer_id, provider_id))


def ensure_referral_rewards():
    with db() as conn:
        conn.execute(pg_ddl(
            """CREATE TABLE IF NOT EXISTS referral_rewards (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   referrer_id INTEGER NOT NULL,
                   referee_id INTEGER NOT NULL UNIQUE,
                   granted_at TEXT NOT NULL DEFAULT ''
               )"""))


def referral_reward_granted(referee_id) -> bool:
    ensure_referral_rewards()
    with db() as conn:
        return bool(conn.execute("SELECT 1 FROM referral_rewards WHERE referee_id = ?",
                                 (referee_id,)).fetchone())


def record_referral_reward(referrer_id, referee_id):
    ensure_referral_rewards()
    with db() as conn:
        conn.execute("INSERT INTO referral_rewards (referrer_id, referee_id, granted_at)"
                     " VALUES (?,?,?) ON CONFLICT(referee_id) DO NOTHING",
                     (referrer_id, referee_id, now_iso()))


def get_referred_by(provider_id):
    """provider_id of whoever referred this provider, or None."""
    ensure_referral_columns()
    with db() as conn:
        row = conn.execute("SELECT referred_by FROM providers WHERE id = ?",
                           (provider_id,)).fetchone()
    try:
        return row["referred_by"] or None
    except (KeyError, IndexError, TypeError):
        return None


def referral_stats(provider_id) -> dict:
    """How many free months this provider earned by referring others."""
    ensure_referral_columns()
    ensure_referral_rewards()
    with db() as conn:
        code = conn.execute("SELECT referral_code FROM providers WHERE id = ?",
                            (provider_id,)).fetchone()
        earned = conn.execute("SELECT COUNT(*) AS c FROM referral_rewards"
                              " WHERE referrer_id = ?", (provider_id,)).fetchone()
    return {"code": code["referral_code"] if code else "",
            "earned_months": earned["c"] if earned else 0}


def create_provider(name, email, password, company="", heard_about="", signup_source=""):
    ensure_admin_columns()
    ensure_templates_es_columns()
    ensure_referral_columns()
    ensure_attribution_columns()
    code = generate_referral_code()
    with db() as conn:
        provider_id = _insert_and_get_id(
            conn,
            "INSERT INTO providers (name, email, password_hash, created_at, company,"
            " referral_code, heard_about, signup_source) VALUES (?,?,?,?,?,?,?,?)",
            (name, email.strip().lower(), hash_password(password), now_iso(),
             (company or "").strip() or None, code,
             (heard_about or "").strip()[:40] or None,
             (signup_source or "").strip()[:40] or None),
        )
        # Everyone starts with the default message wording; editable in Settings.
        conn.execute(
            "INSERT INTO templates (provider_id, tpl_before, tpl_due, tpl_late3, tpl_late7,"
            " tpl_before_es, tpl_due_es, tpl_late3_es, tpl_late7_es)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (provider_id, DEFAULT_TEMPLATES["tpl_before"], DEFAULT_TEMPLATES["tpl_due"],
             DEFAULT_TEMPLATES["tpl_late3"], DEFAULT_TEMPLATES["tpl_late7"],
             DEFAULT_TEMPLATES_ES["tpl_before_es"], DEFAULT_TEMPLATES_ES["tpl_due_es"],
             DEFAULT_TEMPLATES_ES["tpl_late3_es"], DEFAULT_TEMPLATES_ES["tpl_late7_es"]),
        )
        return provider_id


def get_provider_by_email(email):
    with db() as conn:
        return conn.execute("SELECT * FROM providers WHERE email = ?",
                            (email.strip().lower(),)).fetchone()


def get_provider(provider_id):
    with db() as conn:
        return conn.execute("SELECT * FROM providers WHERE id = ?",
                            (provider_id,)).fetchone()


def all_providers():
    with db() as conn:
        return conn.execute("SELECT * FROM providers ORDER BY id").fetchall()


# ---------------------------------------------------------------- sessions ---
def create_session(provider_id):
    token = secrets.token_hex(32)
    with db() as conn:
        conn.execute("INSERT INTO sessions (token, provider_id, created_at) VALUES (?,?,?)",
                     (token, provider_id, now_iso()))
    return token


def get_provider_by_session(token):
    if not token:
        return None
    with db() as conn:
        row = conn.execute(
            "SELECT l.* FROM providers l JOIN sessions s ON s.provider_id = l.id"
            " WHERE s.token = ?", (token,)).fetchone()
        return row


def delete_session(token):
    with db() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


def delete_provider_sessions(provider_id):
    """Kill every active login session for a provider (used on suspension)."""
    with db() as conn:
        conn.execute("DELETE FROM sessions WHERE provider_id = ?", (provider_id,))


# --------------------------------------------------------------- locations --
def ensure_location_payment_column():
    """payment_url: the owner's online-payment link, appended via {pay_link}."""
    _ensure_columns("locations", ("payment_url",),
                    lambda col: f"ALTER TABLE locations ADD COLUMN {col} TEXT")


def create_location(provider_id, name, address="", payment_url=""):
    ensure_location_payment_column()
    with db() as conn:
        return _insert_and_get_id(
            conn,
            "INSERT INTO locations (provider_id, name, address, payment_url)"
            " VALUES (?,?,?,?)",
            (provider_id, name, address, (payment_url or "").strip()[:500]))


def list_locations(provider_id):
    ensure_location_payment_column()
    ensure_location_fee_columns()
    ensure_pickup_fee_column()
    with db() as conn:
        return conn.execute(
            "SELECT * FROM locations WHERE provider_id = ? ORDER BY id",
            (provider_id,)).fetchall()


def count_locations_for_provider(provider_id) -> int:
    """Total locations owned by a provider (for plan caps)."""
    ensure_location_payment_column()
    with db() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM locations WHERE provider_id = ?",
                           (provider_id,)).fetchone()
        return row["c"] if row else 0


def get_location(location_id, provider_id):
    ensure_location_payment_column()
    ensure_location_fee_columns()
    with db() as conn:
        return conn.execute(
            "SELECT * FROM locations WHERE id = ? AND provider_id = ?",
            (location_id, provider_id)).fetchone()


def update_location(location_id, provider_id, name: str, address: str):
    """Update a location's name and address (validated ownership)."""
    with db() as conn:
        conn.execute("UPDATE locations SET name = ?, address = ?"
                     " WHERE id = ? AND provider_id = ?",
                     ((name or "").strip()[:200], (address or "").strip()[:300],
                      location_id, provider_id))


def set_location_payment_url(location_id, provider_id, payment_url: str):
    """Set the online-payment link for one location (validated ownership)."""
    ensure_location_payment_column()
    url = (payment_url or "").strip()[:500]
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    with db() as conn:
        conn.execute("UPDATE locations SET payment_url = ?"
                     " WHERE id = ? AND provider_id = ?",
                     (url, location_id, provider_id))


def location_payment_urls(provider_id) -> dict:
    """location_id -> payment_url for the reminder engine (one query)."""
    return {loc["id"]: (loc["payment_url"] or "")
            for loc in list_locations(provider_id)}


def ensure_location_fee_columns():
    """late_fee: $ amount auto-added when tuition goes unpaid past the grace
    period. late_fee_days: grace days after the due date (0 = feature off)."""
    _ensure_columns("locations", ("late_fee", "late_fee_days"),
                    lambda col: f"ALTER TABLE locations ADD COLUMN {col} "
                                f"{'REAL DEFAULT 0' if col == 'late_fee' else 'INTEGER DEFAULT 0'}")


def set_location_late_fee(location_id, provider_id, amount, days):
    """Configure the automatic late fee for one location (validated ownership)."""
    ensure_location_fee_columns()
    try:
        amount = max(0.0, float(amount or 0))
    except (TypeError, ValueError):
        amount = 0.0
    try:
        days = max(0, int(days or 0))
    except (TypeError, ValueError):
        days = 0
    with db() as conn:
        conn.execute("UPDATE locations SET late_fee = ?, late_fee_days = ?"
                     " WHERE id = ? AND provider_id = ?",
                     (amount, days, location_id, provider_id))


def location_late_fees(provider_id) -> dict:
    """location_id -> (late_fee, late_fee_days) for the reminder engine."""
    ensure_location_fee_columns()
    out = {}
    for loc in list_locations(provider_id):
        try:
            out[loc["id"]] = (float(loc["late_fee"] or 0), int(loc["late_fee_days"] or 0))
        except (KeyError, IndexError, TypeError, ValueError):
            out[loc["id"]] = (0.0, 0)
    return out


def ensure_late_fees():
    with db() as conn:
        conn.execute(pg_ddl(
            """CREATE TABLE IF NOT EXISTS late_fees (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   family_id INTEGER NOT NULL,
                   period TEXT NOT NULL,
                   amount REAL NOT NULL,
                   applied_at TEXT NOT NULL,
                   UNIQUE(family_id, period)
               )"""))


def get_late_fee(family_id, period) -> float:
    """Late fee already applied for this family+period (0 if none)."""
    ensure_late_fees()
    with db() as conn:
        row = conn.execute("SELECT amount FROM late_fees WHERE family_id = ?"
                           " AND period = ?", (family_id, period)).fetchone()
    try:
        return float(row["amount"]) if row else 0.0
    except (KeyError, IndexError, TypeError, ValueError):
        return 0.0


def apply_late_fee(family_id, period, amount) -> bool:
    """Record a late fee once per family+period. True if newly applied."""
    ensure_late_fees()
    with db() as conn:
        if USE_PG:
            cur = conn.execute(
                "INSERT INTO late_fees (family_id, period, amount, applied_at)"
                " VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
                (family_id, period, amount, now_iso()))
            return cur.rowcount > 0
        cur = conn.execute(
            "INSERT OR IGNORE INTO late_fees (family_id, period, amount, applied_at)"
            " VALUES (?,?,?,?)",
            (family_id, period, amount, now_iso()))
        return cur.rowcount > 0


# --------------------------------------------------------------- broadcast --
BROADCAST_DAILY_LIMIT = 5


def ensure_broadcast_log():
    with db() as conn:
        conn.execute(pg_ddl(
            """CREATE TABLE IF NOT EXISTS broadcast_log (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   provider_id INTEGER NOT NULL,
                   sent_at TEXT NOT NULL,
                   recipient_count INTEGER NOT NULL DEFAULT 0
               )"""))


def count_broadcasts_today(provider_id, day_iso) -> int:
    ensure_broadcast_log()
    with db() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM broadcast_log"
                           " WHERE provider_id = ? AND substr(sent_at, 1, 10) = ?",
                           (provider_id, day_iso)).fetchone()
    try:
        return int(row["n"])
    except (KeyError, IndexError, TypeError, ValueError):
        return 0


def log_broadcast(provider_id, recipient_count):
    ensure_broadcast_log()
    with db() as conn:
        conn.execute("INSERT INTO broadcast_log (provider_id, sent_at, recipient_count)"
                     " VALUES (?,?,?)",
                     (provider_id, now_iso(), recipient_count))


def get_broadcast_recipients(provider_id) -> list:
    """All opted-in families with a phone number (primary + 2nd parent)."""
    ensure_family_comms_columns()
    with db() as conn:
        rows = conn.execute(
            """SELECT f.id, f.phone, f.phone2 FROM families f
               JOIN classrooms c ON c.id = f.classroom_id
               JOIN locations l ON l.id = c.location_id
               WHERE l.provider_id = ? AND f.opted_out = 0""",
            (provider_id,)).fetchall()
    recipients = []
    for r in rows:
        try:
            phones = [p for p in (r["phone"], r["phone2"]) if p]
        except (KeyError, IndexError, TypeError):
            continue
        for p in phones:
            recipients.append((r["id"], p))
    return recipients


# ------------------------------------------------------------ review nudge --
REVIEW_NUDGE_PAYMENTS = 3  # ask for a review after this many logged payments


def ensure_review_columns():
    """review_url: the provider's Google review link, texted after N payments."""
    _ensure_columns("providers", ("review_url",),
                    lambda col: f"ALTER TABLE providers ADD COLUMN {col} TEXT")


def get_review_url(provider_id) -> str:
    ensure_review_columns()
    with db() as conn:
        row = conn.execute("SELECT review_url FROM providers WHERE id = ?",
                           (provider_id,)).fetchone()
    try:
        return (row["review_url"] or "").strip()
    except (KeyError, IndexError, TypeError):
        return ""


def set_review_url(provider_id, url: str):
    ensure_review_columns()
    url = (url or "").strip()[:500]
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    with db() as conn:
        conn.execute("UPDATE providers SET review_url = ? WHERE id = ?",
                     (url or None, provider_id))


def ensure_review_nudges():
    with db() as conn:
        conn.execute(pg_ddl(
            """CREATE TABLE IF NOT EXISTS review_nudges (
                   family_id INTEGER PRIMARY KEY,
                   sent_at TEXT NOT NULL
               )"""))


def review_nudge_sent(family_id) -> bool:
    ensure_review_nudges()
    with db() as conn:
        return conn.execute("SELECT 1 FROM review_nudges WHERE family_id = ?",
                            (family_id,)).fetchone() is not None


def record_review_nudge(family_id):
    ensure_review_nudges()
    with db() as conn:
        if USE_PG:
            conn.execute("INSERT INTO review_nudges (family_id, sent_at)"
                         " VALUES (?,?) ON CONFLICT DO NOTHING",
                         (family_id, now_iso()))
        else:
            conn.execute("INSERT OR IGNORE INTO review_nudges (family_id, sent_at)"
                         " VALUES (?,?)", (family_id, now_iso()))


def count_family_payments(family_id) -> int:
    ensure_paid_log()
    with db() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM paid_log WHERE family_id = ?",
                           (family_id,)).fetchone()
    try:
        return int(row["n"])
    except (KeyError, IndexError, TypeError, ValueError):
        return 0


# -------------------------------------------------------------------- classrooms --
def create_classroom(location_id, label):
    with db() as conn:
        return _insert_and_get_id(
            conn,
            "INSERT INTO classrooms (location_id, label) VALUES (?,?)",
            (location_id, label))


def list_classrooms(location_id):
    with db() as conn:
        return conn.execute("SELECT * FROM classrooms WHERE location_id = ? ORDER BY id",
                            (location_id,)).fetchall()


# ---------------------------------------------------------------- admin --
def ensure_admin_columns():
    """Add suspended flag + company name to providers if missing."""
    _ensure_columns("providers", ("suspended", "company"),
                    lambda col: f"ALTER TABLE providers ADD COLUMN {col} "
                                f"{'INTEGER DEFAULT 0' if col == 'suspended' else 'TEXT'}")


def set_provider_suspended(provider_id, suspended: bool):
    ensure_admin_columns()
    with db() as conn:
        conn.execute("UPDATE providers SET suspended = ? WHERE id = ?",
                     (1 if suspended else 0, provider_id))


def set_company(provider_id, company: str):
    ensure_admin_columns()
    with db() as conn:
        conn.execute("UPDATE providers SET company = ? WHERE id = ?",
                     ((company or "").strip() or None, provider_id))


def ensure_tax_id_column():
    """tax_id: provider's EIN, printed on family tax statements if set."""
    _ensure_columns("providers", ("tax_id",),
                    lambda col: f"ALTER TABLE providers ADD COLUMN {col} TEXT")


def set_tax_id(provider_id, tax_id: str):
    ensure_tax_id_column()
    with db() as conn:
        conn.execute("UPDATE providers SET tax_id = ? WHERE id = ?",
                     ((tax_id or "").strip() or None, provider_id))


def get_tax_id(provider_id):
    ensure_tax_id_column()
    with db() as conn:
        row = conn.execute("SELECT tax_id FROM providers WHERE id = ?",
                           (provider_id,)).fetchone()
    try:
        return row["tax_id"] or ""
    except (KeyError, IndexError, TypeError):
        return ""


def get_family_payments_for_year(family_id, year: int):
    """Every logged payment for one family in a calendar year, oldest first.
    Filters in Python so it works identically on SQLite and Postgres."""
    ensure_paid_log()
    prefix = f"{year:04d}"
    with db() as conn:
        rows = conn.execute(
            "SELECT period, amount, paid_at FROM paid_log"
            " WHERE family_id = ? ORDER BY paid_at",
            (family_id,)).fetchall()
    return [dict(r) for r in rows if (r["paid_at"] or "")[:4] == prefix]


def ensure_email_columns():
    """email_verified on providers + email_verifications table.

    New signups start unverified (0) and stay that way until they click
    the verification link. (The one-time grandfathering of pre-existing
    accounts ran when this feature first deployed; it must NOT re-run,
    or it would silently verify accounts that never clicked the link.)
    """
    _ensure_columns("providers", ("email_verified",),
                    lambda col: f"ALTER TABLE providers ADD COLUMN {col} INTEGER DEFAULT 0")
    with db() as conn:
        ddl = """CREATE TABLE IF NOT EXISTS email_verifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider_id INTEGER NOT NULL,
                token TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                used INTEGER NOT NULL DEFAULT 0
            )"""
        if USE_PG:
            ddl = ddl.replace("INTEGER PRIMARY KEY AUTOINCREMENT",
                              "SERIAL PRIMARY KEY")
        conn.execute(ddl)


def create_email_token(provider_id):
    import secrets
    ensure_email_columns()
    token = secrets.token_urlsafe(32)
    with db() as conn:
        conn.execute(
            "INSERT INTO email_verifications (provider_id, token, created_at) "
            "VALUES (?,?,?)", (provider_id, token, now_iso()))
    return token


def verify_email_token(token):
    """Returns provider_id if the token is valid and fresh, else None."""
    ensure_email_columns()
    from datetime import datetime, timedelta, timezone
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM email_verifications WHERE token = ? AND used = 0",
            (token,)).fetchone()
        if not row:
            return None
        row = dict(row)
        age = (datetime.now(timezone.utc) -
               datetime.fromisoformat(row["created_at"])).total_seconds()
        if age > 48 * 3600:
            return None
        conn.execute("UPDATE email_verifications SET used = 1 WHERE id = ?",
                     (row["id"],))
        conn.execute("UPDATE providers SET email_verified = 1 WHERE id = ?",
                     (row["provider_id"],))
        return row["provider_id"]


def set_email_verified(provider_id, verified: bool):
    ensure_email_columns()
    with db() as conn:
        conn.execute("UPDATE providers SET email_verified = ? WHERE id = ?",
                     (1 if verified else 0, provider_id))


# ------------------------------------------------------- password resets ---
def ensure_password_reset_table():
    ddl = """CREATE TABLE IF NOT EXISTS password_resets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id INTEGER NOT NULL,
            token TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            used INTEGER NOT NULL DEFAULT 0
        )"""
    if USE_PG:
        ddl = ddl.replace("INTEGER PRIMARY KEY AUTOINCREMENT",
                          "SERIAL PRIMARY KEY")
    with db() as conn:
        conn.execute(pg_ddl(ddl) if USE_PG else ddl)


def create_password_reset_token(provider_id):
    """Single-use reset token, valid 1 hour. Returns the token string."""
    import secrets
    from datetime import datetime, timezone
    ensure_password_reset_table()
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        conn.execute("UPDATE password_resets SET used = 1 WHERE provider_id = ?",
                     (provider_id,))
        conn.execute("INSERT INTO password_resets (provider_id, token, created_at) "
                     "VALUES (?,?,?)", (provider_id, token, now))
    return token


def consume_password_reset_token(token):
    """Returns provider_id if the token is valid and fresh, else None."""
    from datetime import datetime, timedelta, timezone
    ensure_password_reset_table()
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM password_resets WHERE token = ? AND used = 0",
            (token,)).fetchone()
        if not row:
            return None
        row = dict(row)
        age = (datetime.now(timezone.utc) -
               datetime.fromisoformat(row["created_at"])).total_seconds()
        if age > 3600:
            return None
        conn.execute("UPDATE password_resets SET used = 1 WHERE id = ?",
                     (row["id"],))
        return row["provider_id"]


def set_password(provider_id, password: str):
    with db() as conn:
        conn.execute("UPDATE providers SET password_hash = ? WHERE id = ?",
                     (hash_password(password), provider_id))


def update_family(family_id, name, phone, tuition_amount, due_day,
                  next_due_date=None):
    with db() as conn:
        conn.execute(
            "UPDATE families SET name = ?, phone = ?, tuition_amount = ?, "
            "due_day = ?, next_due_date = ? WHERE id = ?",
            (name.strip(), phone.strip(), float(tuition_amount), int(due_day),
             next_due_date, family_id))


def delete_provider(provider_id):
    """Hard-delete a provider and everything they own. Irreversible."""
    ensure_admin_columns()
    ensure_email_columns()
    with db() as conn:
        conn.execute(
            "DELETE FROM reminder_log WHERE family_id IN "
            "(SELECT f.id FROM families f JOIN classrooms c ON c.id = f.classroom_id "
            "JOIN locations l ON l.id = c.location_id WHERE l.provider_id = ?)",
            (provider_id,))
        conn.execute(
            "DELETE FROM families WHERE classroom_id IN "
            "(SELECT c.id FROM classrooms c JOIN locations l ON l.id = c.location_id "
            "WHERE l.provider_id = ?)", (provider_id,))
        conn.execute(
            "DELETE FROM classrooms WHERE location_id IN "
            "(SELECT id FROM locations WHERE provider_id = ?)", (provider_id,))
        for table in ("message_log", "templates", "subscriptions", "sessions",
                      "locations", "email_verifications"):
            conn.execute(f"DELETE FROM {table} WHERE provider_id = ?",
                         (provider_id,))
        conn.execute("DELETE FROM providers WHERE id = ?", (provider_id,))


def admin_provider_stats():
    """One row per provider for the abuse-monitoring admin page."""
    ensure_admin_columns()
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    day_ago = now - timedelta(hours=24)
    month_ago = now - timedelta(hours=24 * 30)
    fmt = lambda d: d.isoformat(timespec="seconds")
    fam = ("SELECT COUNT(*) FROM families f "
           "JOIN classrooms c ON c.id = f.classroom_id "
           "JOIN locations l ON l.id = c.location_id "
           "WHERE l.provider_id = p.id")
    with db() as conn:
        rows = conn.execute(
            f"""SELECT p.id, p.name, p.company, p.email, p.created_at, p.suspended,
                       ({fam}) AS families,
                       ({fam} AND f.created_at >= ?) AS families_24h,
                       (SELECT COUNT(*) FROM message_log m
                         WHERE m.provider_id = p.id AND m.direction = 'out'
                           AND m.created_at >= ?) AS sent_24h,
                       (SELECT COUNT(*) FROM message_log m
                         WHERE m.provider_id = p.id AND m.direction = 'out'
                           AND m.created_at >= ?) AS sent_30d,
                       ({fam} AND f.opted_out = 1) AS optouts
                FROM providers p ORDER BY p.created_at DESC""",
            (fmt(day_ago), fmt(day_ago), fmt(month_ago))).fetchall()
        loc_rows = conn.execute(
            "SELECT provider_id, name FROM locations ORDER BY name").fetchall()
        sub_rows = conn.execute(
            "SELECT provider_id, plan, status, trial_ends_at, founding FROM subscriptions"
        ).fetchall()
    stats = [dict(r) for r in rows]
    locs = {}
    for r in loc_rows:
        r = dict(r)
        locs.setdefault(r["provider_id"], []).append(r["name"])
    subs = {dict(r)["provider_id"]: dict(r) for r in sub_rows}
    for s in stats:
        s["locations"] = locs.get(s["id"], [])
        s["subscription"] = subs.get(s["id"])
    return stats


# ------------------------------------------------------- sending settings --
SENDING_DEFAULTS = {"timezone": "America/Los_Angeles",
                    "quiet_start": "07:00", "quiet_end": "21:00"}


def ensure_sending_columns():
    """Add timezone / quiet_start / quiet_end to providers if missing."""
    _ensure_columns("providers", ("timezone", "quiet_start", "quiet_end"),
                    lambda col: f"ALTER TABLE providers ADD COLUMN {col} TEXT")


def get_sending_settings(provider_id):
    ensure_sending_columns()
    with db() as conn:
        row = conn.execute(
            "SELECT timezone, quiet_start, quiet_end FROM providers WHERE id = ?",
            (provider_id,)).fetchone()
    d = dict(SENDING_DEFAULTS)
    if row:
        for k in d:
            if row[k]:
                d[k] = row[k]
    return d


def save_sending_settings(provider_id, timezone_, quiet_start, quiet_end):
    ensure_sending_columns()
    with db() as conn:
        conn.execute("UPDATE providers SET timezone=?, quiet_start=?, quiet_end=?"
                     " WHERE id = ?",
                     (timezone_, quiet_start, quiet_end, provider_id))


# ------------------------------------------------------------------ families --
def ensure_next_due_column():
    """Add next_due_date to families if missing (one-time first-bill override)."""
    _ensure_columns("families", ("next_due_date",),
                    lambda col: f"ALTER TABLE families ADD COLUMN {col} TEXT")


def clear_next_due_date(family_id):
    ensure_next_due_column()
    with db() as conn:
        conn.execute("UPDATE families SET next_due_date = NULL WHERE id = ?",
                     (family_id,))


def ensure_consent_column():
    """Add consent_at to families if missing (provider attests to SMS consent)."""
    _ensure_columns("families", ("consent_at",),
                    lambda col: f"ALTER TABLE families ADD COLUMN {col} TEXT")


def create_family(classroom_id, name, phone, tuition_amount, due_day, next_due_date=None,
                phone2=None, language="en"):
    ensure_consent_column()
    ensure_next_due_column()
    ensure_family_comms_columns()
    lang = "es" if language == "es" else "en"
    with db() as conn:
        ts = now_iso()
        return _insert_and_get_id(
            conn,
            "INSERT INTO families (classroom_id, name, phone, tuition_amount, due_day, created_at, consent_at, next_due_date, phone2, language)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (classroom_id, name, phone.strip(), float(tuition_amount), int(due_day), ts, ts,
             next_due_date, (phone2 or "").strip() or None, lang))


def get_family(family_id):
    with db() as conn:
        return conn.execute("SELECT * FROM families WHERE id = ?", (family_id,)).fetchone()


def get_family_for_provider(family_id, provider_id):
    """A family row only if it belongs to this provider (stops ID tampering)."""
    with db() as conn:
        return conn.execute(
            """SELECT t.* FROM families t
               JOIN classrooms u ON u.id = t.classroom_id
               JOIN locations p ON p.id = u.location_id
               WHERE t.id = ? AND p.provider_id = ?""",
            (family_id, provider_id)).fetchone()


def get_classroom_for_provider(classroom_id, provider_id):
    """A classroom row only if it belongs to this provider (stops ID tampering)."""
    with db() as conn:
        return conn.execute(
            """SELECT u.* FROM classrooms u
               JOIN locations p ON p.id = u.location_id
               WHERE u.id = ? AND p.provider_id = ?""",
            (classroom_id, provider_id)).fetchone()


def all_families(provider_id):
    """Every family of a provider, with classroom + location context attached."""
    ensure_phone_bad_column()
    ensure_family_welcome_columns()
    ensure_family_notes_column()
    ensure_discount_column()
    ensure_statement_token_column()
    ensure_child_columns()
    ensure_paid_source_column()
    with db() as conn:
        return conn.execute(
            """SELECT t.*, u.label AS classroom_label, p.name AS location_name, p.id AS location_id
               FROM families t
               JOIN classrooms u ON u.id = t.classroom_id
               JOIN locations p ON p.id = u.location_id
               WHERE p.provider_id = ? ORDER BY t.id""",
            (provider_id,)).fetchall()


def list_families(classroom_id):
    ensure_phone_bad_column()
    ensure_family_welcome_columns()
    ensure_family_notes_column()
    ensure_discount_column()
    ensure_statement_token_column()
    ensure_child_columns()
    with db() as conn:
        return conn.execute("SELECT * FROM families WHERE classroom_id = ? ORDER BY id",
                            (classroom_id,)).fetchall()


def count_families_for_provider(provider_id):
    """Total families across all of a provider's locations (for plan caps)."""
    with db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM families f "
            "JOIN classrooms c ON c.id = f.classroom_id "
            "JOIN locations l ON l.id = c.location_id "
            "WHERE l.provider_id = ?", (provider_id,)).fetchone()
        return row["c"] if row else 0


def get_family_by_phone(provider_id, phone):
    """Match an inbound text to a family. Compares last 10 digits so
    +1 (555) 123-4567 matches 5551234567. Checks the second parent number too."""
    ensure_family_comms_columns()
    digits = "".join(c for c in phone if c.isdigit())[-10:]
    for t in all_families(provider_id):
        cands = [t["phone"]]
        try:
            if t["phone2"]:
                cands.append(t["phone2"])
        except (KeyError, IndexError):
            pass
        for cand in cands:
            if not cand:
                continue
            t_digits = "".join(c for c in cand if c.isdigit())[-10:]
            if t_digits and t_digits == digits:
                return t
    return None


def ensure_paid_source_column():
    _ensure_columns("families", ("paid_source",),
                    lambda col: f"ALTER TABLE families ADD COLUMN {col} TEXT DEFAULT ''")


def mark_family_paid(family_id, period, source="manual"):
    """Mark the period paid, settle outstanding extra charges, and log the
    payment. Returns the amount logged (discounted tuition + charges).
    source: 'reply' when a parent texted PAID (self-reported),
    'manual' when the provider marked it by hand (verified)."""
    ensure_paid_log()
    ensure_extra_charges_table()
    ensure_discount_column()
    ensure_paid_source_column()
    with db() as conn:
        conn.execute("UPDATE families SET paid_period = ?, paid_source = ? WHERE id = ?",
                     (period, source, family_id))
        row = conn.execute("SELECT tuition_amount, discount_pct FROM families"
                           " WHERE id = ?", (family_id,)).fetchone()
        amount = effective_tuition(row) if row else 0
        charges = conn.execute("SELECT COALESCE(SUM(amount),0) s FROM extra_charges"
                               " WHERE family_id = ? AND settled_period IS NULL",
                               (family_id,)).fetchone()["s"]
        amount += float(charges or 0)
        conn.execute("UPDATE extra_charges SET settled_period = ?"
                     " WHERE family_id = ? AND settled_period IS NULL",
                     (period, family_id))
        conn.execute(
            "INSERT INTO paid_log (family_id, period, amount, paid_at)"
            " VALUES (?,?,?,?) ON CONFLICT(family_id, period) DO NOTHING",
            (family_id, period, amount, now_iso()))
        return amount


def set_family_opt_out(family_id, opted_out):
    with db() as conn:
        conn.execute("UPDATE families SET opted_out = ? WHERE id = ?",
                     (1 if opted_out else 0, family_id))


def delete_family(family_id):
    with db() as conn:
        conn.execute("DELETE FROM families WHERE id = ?", (family_id,))


# -------------------------------------------------------------- message log --
def log_message(provider_id, family_id, direction, body, status, twilio_sid=""):
    ensure_message_sid_column()
    with db() as conn:
        conn.execute(
            "INSERT INTO message_log (provider_id, family_id, direction, body, status,"
            " twilio_sid, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (provider_id, family_id, direction, body, status, twilio_sid or "",
             now_iso()))


def ensure_message_sid_column():
    """twilio_sid: Twilio message SID, for matching delivery-status callbacks."""
    _ensure_columns("message_log", ("twilio_sid",),
                    lambda col: f"ALTER TABLE message_log ADD COLUMN {col} TEXT")


def get_message_by_sid(twilio_sid):
    ensure_message_sid_column()
    with db() as conn:
        return conn.execute("SELECT * FROM message_log WHERE twilio_sid = ?"
                            " ORDER BY id DESC LIMIT 1", (twilio_sid,)).fetchone()


def ensure_phone_bad_column():
    """phone_bad: Twilio reported this number undeliverable/failed."""
    _ensure_columns("families", ("phone_bad",),
                    lambda col: f"ALTER TABLE families ADD COLUMN {col} INTEGER DEFAULT 0")


def ensure_family_welcome_columns():
    """welcomed_at: welcome text sent when the family was added.
    confirmed_at: the family replied to at least one text (number is live)."""
    _ensure_columns("families", ("welcomed_at", "confirmed_at"),
                    lambda col: f"ALTER TABLE families ADD COLUMN {col} TEXT")


def ensure_family_notes_column():
    """notes: free-text note per family (kid's name, pickup details...)."""
    _ensure_columns("families", ("notes",),
                    lambda col: f"ALTER TABLE families ADD COLUMN {col} TEXT")


def ensure_discount_column():
    """discount_pct: sibling/multi-child discount, 0-100."""
    _ensure_columns("families", ("discount_pct",),
                    lambda col: f"ALTER TABLE families ADD COLUMN {col} REAL DEFAULT 0")


def ensure_statement_token_column():
    """statement_token: unguessable public token so parents can open their
    own tax statement from a texted link, without a login."""
    _ensure_columns("families", ("statement_token",),
                    lambda col: f"ALTER TABLE families ADD COLUMN {col} TEXT")


def ensure_child_columns():
    """child_name: the kid's first name (birthday texts, absence replies).
    child_birthday: 'MM-DD' for the yearly birthday text.
    immunization_expires: 'YYYY-MM-DD' the child's immunization record lapses."""
    _ensure_columns("families", ("child_name", "child_birthday",
                                 "immunization_expires"),
                    lambda col: f"ALTER TABLE families ADD COLUMN {col} TEXT")


def set_immunization_expires(family_id, date_iso: str):
    """Save the immunization-record expiry as YYYY-MM-DD ('' clears it).
    Updating the date restarts the reminder cycle for that family."""
    ensure_child_columns()
    ensure_immunization_log()
    d = (date_iso or "").strip()[:10]
    from datetime import date as _date
    try:
        _date.fromisoformat(d)
    except ValueError:
        d = ""
    with db() as conn:
        conn.execute("UPDATE families SET immunization_expires = ? WHERE id = ?",
                     (d or None, family_id))
        conn.execute("DELETE FROM immunization_log WHERE family_id = ?",
                     (family_id,))


def ensure_immunization_log():
    with db() as conn:
        conn.execute(pg_ddl(
            """CREATE TABLE IF NOT EXISTS immunization_log (
                   id INTEGER PRIMARY KEY AUTOINCREMENT, family_id INTEGER NOT NULL,
                   kind TEXT NOT NULL)"""))
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_immunization_once"
                     " ON immunization_log (family_id, kind)")


def immunization_notice_sent(family_id, kind: str) -> bool:
    ensure_immunization_log()
    with db() as conn:
        return conn.execute("SELECT 1 FROM immunization_log WHERE family_id = ?"
                            " AND kind = ?", (family_id, kind)).fetchone() is not None


def mark_immunization_notice(family_id, kind: str):
    ensure_immunization_log()
    with db() as conn:
        conn.execute("INSERT INTO immunization_log (family_id, kind) VALUES (?,?)"
                     " ON CONFLICT(family_id, kind) DO NOTHING", (family_id, kind))


def families_with_docs_due(provider_id, day_iso: str, within_days: int = 30):
    """(family, days_until_expiry) for records expired or expiring soon."""
    ensure_child_columns()
    from datetime import date as _date
    try:
        day = _date.fromisoformat(day_iso)
    except ValueError:
        return []
    out = []
    for family in all_families(provider_id):
        try:
            raw = (family["immunization_expires"] or "").strip()
        except (KeyError, IndexError, TypeError):
            continue
        if not raw:
            continue
        try:
            delta = (_date.fromisoformat(raw) - day).days
        except ValueError:
            continue
        if delta <= within_days:
            out.append((family, delta))
    return out


def set_child_info(family_id, child_name: str, child_birthday: str):
    """child_birthday as 'MM-DD' (or '' to clear). Invalid values clear it."""
    ensure_child_columns()
    mmdd = (child_birthday or "").strip()
    import re
    if mmdd and not re.fullmatch(r"(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])", mmdd):
        mmdd = ""
    with db() as conn:
        conn.execute("UPDATE families SET child_name = ?, child_birthday = ?"
                     " WHERE id = ?",
                     ((child_name or "").strip()[:40] or None,
                      mmdd or None, family_id))


def child_display_name(family, lang: str = "en") -> str:
    """Kid's first name, or a generic fallback in the family's language."""
    try:
        name = (family["child_name"] or "").strip()
    except (KeyError, IndexError, TypeError):
        name = ""
    if name:
        return name
    return "su hijo/a" if lang == "es" else "your child"


def ensure_birthday_log():
    with db() as conn:
        conn.execute(pg_ddl(
            """CREATE TABLE IF NOT EXISTS birthday_log (
                   id INTEGER PRIMARY KEY AUTOINCREMENT, family_id INTEGER NOT NULL,
                   year INTEGER NOT NULL)"""))
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_birthday_once"
                     " ON birthday_log (family_id, year)")


def birthday_sent_this_year(family_id, year: int) -> bool:
    ensure_birthday_log()
    with db() as conn:
        return conn.execute("SELECT 1 FROM birthday_log WHERE family_id = ?"
                            " AND year = ?", (family_id, year)).fetchone() is not None


def mark_birthday_sent(family_id, year: int):
    ensure_birthday_log()
    with db() as conn:
        conn.execute("INSERT INTO birthday_log (family_id, year) VALUES (?,?)"
                     " ON CONFLICT(family_id, year) DO NOTHING", (family_id, year))


def ensure_absence_log():
    with db() as conn:
        conn.execute(pg_ddl(
            """CREATE TABLE IF NOT EXISTS absence_log (
                   id INTEGER PRIMARY KEY AUTOINCREMENT, family_id INTEGER NOT NULL,
                   date TEXT NOT NULL, reason TEXT NOT NULL)"""))
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_absence_once"
                     " ON absence_log (family_id, date)")


def log_absence(family_id, date_iso: str, reason: str) -> bool:
    """Record one absence per family per day. Returns True if it was new."""
    ensure_absence_log()
    with db() as conn:
        cur = conn.execute("INSERT INTO absence_log (family_id, date, reason)"
                           " VALUES (?,?,?) ON CONFLICT(family_id, date) DO NOTHING",
                           (family_id, date_iso, reason))
        return cur.rowcount > 0


def is_absent_on(family_id, date_iso: str) -> bool:
    ensure_absence_log()
    with db() as conn:
        return conn.execute("SELECT 1 FROM absence_log WHERE family_id = ?"
                            " AND date = ?", (family_id, date_iso)).fetchone() is not None


def get_absences_for_date(provider_id, date_iso: str):
    """(family, reason) for every family of the provider absent on a date."""
    ensure_absence_log()
    with db() as conn:
        return conn.execute(
            "SELECT f.*, a.reason AS absence_reason FROM absence_log a"
            " JOIN families f ON f.id = a.family_id"
            " JOIN classrooms c ON c.id = f.classroom_id"
            " JOIN locations l ON l.id = c.location_id"
            " WHERE l.provider_id = ? AND a.date = ? ORDER BY f.name",
            (provider_id, date_iso)).fetchall()


def ensure_pickup_fee_column():
    """pickup_fee: $ charged per late pickup via the one-tap logger (0 = off)."""
    _ensure_columns("locations", ("pickup_fee",),
                    lambda col: f"ALTER TABLE locations ADD COLUMN {col} REAL DEFAULT 0")


def set_location_pickup_fee(location_id, provider_id, amount):
    ensure_pickup_fee_column()
    try:
        amount = max(0.0, float(amount))
    except (TypeError, ValueError):
        amount = 0.0
    with db() as conn:
        cur = conn.execute("UPDATE locations SET pickup_fee = ?"
                           " WHERE id = ? AND provider_id = ?",
                           (amount, location_id, provider_id))
        return cur.rowcount > 0


def get_or_create_statement_token(family_id) -> str:
    import secrets
    ensure_statement_token_column()
    ensure_child_columns()
    with db() as conn:
        row = conn.execute("SELECT statement_token FROM families WHERE id = ?",
                           (family_id,)).fetchone()
        if row and row["statement_token"]:
            return row["statement_token"]
        token = secrets.token_urlsafe(24)
        conn.execute("UPDATE families SET statement_token = ? WHERE id = ?",
                     (token, family_id))
        return token


def get_family_by_statement_token(token):
    ensure_statement_token_column()
    ensure_child_columns()
    with db() as conn:
        return conn.execute(
            "SELECT f.*, p.id AS provider_id, p.name AS provider_name,"
            " p.company AS provider_company FROM families f"
            " JOIN classrooms c ON c.id = f.classroom_id"
            " JOIN locations l ON l.id = c.location_id"
            " JOIN providers p ON p.id = l.provider_id"
            " WHERE f.statement_token = ?", (token,)).fetchone()


def get_provider_payments_for_year(provider_id, year: int):
    """Every logged payment for a provider in a calendar year, with the
    location each family belongs to. Filters in Python (SQLite + PG safe)."""
    ensure_paid_log()
    prefix = f"{year:04d}"
    with db() as conn:
        rows = conn.execute(
            "SELECT pl.amount, pl.paid_at, pl.period, l.name AS location_name"
            " FROM paid_log pl"
            " JOIN families f ON f.id = pl.family_id"
            " JOIN classrooms c ON c.id = f.classroom_id"
            " JOIN locations l ON l.id = c.location_id"
            " WHERE l.provider_id = ? ORDER BY pl.paid_at",
            (provider_id,)).fetchall()
    return [dict(r) for r in rows if (r["paid_at"] or "")[:4] == prefix]


def ensure_statement_blast_log():
    with db() as conn:
        conn.execute(pg_ddl(
            """CREATE TABLE IF NOT EXISTS statement_blast_log (
                   id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id INTEGER NOT NULL,
                   year INTEGER NOT NULL, family_count INTEGER NOT NULL,
                   sent_at TEXT NOT NULL)"""))


def statement_blast_sent_today(provider_id) -> bool:
    ensure_statement_blast_log()
    day = now_iso()[:10]
    with db() as conn:
        row = conn.execute("SELECT 1 FROM statement_blast_log"
                           " WHERE provider_id = ? AND substr(sent_at,1,10) = ?",
                           (provider_id, day)).fetchone()
        return row is not None


def log_statement_blast(provider_id, year: int, family_count: int):
    ensure_statement_blast_log()
    with db() as conn:
        conn.execute("INSERT INTO statement_blast_log"
                     " (provider_id, year, family_count, sent_at)"
                     " VALUES (?,?,?,?)",
                     (provider_id, year, family_count, now_iso()))


def effective_tuition(family) -> float:
    """What the family actually owes per period after their discount."""
    try:
        pct = float(family["discount_pct"] or 0)
    except (KeyError, IndexError, TypeError, ValueError):
        pct = 0.0
    pct = max(0.0, min(100.0, pct))
    return float(family["tuition_amount"] or 0) * (1 - pct / 100)


def set_family_discount(family_id, pct):
    ensure_discount_column()
    try:
        pct = max(0.0, min(100.0, float(pct)))
    except (TypeError, ValueError):
        pct = 0.0
    with db() as conn:
        conn.execute("UPDATE families SET discount_pct = ? WHERE id = ?",
                     (pct, family_id))


def ensure_extra_charges_table():
    """One-time charges (late pickup, field trip, supplies...). A charge is
    outstanding until the family's period is marked paid, then it settles."""
    with db() as conn:
        conn.execute(pg_ddl(
            """CREATE TABLE IF NOT EXISTS extra_charges (
                   id INTEGER PRIMARY KEY AUTOINCREMENT, family_id INTEGER NOT NULL,
                   label TEXT NOT NULL, amount REAL NOT NULL,
                   created_at TEXT NOT NULL, settled_period TEXT)"""))
        conn.execute("CREATE INDEX IF NOT EXISTS idx_charges_family"
                     " ON extra_charges (family_id, settled_period)")


def add_extra_charge(family_id, label: str, amount: float) -> int:
    ensure_extra_charges_table()
    with db() as conn:
        return _insert_and_get_id(
            conn, "INSERT INTO extra_charges (family_id, label, amount, created_at)"
                  " VALUES (?,?,?,?)",
            (family_id, (label or "Extra charge").strip()[:80], float(amount),
             now_iso()))


def list_extra_charges(family_id):
    """Outstanding charges only (newest first)."""
    ensure_extra_charges_table()
    with db() as conn:
        return conn.execute("SELECT * FROM extra_charges WHERE family_id = ?"
                            " AND settled_period IS NULL ORDER BY id DESC",
                            (family_id,)).fetchall()


def outstanding_charges_total(family_id) -> float:
    return sum(float(c["amount"] or 0) for c in list_extra_charges(family_id))


def remove_extra_charge(charge_id, family_id) -> bool:
    ensure_extra_charges_table()
    with db() as conn:
        cur = conn.execute("DELETE FROM extra_charges WHERE id = ? AND family_id = ?"
                           " AND settled_period IS NULL", (charge_id, family_id))
        return cur.rowcount > 0


def settle_extra_charges(family_id, period):
    ensure_extra_charges_table()
    with db() as conn:
        conn.execute("UPDATE extra_charges SET settled_period = ?"
                     " WHERE family_id = ? AND settled_period IS NULL",
                     (period, family_id))


def set_family_notes(family_id, notes: str):
    ensure_family_notes_column()
    with db() as conn:
        conn.execute("UPDATE families SET notes = ? WHERE id = ?",
                     ((notes or "").strip() or None, family_id))


def ensure_manual_nudge_log():
    with db() as conn:
        conn.execute(pg_ddl(
            """CREATE TABLE IF NOT EXISTS manual_nudge_log (
                   id INTEGER PRIMARY KEY AUTOINCREMENT, family_id INTEGER NOT NULL,
                   day TEXT NOT NULL, created_at TEXT NOT NULL)"""))
        conn.execute("CREATE INDEX IF NOT EXISTS idx_nudge_log_family_day"
                     " ON manual_nudge_log (family_id, day)")


MANUAL_NUDGE_DAILY_LIMIT = 3


def count_manual_nudges_today(family_id, day_iso) -> int:
    ensure_manual_nudge_log()
    with db() as conn:
        row = conn.execute("SELECT COUNT(*) c FROM manual_nudge_log"
                           " WHERE family_id = ? AND day = ?",
                           (family_id, day_iso)).fetchone()
        return row["c"] if row else 0


def log_manual_nudge(family_id, day_iso):
    ensure_manual_nudge_log()
    with db() as conn:
        conn.execute("INSERT INTO manual_nudge_log (family_id, day, created_at)"
                     " VALUES (?, ?, ?)", (family_id, day_iso, now_iso()))


def set_family_welcomed(family_id):
    ensure_family_welcome_columns()
    with db() as conn:
        conn.execute("UPDATE families SET welcomed_at = ? WHERE id = ?"
                     " AND welcomed_at IS NULL", (now_iso(), family_id))


def set_family_confirmed(family_id):
    ensure_family_welcome_columns()
    with db() as conn:
        conn.execute("UPDATE families SET confirmed_at = ? WHERE id = ?"
                     " AND confirmed_at IS NULL", (now_iso(), family_id))


def set_family_phone_bad(family_id, bad: bool):
    ensure_phone_bad_column()
    with db() as conn:
        conn.execute("UPDATE families SET phone_bad = ? WHERE id = ?",
                     (1 if bad else 0, family_id))


def list_messages(provider_id, limit=50):
    with db() as conn:
        q = ("""SELECT m.*, t.name AS family_name FROM message_log m
               LEFT JOIN families t ON t.id = m.family_id
               WHERE m.provider_id = ? ORDER BY m.id DESC""")
        if limit is not None:
            q += " LIMIT ?"
            return conn.execute(q, (provider_id, limit)).fetchall()
        return conn.execute(q, (provider_id,)).fetchall()


def count_messages(provider_id) -> int:
    with db() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM message_log WHERE provider_id = ?",
                           (provider_id,)).fetchone()
        return row["c"] if row else 0


def has_sent_messages(provider_id) -> bool:
    """Has this provider ever sent an outbound text? (checklist helper)."""
    with db() as conn:
        row = conn.execute("SELECT 1 FROM message_log WHERE provider_id = ?"
                           " AND direction = 'out' LIMIT 1", (provider_id,)).fetchone()
        return row is not None


# ------------------------------------------------------------- reminder log --
def reminder_already_sent(family_id, period, stage):
    with db() as conn:
        row = conn.execute(
            "SELECT 1 FROM reminder_log WHERE family_id = ? AND period = ? AND stage = ?",
            (family_id, period, stage)).fetchone()
        return row is not None


def mark_reminder_sent(family_id, period, stage):
    with db() as conn:
        if USE_PG:
            conn.execute(
                "INSERT INTO reminder_log (family_id, period, stage, sent_at)"
                " VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
                (family_id, period, stage, now_iso()))
        else:
            conn.execute(
                "INSERT OR IGNORE INTO reminder_log (family_id, period, stage, sent_at)"
                " VALUES (?,?,?,?)",
                (family_id, period, stage, now_iso()))


# ---------------------------------------------------------------- templates --
def _previous_default_variants(key, new_default):
    """Older default wordings, for one-time template upgrades.

    v1: pay-link wording without the TuitionPing brand prefix / opt-out
    (the default until Oct 2026). v0: the original wording without the
    pay link appended.
    """
    if key.endswith("_es"):
        v1 = new_default.replace("TuitionPing: ", "", 1).replace(
            " Responda STOP para darse de baja.", "")
    else:
        v1 = new_default.replace("TuitionPing: ", "", 1).replace(
            " Reply STOP to opt out.", "")
    return (v1, v1.replace(" {pay_link}", ""))


def get_templates(provider_id):
    ensure_templates_es_columns()
    with db() as conn:
        row = conn.execute("SELECT * FROM templates WHERE provider_id = ?",
                           (provider_id,)).fetchone()
        if row:
            # One-time upgrades: providers still on an older default wording
            # get the current default. Custom wording is never touched.
            updates = {}
            for key in list(DEFAULT_TEMPLATES) + list(DEFAULT_TEMPLATES_ES):
                new_default = (DEFAULT_TEMPLATES.get(key)
                               or DEFAULT_TEMPLATES_ES.get(key))
                try:
                    current = row[key]
                except (KeyError, IndexError):
                    continue
                if current in _previous_default_variants(key, new_default):
                    updates[key] = new_default
            for key, val in updates.items():
                conn.execute(f"UPDATE templates SET {key} = ? WHERE provider_id = ?",
                             (val, provider_id))
            if updates:
                row = conn.execute("SELECT * FROM templates WHERE provider_id = ?",
                                   (provider_id,)).fetchone()
        return row


def save_templates(provider_id, templates: dict):
    """Save all 8 templates (tpl_before / tpl_due / tpl_late3 / tpl_late7,
    each plain and _es). Missing keys keep their current value."""
    ensure_templates_es_columns()
    cols = [template_key(s, l) for l in TEMPLATE_LANGS for s in TEMPLATE_STAGES]
    current = get_templates(provider_id)
    values = [templates.get(c, current[c] if current else DEFAULT_TEMPLATES_ES.get(c)
                            or DEFAULT_TEMPLATES.get(c))
              for c in cols]
    with db() as conn:
        conn.execute(
            f"UPDATE templates SET {', '.join(f'{c}=?' for c in cols)}"
            " WHERE provider_id = ?",
            (*values, provider_id))


# ------------------------------------------------------------ subscriptions --
def get_subscription(provider_id):
    with db() as conn:
        return conn.execute("SELECT * FROM subscriptions WHERE provider_id = ?",
                            (provider_id,)).fetchone()


def set_subscription(provider_id, plan, status, trial_ends_at=None):
    with db() as conn:
        conn.execute(
            """INSERT INTO subscriptions (provider_id, plan, status, trial_ends_at, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(provider_id) DO UPDATE SET
                 plan=excluded.plan, status=excluded.status,
                 trial_ends_at=excluded.trial_ends_at, updated_at=excluded.updated_at""",
            (provider_id, plan, status, trial_ends_at, now_iso()))


_schema_ensured = set()

def _has_column(conn, table, column):
    """Check-then-alter helper. Never run DDL blind inside try/except on
    Postgres: a failed ALTER aborts the transaction, and running DDL from a
    nested connection while an outer transaction holds the table deadlocks
    the request (undetectable by PG's deadlock detector)."""
    if USE_PG:
        return bool(conn.execute(
            "SELECT 1 FROM information_schema.columns"
            " WHERE table_name = ? AND column_name = ?",
            (table, column)).fetchone())
    return any(c["name"] == column
               for c in conn.execute(f"PRAGMA table_info({table})").fetchall())

def _ensure_columns(table, columns, ddl):
    key = (table, tuple(columns))
    if key in _schema_ensured:
        return
    with db() as conn:
        for col in columns:
            if not _has_column(conn, table, col):
                conn.execute(ddl(col))
    _schema_ensured.add(key)

def ensure_stripe_columns():
    """Add stripe_customer_id / stripe_subscription_id to subscriptions if missing."""
    _ensure_columns("subscriptions",
                    ("stripe_customer_id", "stripe_subscription_id"),
                    lambda col: f"ALTER TABLE subscriptions ADD COLUMN {col} TEXT")


def set_stripe_ids(provider_id, customer_id=None, subscription_id=None):
    ensure_stripe_columns()
    with db() as conn:
        if not get_subscription(provider_id):
            conn.execute(
                "INSERT INTO subscriptions (provider_id, plan, status, updated_at)"
                " VALUES (?,?,?,?) ON CONFLICT(provider_id) DO NOTHING",
                (provider_id, "starter", "none", now_iso()))
        if customer_id is not None:
            conn.execute("UPDATE subscriptions SET stripe_customer_id = ? WHERE provider_id = ?",
                         (customer_id, provider_id))
        if subscription_id is not None:
            conn.execute("UPDATE subscriptions SET stripe_subscription_id = ? WHERE provider_id = ?",
                         (subscription_id, provider_id))


def get_provider_by_stripe_customer(customer_id):
    ensure_stripe_columns()
    with db() as conn:
        row = conn.execute("SELECT provider_id FROM subscriptions WHERE stripe_customer_id = ?",
                           (customer_id,)).fetchone()
        if not row:
            return None
        return conn.execute("SELECT * FROM providers WHERE id = ?",
                            (row["provider_id"],)).fetchone()


# ---------------------------------------------------------- founding members --
def ensure_founding_column():
    """Add the founding flag to subscriptions if missing (0 = no, 1 = yes)."""
    _ensure_columns("subscriptions", ("founding",),
                    lambda col: "ALTER TABLE subscriptions ADD COLUMN founding INTEGER DEFAULT 0")


def founding_claimed_count():
    ensure_founding_column()
    with db() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM subscriptions WHERE founding = 1").fetchone()
        return row["c"] if row else 0


def claim_founding_spot(provider_id, max_spots):
    """Give this provider a founding spot if any remain. Returns True if the
    provider now holds one (including already-held). Never takes one away.
    Single connection throughout: nesting db() calls with DDL inside
    self-deadlocks on Postgres."""
    ensure_founding_column()
    with db() as conn:
        sub = conn.execute("SELECT founding FROM subscriptions WHERE provider_id = ?",
                           (provider_id,)).fetchone()
        if sub and sub["founding"]:
            return True
        row = conn.execute("SELECT COUNT(*) AS c FROM subscriptions WHERE founding = 1").fetchone()
        if (row["c"] if row else 0) >= max_spots:
            return False
        if not sub:
            conn.execute(
                "INSERT INTO subscriptions (provider_id, plan, status, updated_at)"
                " VALUES (?,?,?,?) ON CONFLICT(provider_id) DO NOTHING",
                (provider_id, "starter", "none", now_iso()))
        conn.execute("UPDATE subscriptions SET founding = 1 WHERE provider_id = ?",
                     (provider_id,))
        return True


def is_founding(provider_id):
    ensure_founding_column()
    sub = get_subscription(provider_id)
    try:
        return bool(sub["founding"]) if sub else False
    except (KeyError, IndexError, TypeError):
        return False


def set_founding(provider_id, founding: bool):
    """Mirror the founding flag from Stripe metadata (no spot counting)."""
    ensure_founding_column()
    with db() as conn:
        conn.execute("UPDATE subscriptions SET founding = ? WHERE provider_id = ?",
                     (1 if founding else 0, provider_id))


# ------------------------------------------------- v2: comms, snooze, digests --
# Eight new capabilities (Sep 2026): second parent number, per-family language
# (EN/ES templates), per-family snooze, owner phone + morning unpaid digest,
# test-text rate limiting, and a paid_log powering the "nudged on time" report.

DEFAULT_TEMPLATES_ES = {
    "tpl_before_es": "TuitionPing: Hola {name}, recordatorio amistoso: la colegiatura de ${amount} de {location} vence el {due_date}. ¡Gracias! {pay_link} Responda STOP para darse de baja.",
    "tpl_due_es": "TuitionPing: Hola {name}, le recordamos que la colegiatura de ${amount} de {location} vence hoy ({due_date}). ¡Gracias! {pay_link} Responda STOP para darse de baja.",
    "tpl_late3_es": "TuitionPing: Hola {name}, aún no hemos recibido la colegiatura de ${amount} de {location} (venció el {due_date}). Por favor envíela cuando pueda — responda PAID cuando la haya enviado. {pay_link} Responda STOP para darse de baja.",
    "tpl_late7_es": "TuitionPing: Hola {name}, la colegiatura de ${amount} de {location} ya tiene 7 días de retraso (venció el {due_date}). Por favor envíela de inmediato para evitar un recargo — responda PAID cuando la haya enviado. {pay_link} Responda STOP para darse de baja.",
}

TEMPLATE_STAGES = ("before", "due", "late3", "late7")
TEMPLATE_LANGS = ("en", "es")


def template_key(stage, lang):
    """Column/key for a stage+language: tpl_before / tpl_before_es ..."""
    return f"tpl_{stage}" + ("_es" if lang == "es" else "")


def ensure_family_comms_columns():
    """phone2 (second parent), language (en/es), snoozed_until (YYYY-MM-DD)."""
    key = ("families", ("phone2", "language", "snoozed_until"))
    if key in _schema_ensured:
        return
    _ensure_columns("families", ("phone2", "language", "snoozed_until"),
                    lambda col: f"ALTER TABLE families ADD COLUMN {col} TEXT")
    # Backfill once, right after the columns are created — not on every call.
    with db() as conn:
        conn.execute("UPDATE families SET language = 'en' WHERE language IS NULL")


def ensure_templates_es_columns():
    """Spanish template columns on templates, backfilled with defaults."""
    cols = ("tpl_before_es", "tpl_due_es", "tpl_late3_es", "tpl_late7_es")
    _ensure_columns("templates", cols,
                    lambda col: f"ALTER TABLE templates ADD COLUMN {col} TEXT")
    with db() as conn:
        for col in cols:
            conn.execute(f"UPDATE templates SET {col} = ? WHERE {col} IS NULL",
                         (DEFAULT_TEMPLATES_ES[col],))


def ensure_owner_phone_column():
    _ensure_columns("providers", ("owner_phone",),
                    lambda col: f"ALTER TABLE providers ADD COLUMN {col} TEXT")


def get_owner_phone(provider_id):
    ensure_owner_phone_column()
    with db() as conn:
        row = conn.execute("SELECT owner_phone FROM providers WHERE id = ?",
                           (provider_id,)).fetchone()
    try:
        return (row["owner_phone"] or "").strip() or None
    except (KeyError, IndexError, TypeError):
        return None


def set_owner_phone(provider_id, phone):
    ensure_owner_phone_column()
    with db() as conn:
        conn.execute("UPDATE providers SET owner_phone = ? WHERE id = ?",
                     (phone, provider_id))


def family_language(family) -> str:
    try:
        return family["language"] if family["language"] == "es" else "en"
    except (KeyError, IndexError, TypeError):
        return "en"


def family_phones(family) -> list:
    """All numbers that should receive this family's reminders (primary + 2nd)."""
    ensure_family_comms_columns()
    phones = []
    try:
        if family["phone"]:
            phones.append(family["phone"])
    except (KeyError, IndexError, TypeError):
        pass
    try:
        if family["phone2"]:
            phones.append(family["phone2"])
    except (KeyError, IndexError, TypeError):
        pass
    # de-dupe while keeping order
    seen, out = set(), []
    for p in phones:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def is_snoozed(family, day_iso: str) -> bool:
    """True when the family's reminders are paused through day_iso (YYYY-MM-DD)."""
    try:
        until = family["snoozed_until"]
    except (KeyError, IndexError, TypeError):
        return False
    return bool(until) and until >= day_iso


def set_family_snooze(family_id, until_iso):
    ensure_family_comms_columns()
    with db() as conn:
        conn.execute("UPDATE families SET snoozed_until = ? WHERE id = ?",
                     (until_iso, family_id))


def clear_family_snooze(family_id):
    set_family_snooze(family_id, None)


def update_family_comms(family_id, phone2, language):
    ensure_family_comms_columns()
    lang = "es" if language == "es" else "en"
    with db() as conn:
        conn.execute("UPDATE families SET phone2 = ?, language = ? WHERE id = ?",
                     (phone2 or None, lang, family_id))


# ------------------------------------------------------------------ paid_log --
def pg_ddl(sqlite_ddl: str) -> str:
    """Translate SQLite-flavored CREATE TABLE to Postgres when needed."""
    if USE_PG:
        return sqlite_ddl.replace("INTEGER PRIMARY KEY AUTOINCREMENT",
                                  "SERIAL PRIMARY KEY")
    return sqlite_ddl


# Tables that were first created on Postgres with `id INTEGER PRIMARY KEY`
# (no sequence/default), so inserts omitting id fail there. New tables are
# created correctly via pg_ddl(); this repairs ones that already exist.
_PG_SEQ_BACKFILL_TABLES = ("extra_charges", "manual_nudge_log",
                           "statement_blast_log")
_pg_seq_backfilled = False


def _pg_backfill_id_sequences():
    """Give each table's id a sequence default, matching what SERIAL would
    have created. Check-then-alter via information_schema (never blind DDL),
    single connection, idempotent — safe to re-run."""
    global _pg_seq_backfilled
    if not USE_PG or _pg_seq_backfilled:
        return
    _pg_seq_backfilled = True
    with db() as conn:
        for table in _PG_SEQ_BACKFILL_TABLES:
            exists = conn.execute(
                "SELECT 1 FROM information_schema.tables"
                " WHERE table_name = %s", (table,)).fetchone()
            if not exists:
                continue  # ensure_* will create it correctly with SERIAL
            has_default = conn.execute(
                "SELECT column_default FROM information_schema.columns"
                " WHERE table_name = %s AND column_name = 'id'",
                (table,)).fetchone()["column_default"]
            if has_default:
                continue  # already SERIAL-backed
            seq = f"{table}_id_seq"
            conn.execute(f"CREATE SEQUENCE IF NOT EXISTS {seq}")
            conn.execute(f"ALTER TABLE {table} ALTER COLUMN id"
                         f" SET DEFAULT nextval('{seq}')")
            conn.execute(f"ALTER SEQUENCE {seq} OWNED BY {table}.id")
            mx = conn.execute(f"SELECT MAX(id) AS mx FROM {table}").fetchone()["mx"]
            if mx is not None:
                # setval(seq, 0) is out of bounds (minvalue 1), so only
                # advance the sequence when the table already has rows.
                conn.execute(f"SELECT setval('{seq}', ?)", (int(mx),))


def ensure_paid_log():
    with db() as conn:
        conn.execute(pg_ddl(
            """CREATE TABLE IF NOT EXISTS paid_log (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   family_id INTEGER NOT NULL,
                   period TEXT NOT NULL,
                   amount REAL NOT NULL,
                   paid_at TEXT NOT NULL,
                   UNIQUE(family_id, period)
               )"""))


def year_nudged_stats(provider_id, year: int) -> dict:
    """'Nudged back on time' report: payments this year in periods where at
    least one reminder went out. A payment with no reminders is just 'paid'."""
    ensure_paid_log()
    ensure_family_comms_columns()
    prefix = f"{year:04d}-"
    with db() as conn:
        rows = conn.execute(
            """SELECT pl.amount AS amount,
                      EXISTS (SELECT 1 FROM reminder_log rl
                              WHERE rl.family_id = pl.family_id
                                AND rl.period = pl.period) AS nudged
               FROM paid_log pl
               JOIN families f ON f.id = pl.family_id
               JOIN classrooms u ON u.id = f.classroom_id
               JOIN locations l ON l.id = u.location_id
               WHERE l.provider_id = ? AND pl.period LIKE ?""",
            (provider_id, prefix + "%")).fetchall()
    nudged_n = sum(1 for r in rows if r["nudged"])
    nudged_amt = sum(r["amount"] for r in rows if r["nudged"])
    return {"payments": nudged_n, "amount": nudged_amt,
            "total_payments": len(rows),
            "total_amount": sum(r["amount"] for r in rows)}


# ----------------------------------------------------------------- digest_log --
def ensure_digest_log():
    with db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS digest_log (
                   provider_id INTEGER NOT NULL,
                   day TEXT NOT NULL,
                   sent_at TEXT NOT NULL,
                   UNIQUE(provider_id, day)
               )""")


def digest_sent_today(provider_id, day_iso: str) -> bool:
    ensure_digest_log()
    with db() as conn:
        return bool(conn.execute(
            "SELECT 1 FROM digest_log WHERE provider_id = ? AND day = ?",
            (provider_id, day_iso)).fetchone())


def mark_digest_sent(provider_id, day_iso: str):
    ensure_digest_log()
    with db() as conn:
        conn.execute(
            "INSERT INTO digest_log (provider_id, day, sent_at) VALUES (?,?,?)"
            " ON CONFLICT(provider_id, day) DO NOTHING",
            (provider_id, day_iso, now_iso()))


def last_digest_sent_at(provider_id):
    """ISO timestamp of the most recent digest sent to this owner (or None)."""
    ensure_digest_log()
    with db() as conn:
        row = conn.execute(
            "SELECT sent_at FROM digest_log WHERE provider_id = ?"
            " ORDER BY sent_at DESC LIMIT 1", (provider_id,)).fetchone()
    return row["sent_at"] if row else None


def count_unreported_suggestions(provider_id) -> int:
    """Suggestions not yet mentioned in any owner digest."""
    ensure_suggestions()
    with db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM suggestions"
            " WHERE provider_id = ? AND reported = 0",
            (provider_id,)).fetchone()
    return row["c"] if row else 0


def mark_suggestions_reported(provider_id):
    ensure_suggestions()
    with db() as conn:
        conn.execute("UPDATE suggestions SET reported = 1 WHERE provider_id = ?",
                     (provider_id,))


# -------------------------------------------------------------- test_text_log --
def ensure_test_text_log():
    with db() as conn:
        conn.execute(pg_ddl(
            """CREATE TABLE IF NOT EXISTS test_text_log (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   provider_id INTEGER NOT NULL,
                   sent_at TEXT NOT NULL
               )"""))


def count_test_texts_today(provider_id, day_iso: str) -> int:
    ensure_test_text_log()
    with db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM test_text_log"
            " WHERE provider_id = ? AND substr(sent_at, 1, 10) = ?",
            (provider_id, day_iso)).fetchone()
    return row["c"] if row else 0


def log_test_text(provider_id):
    ensure_test_text_log()
    with db() as conn:
        conn.execute("INSERT INTO test_text_log (provider_id, sent_at) VALUES (?,?)",
                     (provider_id, now_iso()))


def has_ever_sent_test_text(provider_id) -> bool:
    """Has this provider ever sent themselves a test text (setup checklist)."""
    ensure_test_text_log()
    with db() as conn:
        row = conn.execute("SELECT 1 FROM test_text_log WHERE provider_id = ? LIMIT 1",
                           (provider_id,)).fetchone()
    return row is not None

TEST_TEXT_DAILY_LIMIT = 3


# -------------------------------------------------------- reminder_run_log --
def ensure_reminder_run_log():
    with db() as conn:
        conn.execute(pg_ddl(
            """CREATE TABLE IF NOT EXISTS reminder_run_log (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   ran_at TEXT NOT NULL,
                   sent_count INTEGER NOT NULL DEFAULT 0
               )"""))


def record_reminder_run(sent_count: int):
    """Log a completed reminder-engine run (cron or manual)."""
    ensure_reminder_run_log()
    with db() as conn:
        conn.execute("INSERT INTO reminder_run_log (ran_at, sent_count) VALUES (?, ?)",
                     (now_iso(), sent_count))


def last_reminder_run():
    """Most recent engine run, or None if it never ran."""
    ensure_reminder_run_log()
    with db() as conn:
        row = conn.execute(
            "SELECT ran_at, sent_count FROM reminder_run_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------- suggestions --
def ensure_suggestions():
    with db() as conn:
        conn.execute(pg_ddl(
            """CREATE TABLE IF NOT EXISTS suggestions (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   provider_id INTEGER NOT NULL,
                   name TEXT NOT NULL DEFAULT '',
                   email TEXT NOT NULL DEFAULT '',
                   body TEXT NOT NULL,
                   reported INTEGER NOT NULL DEFAULT 0,
                   created_at TEXT NOT NULL DEFAULT ''
               )"""))
    _ensure_columns("suggestions", ("reported",),
                    lambda col: f"ALTER TABLE suggestions ADD COLUMN {col} INTEGER DEFAULT 0")


def log_suggestion(provider_id, name: str, email: str, body: str):
    ensure_suggestions()
    with db() as conn:
        conn.execute(
            "INSERT INTO suggestions (provider_id, name, email, body, created_at)"
            " VALUES (?,?,?,?,?)",
            (provider_id, (name or "").strip()[:120], (email or "").strip()[:160],
             (body or "").strip()[:2000], now_iso()))


def list_suggestions(limit: int = 200):
    ensure_suggestions()
    with db() as conn:
        rows = conn.execute(
            "SELECT s.*, p.name AS company FROM suggestions s"
            " LEFT JOIN providers p ON p.id = s.provider_id"
            " ORDER BY s.id DESC LIMIT ?", (limit,)).fetchall()
    return rows


def delete_suggestion(suggestion_id: int):
    ensure_suggestions()
    with db() as conn:
        conn.execute("DELETE FROM suggestions WHERE id = ?", (suggestion_id,))
