"""Local analytics classification on the existing visitor IDs and hit log.

This never blocks a request. Browser execution is evidence, not proof of a
person. Unknown traffic needs independent signals before becoming likely bot.
"""
import hashlib
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlencode, urlsplit

import store
import acquisition
from admin_time import PACIFIC

HUMAN_TYPES = ("HUMAN", "LIKELY HUMAN")
BOT_TYPES = ("LIKELY BOT", "KNOWN BOT")
LABELS = {"HUMAN": "Human", "LIKELY HUMAN": "Likely Human", "LIKELY BOT": "Likely Bot",
          "KNOWN BOT": "Known Bot", "UNKNOWN": "Unknown / Unclassified"}
VERSION = 2
GRACE_SECONDS = 20
_history_lock = threading.Lock()
SORT_COLUMNS = {
    "visitor": ("Visitor", "visitor_id"),
    "type": ("Type", "CASE classification WHEN 'HUMAN' THEN 1 WHEN 'LIKELY HUMAN' THEN 2 WHEN 'LIKELY BOT' THEN 3 WHEN 'KNOWN BOT' THEN 4 ELSE 5 END"),
    "hits": ("Hits", "hit_count"), "pages": ("Pages", "page_count"),
    "first_seen": ("First seen", "first_seen"), "last_seen": ("Last seen", "last_seen"),
    "last_page": ("Last page", "LOWER(last_path)"), "source": ("Source", "LOWER(source)"),
}

# Names are saved as the reason; patterns are maintained in this one place.
BOT_PATTERNS = tuple((name, re.compile(pattern, re.I)) for name, pattern in (
    ("Google crawler", r"googlebot|googleother|google-inspectiontool|adsbot-google|mediapartners-google"),
    ("Bing crawler", r"bingbot|bingpreview|adidxbot"),
    ("DuckDuckGo crawler", r"duckduckbot"), ("Yandex crawler", r"yandexbot|yandeximages"),
    ("Baidu crawler", r"baiduspider"), ("Ahrefs crawler", r"ahrefsbot|ahrefssiteaudit"),
    ("Semrush crawler", r"semrushbot"), ("Majestic crawler", r"mj12bot"),
    ("Facebook preview", r"facebookexternalhit|facebot|meta-externalagent|meta-externalfetcher"),
    ("Twitter preview", r"twitterbot"), ("LinkedIn preview", r"linkedinbot"),
    ("Slack preview", r"slackbot"), ("Discord preview", r"discordbot"),
    ("WhatsApp preview", r"^whatsapp(?:/|\s|$)"), ("Apple crawler", r"applebot"),
    ("Monitoring service", r"uptimerobot|uptime-kuma|betteruptime|betterstack|pingdom|site24x7|statuscake|datadog|newrelicpinger|railway[ /_-]*health|healthcheck|kube-probe|checkly"),
    ("Security scanner", r"nuclei|nikto|sqlmap|nessus|zgrab|masscan|acunetix|netsparker|burpsuite|wpscan|gobuster|dirbuster"),
    ("Browser automation", r"headlesschrome|phantomjs|selenium|playwright|puppeteer|lighthouse"),
    ("curl client", r"(?:^|[ /])curl(?:/|\s|$)"), ("wget client", r"(?:^|[ /])wget(?:/|\s|$)"),
    ("HTTP library", r"python-requests|python-httpx|python-urllib|aiohttp|go-http-client|libwww-perl|okhttp|apache-httpclient|java/|node-fetch|undici|axios/|scrapy"),
    ("Other crawler", r"\b(?:crawler|spider|scraper)(?:/|\b)|\b[a-z][a-z0-9_.-]*bot/\d|\b(?-i:[A-Za-z][A-Za-z0-9_.-]*Bot)\b|\bbot(?:\s|$)|gptbot|chatgpt-user|oai-searchbot|claudebot|perplexitybot"),
))
PROBES = tuple((name, kind, target, re.compile(pattern, re.I)) for name, kind, target, pattern in (
    ("environment file", "KNOWN BOT", "/.env", r"(?:^|/)\.env(?:[./]|$)"),
    ("WordPress configuration", "KNOWN BOT", "/wp-config.php", r"(?:^|/)wp-config\.php(?:[./~]|$)"),
    ("PHP code execution", "KNOWN BOT", "/phpunit/eval-stdin.php", r"(?:^|/)(?:vendor/)?phpunit(?:/|$)|(?:^|/)(?:shell|wso|c99|r57|alfa)\.php(?:/|$)"),
    ("Git repository", "KNOWN BOT", "/.git", r"(?:^|/)\.git(?:/|$)"),
    ("Credential configuration", "KNOWN BOT", "/[credential-file]", r"(?:^|/)(?:\.aws/credentials|\.htpasswd|\.vscode/sftp\.json|web\.config)(?:[./]|$)"),
    ("System file traversal", "KNOWN BOT", "/[system-file]", r"(?:^|/)(?:etc/passwd|proc/self/environ)(?:/|$)"),
    ("WordPress endpoint", "LIKELY BOT", "/wp-admin or wp-login.php", r"(?:^|/)(?:wp-admin|wp-login\.php|xmlrpc\.php|wp-json)(?:/|$)|(?:^|/)(?:wp-content|wp-includes)(?:/|$)"),
    ("PHP admin endpoint", "LIKELY BOT", "/phpmyadmin or adminer.php", r"(?:^|/)(?:phpmyadmin|pma|adminer\.php)(?:/|$)"),
    ("Server configuration", "LIKELY BOT", "/actuator, server-status or cgi-bin", r"(?:^|/)(?:actuator|server-status|cgi-bin|\.aws)(?:/|$)"),
    ("Application admin scanner", "LIKELY BOT", "/[application-admin]", r"(?:^|/)(?:manager/html|jenkins|solr|owa|autodiscover|\.DS_Store)(?:/|$)"),
))
COLUMNS = {
    "classification": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
    "classification_reasons": "TEXT NOT NULL DEFAULT '[]'",
    "bot_score": "INTEGER NOT NULL DEFAULT 0",
    "ua": "TEXT NOT NULL DEFAULT ''", "referrer_host": "TEXT NOT NULL DEFAULT ''",
    "ip_hash": "TEXT NOT NULL DEFAULT ''", "last_seen": "TEXT NOT NULL DEFAULT ''",
    "hit_count": "INTEGER NOT NULL DEFAULT 0", "page_count": "INTEGER NOT NULL DEFAULT 0",
    "last_path": "TEXT NOT NULL DEFAULT ''", "browser_verified": "INTEGER NOT NULL DEFAULT 0",
    "verified_at": "TEXT NOT NULL DEFAULT ''", "webdriver": "INTEGER NOT NULL DEFAULT 0",
    "classification_version": "INTEGER NOT NULL DEFAULT 0",
    "identity_kind": "TEXT NOT NULL DEFAULT 'historical_browser'",
    "history": "INTEGER NOT NULL DEFAULT 1",
    "review_pending": "INTEGER NOT NULL DEFAULT 0",
    "scan_kind": "TEXT NOT NULL DEFAULT ''", "scan_reasons": "TEXT NOT NULL DEFAULT '[]'",
    "automation_key": "TEXT NOT NULL DEFAULT ''",
    "engagement_confirmed": "INTEGER NOT NULL DEFAULT 0",
    "engaged_at": "TEXT NOT NULL DEFAULT ''", "engagement_reason": "TEXT NOT NULL DEFAULT ''",
    "attribution_json": "TEXT NOT NULL DEFAULT ''",
}


def now():
    return datetime.now(timezone.utc)


def stamp(dt=None):
    return (dt or now()).isoformat(timespec="microseconds")


def parsed(value):
    try:
        dt = datetime.fromisoformat(value)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except (ValueError, TypeError):
        return None


def known_bot(ua):
    for name, pattern in BOT_PATTERNS:
        if pattern.search(ua or ""):
            return name
    return ""


def browser_agent(ua):
    return bool(re.match(r"Mozilla/\d", ua or "", re.I)) and not known_bot(ua)


def referrer_host(value):
    try:
        host = (urlsplit(value or "").hostname or "").lower()
        return host[:120] if re.fullmatch(r"[a-z0-9.-]+", host) else ""
    except ValueError:
        return ""


def probe_name(path):
    return scanner_evidence(path, 404)[1]


def scanner_evidence(path, status, query=""):
    """Return safe catalog labels, never arbitrary query payloads.

    Reconnaissance needs a technology-specific target and a failed route.
    Secret-file/execution targets are stronger evidence on this non-PHP app.
    Ordinary missing pages and single-page visits are not scanner evidence.
    """
    candidate = (path or "")[:2048].split("?", 1)[0].split("#", 1)[0]
    for _ in range(2):
        candidate = unquote(candidate)
    candidate = candidate.replace("\\", "/")
    if candidate.startswith("/[probe]/"):
        # Version-one logs kept the family, not the precise exploit target.
        name = candidate[len("/[probe]/"):]
        if name in {item[0] for item in PROBES} and status in (404,405):
            return "LIKELY BOT", name, "/[probe]/" + name
        return "", "", ""
    for name, kind, target, pattern in PROBES:
        if pattern.search(candidate) and (status >= 300 or kind == "KNOWN BOT"):
            if name == "WordPress endpoint":
                token = re.search(r"(?:^|/)(wp-admin|wp-login\.php|xmlrpc\.php|wp-json|wp-content|wp-includes)(?:/|$)", candidate, re.I)
                if token:
                    target = "/" + token.group(1).lower()
            return kind, name, target
    payload = unquote((query or "")[:4096])
    if re.search(r"\$\{jndi:(?:ldap|rmi|dns):|<\?php|(?:\.\./){2,}(?:etc/passwd|proc/self/environ)", payload, re.I):
        return "KNOWN BOT", "Exploit query payload", "/[exploit-query]"
    return "", "", ""


def audit_path(path, status):
    # Failed arbitrary URLs can contain private values. Save only a category.
    path = (path or "/").split("?", 1)[0].split("#", 1)[0]
    if status in (404, 405):
        name = probe_name(path)
        return "/[probe]/" + name if name else "/[unmatched-route]"
    return (path or "/")[:200]


def fallback_id(ip, ua, dt=None):
    import growth
    signature = known_bot(ua)
    if signature:
        return growth._mac(f"traffic-agent:{ip or ''}:{signature}")[:32]
    slot = int((dt or now()).timestamp()) // 1800
    # HMAC hides dictionary-guessable IP/UA combinations. No raw IP is saved.
    return growth._mac(f"traffic:{slot}:{ip or ''}:{ua or ''}")[:32]


def identity(request):
    import growth
    visitor = growth.visitor_from_cookie(request.cookies.get(growth.COOKIE, ""))
    if request.headers.get("dnt") == "1" or request.headers.get("sec-gpc") == "1":
        visitor = ""
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    ip = ip or (request.client.host if request.client else "")
    return visitor or fallback_id(ip, request.headers.get("user-agent", ""))


def ensure_schema(conn):
    for column, definition in COLUMNS.items():
        if not store._has_column(conn, "growth_visitors", column):
            conn.execute(f"ALTER TABLE growth_visitors ADD COLUMN {column} {definition}")
    conn.execute(store.pg_ddl("""CREATE TABLE IF NOT EXISTS site_visits (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, ip_hash TEXT NOT NULL,
        path TEXT NOT NULL, referrer TEXT NOT NULL DEFAULT '', ua TEXT NOT NULL DEFAULT '')"""))
    for column, definition in {"visitor_id": "TEXT NOT NULL DEFAULT ''", "status_code": "INTEGER NOT NULL DEFAULT 200",
                               "request_type": "TEXT NOT NULL DEFAULT 'page'", "method": "TEXT NOT NULL DEFAULT 'GET'",
                               "probe_category": "TEXT NOT NULL DEFAULT ''", "probe_target": "TEXT NOT NULL DEFAULT ''"}.items():
        if not store._has_column(conn, "site_visits", column):
            conn.execute(f"ALTER TABLE site_visits ADD COLUMN {column} {definition}")
    conn.execute(store.pg_ddl("""CREATE TABLE IF NOT EXISTS traffic_classifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT, visitor_id TEXT NOT NULL, ts TEXT NOT NULL,
        previous_type TEXT NOT NULL, classification TEXT NOT NULL, bot_score INTEGER NOT NULL,
        reasons TEXT NOT NULL)"""))
    conn.execute("CREATE INDEX IF NOT EXISTS traffic_hit_visitor_time ON site_visits (visitor_id,ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS traffic_hit_visitor_path ON site_visits (visitor_id,path)")
    conn.execute("CREATE INDEX IF NOT EXISTS traffic_hit_time ON site_visits (ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS traffic_type ON growth_visitors (classification,last_seen)")
    conn.execute("CREATE INDEX IF NOT EXISTS traffic_cluster ON growth_visitors (ip_hash,first_seen)")
    conn.execute("CREATE INDEX IF NOT EXISTS traffic_history ON growth_visitors (history,last_seen)")
    conn.execute("CREATE INDEX IF NOT EXISTS traffic_unassigned ON site_visits (visitor_id,ip_hash,ua)")
    conn.execute("CREATE INDEX IF NOT EXISTS traffic_pending ON growth_visitors (review_pending,first_seen)")
    conn.execute("CREATE INDEX IF NOT EXISTS traffic_engagement ON growth_visitors (engagement_confirmed,classification)")
    conn.execute("CREATE INDEX IF NOT EXISTS traffic_version ON growth_visitors (classification_version,hit_count)")


def _save_decision(conn, row, kind, score, reasons, dt):
    reasons = json.dumps(reasons)
    if row["classification"] != kind:
        conn.execute("INSERT INTO traffic_classifications (visitor_id,ts,previous_type,classification,bot_score,reasons) VALUES (?,?,?,?,?,?)",
                     (row["visitor_id"], stamp(dt), row["classification"], kind, score, reasons))
    start = parsed(row["first_seen"]) or dt
    pending = int(not row["history"] and not row["browser_verified"] and kind not in BOT_TYPES
                  and (dt - start).total_seconds() < GRACE_SECONDS)
    actor = "network:" + row["ip_hash"] if kind in BOT_TYPES and row["ip_hash"] else ""
    conn.execute("UPDATE growth_visitors SET classification=?,bot_score=?,classification_reasons=?,classification_version=?,review_pending=?,automation_key=? WHERE visitor_id=?",
                 (kind, score, reasons, VERSION, pending, actor, row["visitor_id"]))


def _classify(conn, row, dt, allow_recovery=False):
    if row["classification_version"] < VERSION and not row["scan_kind"]:
        for hit in conn.execute("SELECT DISTINCT path,status_code FROM site_visits WHERE visitor_id=?", (row["visitor_id"],)).fetchall():
            kind, category, target = scanner_evidence(hit["path"], hit["status_code"])
            if kind:
                row["scan_kind"] = kind
                row["scan_reasons"] = json.dumps(["Scanner target: " + category, "Recorded response: " + str(hit["status_code"]), "Target: " + target])
                if kind == "KNOWN BOT":
                    break
        if row["scan_kind"]:
            conn.execute("UPDATE growth_visitors SET scan_kind=?,scan_reasons=? WHERE visitor_id=?", (row["scan_kind"],row["scan_reasons"],row["visitor_id"]))
    match = known_bot(row["ua"])
    if match or row["webdriver"] or row["classification"] == "KNOWN BOT":
        reasons = ["User-Agent matched " + match] if match else (["Browser reported navigator.webdriver"] if row["webdriver"] else json.loads(row["classification_reasons"]))
        _save_decision(conn, row, "KNOWN BOT", 100, reasons, dt)
        return
    if row["scan_kind"]:
        _save_decision(conn, row, row["scan_kind"], 95 if row["scan_kind"] == "KNOWN BOT" else 75,
                       json.loads(row["scan_reasons"]), dt)
        return
    if row["history"]:
        kind = "LIKELY HUMAN" if browser_agent(row["ua"]) else "UNKNOWN"
        reasons = ["Historical browser User-Agent; browser execution unconfirmed"] if kind != "UNKNOWN" else ["Historical record has insufficient evidence"]
        _save_decision(conn, row, kind, 0, reasons, dt)
        return
    # Review the last observed minute even if Admin opens much later. Waiting
    # to open a report must not erase the original suspicious request pattern.
    observed_at = parsed(row["last_seen"]) or dt
    recent = [dict(r) for r in conn.execute("SELECT ts,path,status_code FROM site_visits WHERE visitor_id=? AND ts>=? AND ts<=? ORDER BY id DESC LIMIT 64",
                (row["visitor_id"], stamp(observed_at - timedelta(seconds=60)), stamp(observed_at))).fetchall()]
    score, signals, groups = 0, [], set()
    def signal(points, reason, group):
        nonlocal score
        score += points; signals.append(reason); groups.add(group)
    if not row["ua"]:
        signal(10, "Missing User-Agent", "headers")
    # Keep a burst visible through the JS grace period: find the busiest
    # ten-second window in the recent minute, rather than only the last ten.
    timed = sorted([(parsed(r["ts"]), r) for r in recent if parsed(r["ts"]) and 0 <= (observed_at - parsed(r["ts"])).total_seconds() <= 60], key=lambda item: item[0])
    rapid, left = [], 0
    for right, (when, _) in enumerate(timed):
        while (when - timed[left][0]).total_seconds() > 10:
            left += 1
        window = [item[1] for item in timed[left:right+1]]
        if len(window) > len(rapid):
            rapid = window
    if len(rapid) >= 20:
        signal(40, f"{len(rapid)} requests within 10 seconds", "velocity")
    elif len(rapid) >= 10 and len({r["path"] for r in rapid}) >= 8:
        signal(40, "At least 8 different URLs requested within 10 seconds", "velocity")
    if len(recent) >= 12 and len({r["path"] for r in recent}) <= 2:
        signal(25, "Repetitive requests to at most 2 pages", "repetition")
    probes = [r for r in recent if r["path"].startswith("/[probe]/")]
    if len(probes) >= 3 or sum(r["status_code"] in (404,405) for r in recent) >= 6:
        signal(45, "Repeated scanner endpoints or nonexistent routes", "probing")
    start = parsed(row["first_seen"]) or dt
    second = start.replace(microsecond=0)
    cluster = conn.execute("SELECT COUNT(*) AS n FROM growth_visitors WHERE ip_hash=? AND ua=? AND landing_path=? AND first_seen>=? AND first_seen<? AND identity_kind='browser' AND hit_count>0",
                          (row["ip_hash"], row["ua"], row["landing_path"], second.strftime("%Y-%m-%dT%H:%M:%S"),
                           (second + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S"))).fetchone()["n"]
    if cluster >= 12:
        signal(40, f"{cluster} browser identifiers began in the same second with the same network, User-Agent and landing page", "cluster")
    if not row["browser_verified"] and (dt - start).total_seconds() >= GRACE_SECONDS and (len(recent) >= 6 or cluster >= 12):
        signal(15, "No browser execution confirmation after the 20-second grace period", "execution")
    if row["browser_verified"]:
        score = max(0, score - 10)
    if score >= 55 and len(groups) >= 2:
        kind = "LIKELY BOT"
    elif row["classification"] == "LIKELY BOT" and not (allow_recovery and row["browser_verified"] and not signals):
        # Time passing is not evidence that a recorded burst came from people.
        _save_decision(conn, row, "LIKELY BOT", row["bot_score"], json.loads(row["classification_reasons"]), dt)
        return
    elif row["browser_verified"] and browser_agent(row["ua"]) and not signals:
        kind = "HUMAN"
    else:
        kind = "LIKELY HUMAN"
    reasons = signals or ["Normal browser User-Agent" if browser_agent(row["ua"]) else "No sufficient evidence of automation"]
    if row["browser_verified"]:
        reasons.append("Browser execution confirmed; not absolute proof of a person")
    elif not signals:
        reasons.append("Browser execution unconfirmed; single-page visits remain eligible")
    _save_decision(conn, row, kind, min(score, 100), reasons, dt)


def observe(ip, path, referrer, ua, visitor_id="", source="direct", medium="none", campaign="",
            status_code=200, request_type="page", identity_kind="anonymous", method="GET", scan_query="", attribution_detail=None):
    import growth
    growth.ensure_tables()
    dt = now(); ts = stamp(dt)
    scan_kind, category, target = scanner_evidence(path, status_code, scan_query)
    if identity_kind == "anonymous" and scan_kind:
        visitor_id = growth._mac("traffic-scanner:" + (ip or ""))[:32]
    elif identity_kind == "anonymous" and known_bot(ua):
        visitor_id = fallback_id(ip, ua, dt)
    else:
        visitor_id = visitor_id or fallback_id(ip, ua, dt)
    ip_hash = growth._mac("traffic-ip:" + (ip or ""))[:32]
    ua = (ua or "")[:512]; ref = referrer_host(referrer)
    path = "/[probe]/" + category if scan_kind and category != "Exploit query payload" else audit_path(path, status_code)
    with store.db() as conn:
        conn.execute("INSERT INTO growth_visitors (visitor_id,first_seen,source,medium,campaign,landing_path,attribution_json) VALUES (?,?,?,?,?,?,?) ON CONFLICT(visitor_id) DO NOTHING",
                     (visitor_id, ts, source, medium, campaign, path, json.dumps(attribution_detail) if attribution_detail else ''))
        # UPDATE obtains a row lock before counting/inserting this hit.
        conn.execute("UPDATE growth_visitors SET last_seen=?,hit_count=hit_count+1,last_path=?,ua=?,referrer_host=?,ip_hash=?,history=0,identity_kind=? WHERE visitor_id=?",
                     (ts, path, ua, ref, ip_hash, identity_kind, visitor_id))
        if scan_kind:
            scan_reasons = json.dumps(["Scanner target: " + category, "Response: " + str(status_code), "Target: " + target])
            conn.execute("UPDATE growth_visitors SET scan_kind=?,scan_reasons=? WHERE visitor_id=? AND scan_kind!='KNOWN BOT'", (scan_kind,scan_reasons,visitor_id))
        seen = conn.execute("SELECT 1 FROM site_visits WHERE visitor_id=? AND path=? LIMIT 1", (visitor_id, path)).fetchone()
        conn.execute("INSERT INTO site_visits (ts,ip_hash,path,referrer,ua,visitor_id,status_code,request_type,method,probe_category,probe_target) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (ts, ip_hash, path, ref, ua, visitor_id, status_code, request_type, method[:12], category, target))
        if not seen:
            conn.execute("UPDATE growth_visitors SET page_count=page_count+1 WHERE visitor_id=?", (visitor_id,))
        row = dict(conn.execute("SELECT * FROM growth_visitors WHERE visitor_id=?", (visitor_id,)).fetchone())
        _classify(conn, row, dt, allow_recovery=True)
    return visitor_id


def verify_browser(visitor_id, path, webdriver=False):
    import growth
    growth.ensure_tables()
    dt = now()
    with store.db() as conn:
        if not conn.execute("SELECT 1 FROM site_visits WHERE visitor_id=? AND path=? AND status_code=200 LIMIT 1", (visitor_id, path)).fetchone():
            return False
        conn.execute("UPDATE growth_visitors SET browser_verified=1,verified_at=CASE WHEN verified_at='' THEN ? ELSE verified_at END,webdriver=CASE WHEN ?=1 THEN 1 ELSE webdriver END WHERE visitor_id=?",
                     (stamp(dt), int(webdriver), visitor_id))
        row = conn.execute("SELECT * FROM growth_visitors WHERE visitor_id=?", (visitor_id,)).fetchone()
        if not row:
            return False
        _classify(conn, dict(row), dt, allow_recovery=True)
    return True


def confirm_engagement(visitor_id, path, signal):
    """Additional signed browser evidence; never a page view or proof of identity."""
    import growth
    growth.ensure_tables()
    if signal not in ('interaction','active_reading'):
        return False
    dt = now()
    with store.db() as conn:
        row = conn.execute('SELECT * FROM growth_visitors WHERE visitor_id=?', (visitor_id,)).fetchone()
        hit = conn.execute("SELECT MIN(ts) AS ts FROM site_visits WHERE visitor_id=? AND path=? AND status_code=200", (visitor_id,path)).fetchone()
        rendered = parsed(hit['ts']) if hit else None
        if (not row or not row['browser_verified'] or not browser_agent(row['ua']) or row['webdriver']
                or row['classification'] not in HUMAN_TYPES or not rendered
                or (dt-rendered).total_seconds() < (30 if signal == 'active_reading' else 8)):
            return False
        reason = 'Browser execution plus trusted interaction after 8 seconds' if signal == 'interaction' else 'Browser execution, interaction and 30 seconds of visible reading'
        conn.execute("UPDATE growth_visitors SET engagement_confirmed=1,engaged_at=CASE WHEN engaged_at='' THEN ? ELSE engaged_at END,engagement_reason=CASE WHEN engagement_reason='' THEN ? ELSE engagement_reason END WHERE visitor_id=?", (stamp(dt),reason,visitor_id))
    return True


def confirm_account_activity(visitor_id, event):
    # Called only after a server-side account milestone has been retained.
    if event not in {'signup','checkout_completed','trial_started','first_reminder','paid_customer'}:
        return
    with store.db() as conn:
        conn.execute("UPDATE growth_visitors SET engagement_confirmed=1,engaged_at=CASE WHEN engaged_at='' THEN ? ELSE engaged_at END,engagement_reason=CASE WHEN engagement_reason='' THEN ? ELSE engagement_reason END WHERE visitor_id=? AND classification IN ('HUMAN','LIKELY HUMAN') AND EXISTS (SELECT 1 FROM growth_events e JOIN providers p ON p.id=e.provider_id WHERE e.visitor_id=growth_visitors.visitor_id AND e.event=? AND e.provider_id IS NOT NULL)",
                     (stamp(),'Server-confirmed account activity: '+event,visitor_id,event))


def refresh_account_engagement(limit=250):
    with store.db() as conn:
        rows = conn.execute("SELECT v.visitor_id,MIN(e.ts) AS ts FROM growth_visitors v JOIN growth_events e ON e.visitor_id=v.visitor_id JOIN providers p ON p.id=e.provider_id WHERE v.engagement_confirmed=0 AND v.classification IN ('HUMAN','LIKELY HUMAN') AND e.event IN ('signup','checkout_completed','trial_started','first_reminder','paid_customer') AND e.provider_id IS NOT NULL GROUP BY v.visitor_id LIMIT ?", (limit,)).fetchall()
        for row in rows:
            conn.execute("UPDATE growth_visitors SET engagement_confirmed=1,engaged_at=?,engagement_reason='Existing server-confirmed account activity' WHERE visitor_id=?", (row['ts'],row['visitor_id']))


def backfill(limit=250):
    with _history_lock:
        return _backfill(limit)


def _backfill(limit):
    """Bounded, idempotent historical classification; never invent JS evidence."""
    import growth
    growth.ensure_tables()
    with store.db() as conn:
        groups = conn.execute("SELECT ip_hash,ua,MIN(ts) AS first_seen,MAX(ts) AS last_seen,COUNT(*) AS hits,COUNT(DISTINCT path) AS pages FROM site_visits WHERE visitor_id='' GROUP BY ip_hash,ua LIMIT ?", (limit,)).fetchall()
        for group in groups:
            group = dict(group)
            vid = hashlib.sha256(("historical:" + group["ip_hash"] + ":" + group["ua"]).encode()).hexdigest()[:32]
            first = conn.execute("SELECT path,referrer FROM site_visits WHERE visitor_id='' AND ip_hash=? AND ua=? ORDER BY id LIMIT 1", (group["ip_hash"], group["ua"])).fetchone()
            last = conn.execute("SELECT path FROM site_visits WHERE visitor_id='' AND ip_hash=? AND ua=? ORDER BY id DESC LIMIT 1", (group["ip_hash"], group["ua"])).fetchone()
            if not first or not last:
                continue  # Another process may already have migrated this group.
            ref = referrer_host(first["referrer"])
            conn.execute("INSERT INTO growth_visitors (visitor_id,first_seen,source,medium,campaign,landing_path,last_seen,hit_count,page_count,last_path,ua,ip_hash,referrer_host,history,identity_kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,'historical_ip') ON CONFLICT(visitor_id) DO NOTHING",
                         (vid, group["first_seen"], ref or "direct", "referral" if ref else "none", "", first["path"], group["last_seen"], group["hits"], group["pages"], last["path"], group["ua"], group["ip_hash"], ref))
            conn.execute("UPDATE site_visits SET visitor_id=? WHERE visitor_id='' AND ip_hash=? AND ua=?", (vid, group["ip_hash"], group["ua"]))
            row = dict(conn.execute("SELECT * FROM growth_visitors WHERE visitor_id=?", (vid,)).fetchone())
            _classify(conn, row, now())
        remaining = conn.execute("SELECT COUNT(*) AS n FROM site_visits WHERE visitor_id=''").fetchone()["n"]
    return remaining


def refresh_candidates(limit=250):
    """Revisit persisted pending sessions after grace, including delayed reads."""
    import growth
    growth.ensure_tables()
    dt = now()
    with store.db() as conn:
        rows = conn.execute("SELECT visitor_id FROM growth_visitors WHERE (review_pending=1 AND first_seen<=?) OR (classification_version<? AND hit_count>0) ORDER BY classification_version,first_seen LIMIT ?",
                            (stamp(dt - timedelta(seconds=GRACE_SECONDS)), VERSION, limit)).fetchall()
        for candidate in rows:
            # Lock before rereading so a concurrent browser beacon is respected.
            conn.execute("UPDATE growth_visitors SET review_pending=review_pending WHERE visitor_id=?", (candidate["visitor_id"],))
            row = conn.execute("SELECT * FROM growth_visitors WHERE visitor_id=?", (candidate["visitor_id"],)).fetchone()
            if row and (row["review_pending"] or row["classification_version"] < VERSION):
                _classify(conn, dict(row), dt)


def matches(kind, classification):
    return kind == "all" or (kind == "human" and classification in HUMAN_TYPES) or (kind == "automated" and classification in BOT_TYPES) or (kind == "unknown" and classification == "UNKNOWN")


def eligible(kind, row):
    if kind == 'engaged': return row['classification'] in HUMAN_TYPES and bool(row['engagement_confirmed'])
    if kind == 'browser': return row['classification'] in HUMAN_TYPES and bool(row['browser_verified']) and not row['engagement_confirmed']
    if kind == 'unconfirmed': return row['classification'] in HUMAN_TYPES and not row['browser_verified'] and not row['engagement_confirmed']
    return matches(kind,row['classification'])


def metric_id(row):
    if row["classification"] in BOT_TYPES:
        return "automated:" + (row.get("automation_key") or row["visitor_id"])
    return "visitor:" + row["visitor_id"]


def counters_sql():
    actor = "CASE WHEN v.automation_key!='' THEN v.automation_key ELSE v.visitor_id END"
    bot = "v.classification IN ('KNOWN BOT','LIKELY BOT')"
    human = "v.classification IN ('HUMAN','LIKELY HUMAN')"
    return (f"COUNT(DISTINCT CASE WHEN {human} THEN v.visitor_id END) AS human,"
            f"COUNT(DISTINCT CASE WHEN {human} AND v.engagement_confirmed=1 THEN v.visitor_id END) AS engaged,"
            f"COUNT(DISTINCT CASE WHEN {human} AND v.browser_verified=1 AND v.engagement_confirmed=0 THEN v.visitor_id END) AS browser_only,"
            f"COUNT(DISTINCT CASE WHEN {human} AND v.browser_verified=0 AND v.engagement_confirmed=0 THEN v.visitor_id END) AS unconfirmed,"
            f"COUNT(DISTINCT CASE WHEN {bot} THEN {actor} END) AS automated,"
            "COUNT(DISTINCT CASE WHEN v.classification='UNKNOWN' THEN v.visitor_id END) AS unknown,"
            f"COUNT(DISTINCT CASE WHEN {bot} THEN 'a:' || ({actor}) ELSE 'v:' || v.visitor_id END) AS all_count")


def postcard_summary(accounts):
    """QR conversion rates use linked human browsers, not raw request totals."""
    import growth
    growth.ensure_tables()
    backfill()
    refresh_candidates()
    with store.db() as conn:
        qr = {r["visitor_id"]: r["classification"] for r in conn.execute("SELECT DISTINCT v.visitor_id,v.classification FROM growth_visitors v JOIN site_visits h ON h.visitor_id=v.visitor_id WHERE h.path='/postcard'").fetchall()}
        links = {r["provider_id"]: dict(r) for r in conn.execute("SELECT a.provider_id,a.visitor_id,v.classification FROM growth_accounts a JOIN growth_visitors v ON a.visitor_id=v.visitor_id").fetchall()}
    human_ids = {vid for vid, kind in qr.items() if kind in HUMAN_TYPES}
    signups = [a for a in accounts if a["signup_source"] == "postcard" and a["id"] in links and links[a["id"]]["visitor_id"] in human_ids]
    customers = [a for a in signups if a["sub_status"] in ("trialing", "active")]
    for account in accounts:
        account["traffic_type"] = LABELS.get(links.get(account["id"], {}).get("classification", "UNKNOWN"), LABELS["UNKNOWN"])
    return {"visits": len(human_ids), "all_visitors": len(qr), "automated": sum(kind in BOT_TYPES for kind in qr.values()),
            "unknown": sum(kind == "UNKNOWN" for kind in qr.values()), "attributed": len(signups), "customers": len(customers),
            "converting_visitors": len({links[a["id"]]["visitor_id"] for a in signups})}


def report(kind="human", offset=0, sort="last_seen", direction="desc", hit_offset=0):
    import growth
    growth.ensure_tables()
    remaining = backfill()
    refresh_candidates()
    refresh_account_engagement()
    kind = kind if kind in ("engaged", "browser", "unconfirmed", "human", "automated", "all", "unknown") else "engaged"
    sort = sort if sort in SORT_COLUMNS else "last_seen"
    direction = direction if direction in ("asc", "desc") else "desc"
    order = SORT_COLUMNS[sort][1] + " " + direction.upper()
    offset = max(0, min(offset, 1000000))
    hit_offset = max(0, min(hit_offset, 1000000))
    day = now().astimezone(PACIFIC).date()
    start = datetime.combine(day, datetime.min.time(), PACIFIC).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), PACIFIC).astimezone(timezone.utc)
    with store.db() as conn:
        totals = dict(conn.execute(f"SELECT {counters_sql()} FROM growth_visitors v WHERE v.hit_count>0").fetchone())
        today = dict(conn.execute(f"SELECT {counters_sql()} FROM growth_visitors v JOIN site_visits h ON h.visitor_id=v.visitor_id WHERE h.ts>=? AND h.ts<?", (start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"))).fetchone())
        where = {"engaged": "classification IN ('HUMAN','LIKELY HUMAN') AND engagement_confirmed=1",
                 "browser": "classification IN ('HUMAN','LIKELY HUMAN') AND browser_verified=1 AND engagement_confirmed=0",
                 "unconfirmed": "classification IN ('HUMAN','LIKELY HUMAN') AND browser_verified=0 AND engagement_confirmed=0",
                 "human": "classification IN ('HUMAN','LIKELY HUMAN')", "automated": "classification IN ('LIKELY BOT','KNOWN BOT')", "unknown": "classification='UNKNOWN'", "all": "1=1"}[kind]
        filtered_total = conn.execute(f"SELECT COUNT(*) AS n FROM growth_visitors WHERE hit_count>0 AND {where}").fetchone()["n"]
        rows = [dict(r) for r in conn.execute(f"SELECT * FROM growth_visitors WHERE hit_count>0 AND {where} ORDER BY {order},visitor_id ASC LIMIT 101 OFFSET ?", (offset,)).fetchall()]
        hit_where = f"v.{where if where != '1=1' else 'hit_count>0'}"
        hits_total = conn.execute(f"SELECT COUNT(*) AS n FROM site_visits h JOIN growth_visitors v ON h.visitor_id=v.visitor_id WHERE {hit_where}").fetchone()["n"]
        hits = [dict(r) for r in conn.execute(f"SELECT h.*,v.classification,v.classification_reasons FROM site_visits h JOIN growth_visitors v ON h.visitor_id=v.visitor_id WHERE {hit_where} ORDER BY h.id DESC LIMIT 101 OFFSET ?", (hit_offset,)).fetchall()]
        spike = conn.execute("SELECT COUNT(*) AS n FROM site_visits h JOIN growth_visitors v ON h.visitor_id=v.visitor_id WHERE h.ts>=? AND v.classification IN ('LIKELY BOT','KNOWN BOT')", (stamp(now() - timedelta(minutes=5)),)).fetchone()["n"] >= 30
        pending = conn.execute("SELECT COUNT(*) AS n FROM growth_visitors WHERE review_pending=1 AND first_seen<=?", (stamp(now() - timedelta(seconds=GRACE_SECONDS)),)).fetchone()["n"]
    for row in rows:
        row["type_label"] = LABELS.get(row["classification"], LABELS["UNKNOWN"])
        row["reasons"] = json.loads(row["classification_reasons"] or "[]") or ["Historical record has insufficient evidence"]
        row['acquisition'] = acquisition.display(row)
    for hit in hits:
        hit["referrer"] = referrer_host(hit["referrer"])
    def url(**changes):
        return "?" + urlencode(dict(kind=kind, sort=sort, direction=direction, **changes))
    headers = [{"key": key, "label": value[0],
                "url": "?" + urlencode({"kind": kind, "sort": key, "direction": ("asc" if direction == "desc" else "desc") if sort == key else ("asc" if key in ("visitor","type","source","last_page") else "desc")}),
                "aria_sort": ("ascending" if direction == "asc" else "descending") if sort == key else "none"}
               for key, value in SORT_COLUMNS.items()]
    return {"kind": kind, "sort": sort, "direction": direction, "headers": headers, "filtered_total": filtered_total,
            "filter_links": {k: "?" + urlencode({"kind": k, "sort": sort, "direction": direction}) for k in ("engaged", "browser", "unconfirmed", "human", "automated", "all", "unknown")},
            "next_url": url(offset=offset+100), "previous_url": url(offset=max(0,offset-100)),
            "rows": rows[:100], "hits": hits[:100], "offset": offset, "next": offset + 100 if len(rows)>100 else None,
            "hits_total": hits_total, "hit_offset": hit_offset, "hits_next": len(hits)>100,
            "hits_next_url": url(offset=offset,hit_offset=hit_offset+100),
            "hits_previous_url": url(offset=offset,hit_offset=max(0,hit_offset-100)),
            "previous": max(0,offset-100), "remaining": remaining, "pending_reviews": pending, "spike": spike,
            "human_total": totals["human"], "human_today": today["human"],
            "engaged_total": totals["engaged"], "engaged_today": today["engaged"],
            "browser_only_total": totals["browser_only"], "browser_only_today": today["browser_only"],
            "unconfirmed_total": totals["unconfirmed"], "unconfirmed_today": today["unconfirmed"],
            "automated_total": totals["automated"], "automated_today": today["automated"],
            "unknown_total": totals["unknown"], "unknown_today": today["unknown"],
            "all_total": totals["all_count"], "all_today": today["all_count"]}
