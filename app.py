"""TuitionPing v1 — automated tuition reminders + late nudges for small providers.

Run:  uvicorn app:app --reload        (then open http://localhost:8000)

Routes:
  GET  /                        landing page
  GET  /signup  POST /signup    create provider account
  GET  /login   POST /login     log in
  POST /logout                  log out
  GET  /dashboard               collection status, add location/classroom/family, message log
  POST /locations/add  /classrooms/add  /families/add  /families/delete
  POST /families/mark-paid       (provider marks a family paid by hand)
  POST /families/opt-out         (toggle STOP on/off)
  GET  /settings  POST /settings  edit the 4 message templates
  GET  /billing   POST /billing/subscribe  plans + demo subscribe
  GET/POST /internal/run-reminders   the daily engine (cron hits this)
  POST /webhooks/twilio/sms     inbound texts: PAID / STOP / START / HELP
  POST /webhooks/twilio/status  delivery receipts: flags bad numbers
  POST /webhooks/stripe         billing webhook stub
  GET  /healthz                 200 when alive
"""
import os
import json
import re
from datetime import date, datetime, timedelta, timezone

from collections import deque as _deque
import time as _time

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import store
from store import init_db
import reminders
from reminders import run_reminders, today, open_period, family_status, period_of, \
    provider_local_now, render_template, due_date_for_family, cash_forecast, \
    pending_reminders, in_quiet_hours
import billing
import growth
import email_engagement
import setup_wizard
import setup_help
import partner_resources
from sms import DEMO_MODE as SMS_DEMO_MODE

app = FastAPI(title="TuitionPing")

# Dates describe substantive page edits, not filesystem timestamps at deployment.
PUBLIC_PAGES = {
    "/": ("landing.html", "2026-10-06"),
    "/guide": ("guide.html", "2026-10-06"),
    "/late-fee-policy": ("late-fee-policy.html", "2026-10-06"),
    "/compare/brightwheel": ("compare-brightwheel.html", "2026-10-06"),
    "/guides": ("guides.html", "2026-10-06"),
    "/guides/tuition-collection": ("collection_hub.html", "2026-10-06"),
    "/tools/daycare-invoice-receipt": ("tool_invoice.html", "2026-10-06"),
    "/partners": ("partners.html", "2026-10-06"),
    "/guides/tuition-reminder-templates": ("guide_templates.html", "2026-10-06"),
    "/guides/handling-late-paying-parents": ("guide_late_parents.html", "2026-10-06"),
    "/tools/late-fee-calculator": ("tool_late_fee_calc.html", "2026-10-06"),
    "/tools/tuition-payment-tracker": ("tool_payment_tracker.html", "2026-10-06"),
    "/about": ("about.html", "2026-10-06"),
    "/demo": ("demo.html", "2026-10-06"),
    "/home-daycare-tuition-reminders": ("audience.html", "2026-10-06"),
    "/small-daycare-tuition-reminders": ("audience.html", "2026-10-06"),
    "/bilingual-daycare-tuition-reminders": ("audience.html", "2026-10-06"),
    "/support": ("support.html", None),
    "/privacy": ("privacy.html", None),
    "/terms": ("terms.html", None),
    "/sms-consent": ("sms_consent.html", None),
    "/sms-privacy": ("sms_privacy.html", None),
    "/security": ("security.html", None),
}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Baseline security headers. CSP allows inline styles/scripts because
    the templates use them heavily; no third-party JS is loaded."""
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["Permissions-Policy"] = \
        "camera=(), microphone=(), geolocation=(), payment=()"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self' data:; "
        "object-src 'none'; "
        "base-uri 'self'; "
        # form-action must allow Stripe: after POST /signup (or /billing/subscribe)
        # the server 303-redirects to checkout.stripe.com, and browsers check
        # the redirect target against form-action too. 'self' alone silently
        # blocks the Stripe handoff (Oct 2026).
        "form-action 'self' https://checkout.stripe.com https://billing.stripe.com")
    proto = request.headers.get("X-Forwarded-Proto", request.url.scheme)
    if proto.split(",")[0].strip() == "https":
        # Railway terminates TLS at its router; the header tells us the
        # client really used https.
        resp.headers["Strict-Transport-Security"] = \
            "max-age=31536000; includeSubDomains"
    return resp


# ------------------------------------------------------------- CSRF guard --
import hmac as _hmac
import hashlib as _hashlib
import secrets as _secrets

# POST paths that must NOT get CSRF-checked: machine callbacks (Twilio/Stripe
# signatures already authenticate them), the cron token endpoint, and the
# pre-session auth forms.
# /analytics/event validates its own signed-cookie-bound token and event allowlist.
_CSRF_EXEMPT_PREFIXES = ("/webhooks/", "/internal/")
# /logout is exempt on purpose: a forged logout can only end the victim's own
# session (nuisance, not data loss), while a strict check strands users on
# stale tabs with "Security check failed" after any re-login rotates the
# session-bound token. The handler still deletes the session server-side.
_CSRF_EXEMPT_EXACT = {"/login", "/forgot-password", "/reset-password", "/logout", "/analytics/event"}
# Email preference/confirmation links are authenticated by opaque capability
# tokens. GET never changes consent; POST also serves RFC 8058 one-click opt-out.
_CSRF_EXEMPT_EXACT.update({"/email-list/confirm", "/email-list/preferences"})


def csrf_token_for_session(session_token: str) -> str:
    """CSRF token bound to the login session. The session token is a 256-bit
    random httponly cookie, so HMAC(session_token) is unforgeable by a
    cross-site attacker — and revealing it in a form never leaks the
    session itself."""
    return _hmac.new(session_token.encode(),
                     b"tuitionping-csrf-v1", _hashlib.sha256).hexdigest()


def csrf_token_for_seed(seed: str) -> str:
    """CSRF token for the pre-login signup form (double-submit cookie)."""
    return _hmac.new(seed.encode(),
                     b"tuitionping-csrf-signup-v1", _hashlib.sha256).hexdigest()


def csrf_input(request: Request) -> str:
    """Hidden form field carrying this request's CSRF token."""
    token = getattr(request.state, "csrf_token", "") or ""
    from markupsafe import Markup, escape
    return Markup(f'<input type="hidden" name="csrf_token" value="{escape(token)}">')


@app.middleware("http")
async def csrf_protect(request: Request, call_next):
    """Reject state-changing requests whose CSRF token doesn't match the
    caller's session (or the signup double-submit cookie)."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        path = request.url.path
        if not (path.startswith(_CSRF_EXEMPT_PREFIXES)
                or path in _CSRF_EXEMPT_EXACT):
            session_token = request.cookies.get(SESSION_COOKIE, "")
            if session_token:
                expected = csrf_token_for_session(session_token)
            elif path in ("/signup", "/email-list/join", "/setup-help"):
                expected = csrf_token_for_seed(
                    request.cookies.get("csrf_seed", ""))
            else:
                expected = ""
            if path == "/setup-help" and not session_token and not request.cookies.get("csrf_seed"):
                return HTMLResponse("Reload the setup-help form before submitting.", status_code=403)
            if expected:
                # Read the raw body first: on _CachedRequest this caches
                # _body so the downstream app replays it. Parse the form
                # from a throwaway probe request — calling request.form()
                # here would consume the stream without caching _body and
                # leave route handlers with an empty body.
                body = await request.body()
                if path == "/setup-help" and len(body) > 16384:
                    return HTMLResponse("The setup request is too large. Keep your note under 1,000 characters.", status_code=413)

                async def _replay():
                    return {"type": "http.request", "body": body,
                            "more_body": False}

                try:
                    probe = Request(request.scope, _replay)
                    form = await probe.form()
                    got = str(form.get("csrf_token", ""))
                except Exception:
                    got = ""
                if not _hmac.compare_digest(got, expected):
                    return HTMLResponse(
                        "<!doctype html><html><head><title>Security check failed"
                        "</title></head><body style=\"font-family:sans-serif;"
                        "max-width:560px;margin:64px auto;padding:0 20px\">"
                        "<h1>Security check failed</h1>"
                        "<p>Your form was missing a valid security token. "
                        "Please go back, reload the page, and try again.</p>"
                        "<p><a href=\"/\">Back to TuitionPing</a></p>"
                        "</body></html>",
                        status_code=403)
    return await call_next(request)


# ------------------------------------------------- subscription gate --
# No dashboard (or any other app page) until Stripe checkout has completed:
# the subscription must be trialing, active, or past_due (in dunning).
# Reachable without one: billing (to finish paying), auth, support, admin,
# webhooks, and public pages. New routes are gated by default.
_SUB_OK_STATUSES = {"trialing", "active", "past_due"}
_NO_SUB_EXACT = {
    "/", "/login", "/signup", "/logout", "/billing", "/support", "/suggest",
    "/forgot-password", "/reset-password", "/verify-email",
    "/healthz", "/sitemap.xml", "/robots.txt", "/favicon.ico",
    "/terms", "/privacy", "/security", "/sms-privacy", "/sms-consent",
    "/guide", "/postcard", "/late-fee-policy", "/compare/brightwheel",
    "/tools/late-fee-calculator",
}
_NO_SUB_EXACT.update(PUBLIC_PAGES)
_NO_SUB_EXACT.add("/analytics/event")
_NO_SUB_EXACT.add("/email-kit")
_NO_SUB_EXACT.add("/partners/download")
_NO_SUB_EXACT.add("/setup-help")
_NO_SUB_PREFIXES = ("/billing/", "/admin", "/webhooks/", "/internal/",
                    "/static/", "/s/", "/email-list/")


@app.middleware("http")
async def email_form_seed(request: Request, call_next):
    paths = {"/email-kit", "/guides", "/setup-help"}
    seed = ""
    if (request.method == "GET" and request.url.path in paths) or (request.method == "POST" and request.url.path in {"/email-list/join", "/setup-help"}):
        session = request.cookies.get(SESSION_COOKIE, "")
        seed = request.cookies.get("csrf_seed", "") or _secrets.token_hex(16)
        request.state.csrf_token = csrf_token_for_session(session) if session else csrf_token_for_seed(seed)
    response = await call_next(request)
    if seed:
        response.set_cookie("csrf_seed", seed, samesite="lax", secure=PUBLIC_BASE_URL.startswith("https://"), max_age=2592000)
        response.headers["Cache-Control"] = "private, no-store"
    return response


@app.middleware("http")
async def subscription_gate(request: Request, call_next):
    path = request.url.path
    if path in _NO_SUB_EXACT or path.startswith(_NO_SUB_PREFIXES):
        return await call_next(request)
    session_token = request.cookies.get(SESSION_COOKIE)
    if not session_token:
        return await call_next(request)  # logged out; require_login redirects
    provider = store.get_provider_by_session(session_token)
    if not provider:
        return await call_next(request)
    if is_admin(provider):
        # Site admins always have full access, regardless of subscription.
        return await call_next(request)
    sub = store.get_subscription(provider["id"])
    try:
        status = (sub["status"] if sub else "") or ""
    except (KeyError, IndexError, TypeError):
        status = ""
    if status not in _SUB_OK_STATUSES:
        return RedirectResponse("/billing?checkout=required", status_code=303)
    return await call_next(request)


@app.middleware("http")
async def static_cache(request: Request, call_next):
    """Cache static assets for a day. Filenames aren't fingerprinted, so no
    immutable — must-revalidate keeps updates safe."""
    resp = await call_next(request)
    if request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "public, max-age=86400, must-revalidate"
    return resp


# Public marketing pages whose visits are logged (first-party analytics —
# hashed IP only, no personal identity; see Privacy Policy).
_TRACKED_PATHS = {"/", "/guide", "/postcard", "/late-fee-policy",
                  "/tools/late-fee-calculator",
                  "/terms", "/privacy", "/security", "/sms-privacy",
                  "/sms-consent", "/support", "/login", "/signup"}
_TRACKED_PATHS.update(PUBLIC_PAGES)
_TRACKED_PATHS.add("/email-kit")
_TRACKED_PATHS.add("/partners/download")
_TRACKED_PATHS.add("/setup-help")
_TRACKED_PREFIXES = ("/compare/",)

# Downloads are recorded only after a successful file response.
_GROWTH_DOWNLOADS = {"/static/downloads/" + name for name in (
    "daycare-late-fee-policy.pdf", "daycare-late-fee-policy.docx",
    "daycare-tuition-reminders.pdf", "daycare-tuition-reminders.docx",
    "daycare-tuition-payment-tracker.xlsx", "daycare-tuition-collection-kit.zip")}


@app.middleware("http")
async def track_conversions(request: Request, call_next):
    path = request.url.path
    public = path in _TRACKED_PATHS or path in _GROWTH_DOWNLOADS
    visitor = ""
    if not growth.excluded(request):
        visitor = growth.visitor_from_cookie(request.cookies.get(growth.COOKIE, ""))
        if request.method == "GET" and public:
            visitor = visitor or _secrets.token_hex(16)
    request.state.growth_visitor = visitor
    request.state.growth_token = growth.token(visitor) if visitor and public else ""
    response = await call_next(request)
    if visitor and public and request.method == "GET" and response.status_code == 200:
        try:
            source, medium, campaign = growth.attribution(request)
            growth.register(visitor, source, medium, campaign, path)
            if path in {"/partners", "/partners/download"}:
                partner_resources.touch(visitor, request.query_params.get("partner", ""))
            if path in _GROWTH_DOWNLOADS:
                growth.record(visitor, "download", path.rsplit("/", 1)[-1], path)
            else:
                growth.record(visitor, "page_view", path=path)
            if not growth.visitor_from_cookie(request.cookies.get(growth.COOKIE, "")):
                response.set_cookie(growth.COOKIE, growth.cookie_value(visitor), max_age=30*24*3600,
                                    httponly=True, secure=PUBLIC_BASE_URL.startswith("https://"), samesite="lax")
            response.headers["Cache-Control"] = "private, no-store"
        except Exception:
            pass  # Product access must not depend on acquisition analytics.
    return response


@app.post("/analytics/event")
async def conversion_event(request: Request):
    visitor = getattr(request.state, "growth_visitor", "")
    if (not visitor or growth.excluded(request)
            or not _hmac.compare_digest(request.headers.get("x-tp-analytics", ""), growth.token(visitor))):
        return Response(status_code=403)
    body = await request.body()
    if len(body) > 1024:
        return Response(status_code=413)
    try:
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError()
        event, detail, path = data.get("event"), data.get("detail", ""), data.get("path", "")
        if (not isinstance(event, str) or not isinstance(detail, str) or not isinstance(path, str)
                or event not in growth.CLIENT_EVENTS or detail not in growth.CLIENT_EVENTS[event]
                or path not in _TRACKED_PATHS
                or (event.startswith("demo_") and path != "/demo")
                or (event.startswith("video_") and path != "/")
                or (event == "document_created" and path != "/tools/daycare-invoice-receipt")):
            raise ValueError()
        growth.record(visitor, event, detail, path)
    except (ValueError, TypeError):
        return Response(status_code=400)
    except Exception:
        return Response(status_code=503)
    return Response(status_code=204)


@app.middleware("http")
async def track_site_visits(request: Request, call_next):
    """Log one row per hit on public pages so the admin Visitors view can
    tell individual visitors apart. Never breaks the request; skips
    logged-in providers (their own browsing isn't prospect traffic)."""
    resp = await call_next(request)
    try:
        if request.method == "GET" and resp.status_code == 200:
            path = request.url.path
            if (path in _TRACKED_PATHS or path.startswith(_TRACKED_PREFIXES)) \
               and "tuitionping_session" not in request.cookies:
                fwd = request.headers.get("x-forwarded-for", "")
                ip = fwd.split(",")[0].strip() if fwd else \
                    (request.client.host if request.client else "")
                store.log_site_visit(ip, path,
                                     request.headers.get("referer", ""),
                                     request.headers.get("user-agent", ""))
    except Exception:
        pass
    return resp


app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
templates.env.globals["csrf_input"] = csrf_input
with open(os.path.join(os.path.dirname(__file__), "content", "provider-resources.json"), encoding="utf-8") as resource_file:
    templates.env.globals["provider_resources"] = json.load(resource_file)
with open(os.path.join(os.path.dirname(__file__), "content", "audiences.json"), encoding="utf-8") as audience_file:
    AUDIENCES = json.load(audience_file)
templates.env.globals["audiences"] = AUDIENCES


@app.middleware("http")
async def public_search_headers(request: Request, call_next):
    # Canonicalize marketing GET/HEAD pages only. Keep account sessions,
    # billing, POSTs and machine callbacks on their original host.
    if (request.method in ("GET", "HEAD")
            and request.url.hostname == "tuitionping.com"
            and request.url.path in PUBLIC_PAGES):
        target = "https://www.tuitionping.com" + request.url.path
        if request.url.query:
            target += "?" + request.url.query
        return RedirectResponse(target, status_code=308)
    response = await call_next(request)
    if request.url.path in {"/login", "/signup", "/forgot-password", "/reset-password", "/verify-email"}:
        response.headers["X-Robots-Tag"] = "noindex"
    if request.url.path.startswith("/static/downloads/"):
        response.headers["X-Robots-Tag"] = "noindex"
    return response


@app.exception_handler(404)
async def branded_404(request: Request, exc):
    """Branded 404 page with a way home (never a bare JSON error)."""
    return templates.TemplateResponse(
        request, "404.html", {"request": request, "provider": None}, status_code=404)


@app.exception_handler(405)
async def branded_405(request: Request, exc):
    return templates.TemplateResponse(
        request, "404.html", {"request": request, "provider": None}, status_code=405)


@app.get("/favicon.ico")
def favicon():
    """Browsers request this by default; serve the app icon instead of 404ing."""
    return FileResponse(os.path.join(os.path.dirname(__file__), "static",
                                     "apple-touch-icon.png"),
                        media_type="image/png")

SESSION_COOKIE = "tuitionping_session"

# Protect the cron endpoint in production: set INTERNAL_CRON_TOKEN and the
# cron job must pass ?token=...  (Locally it is wide open for easy testing.)
INTERNAL_CRON_TOKEN = os.getenv("INTERNAL_CRON_TOKEN", "")

init_db()  # create tables on startup if they don't exist yet
store.ensure_paid_log()  # migrate payment verification before serving statements


# ------------------------------------------------------------------ helpers --
ADMIN_EMAILS = {e.strip().lower() for e in
                os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
EMAIL_FROM = os.environ.get("EMAIL_FROM", "TuitionPing <hello@tuitionping.com>")
EMAIL_ACTIVE = bool(RESEND_API_KEY)


def send_email(to: str, subject: str, html: str, *, headers=None, idempotency_key=None) -> bool:
    """Send an email via Resend. Dormant without RESEND_API_KEY (logs only)."""
    if not EMAIL_ACTIVE:
        print(f"[email] dormant (no RESEND_API_KEY): to={to} subject={subject}",
              flush=True)
        return False
    import json
    import urllib.request
    import urllib.error
    print(f"[email] attempting send to={to} subject={subject}", flush=True)
    try:
        payload = {"from": EMAIL_FROM, "to": [to], "subject": subject, "html": html}
        if headers:
            payload["headers"] = headers
        request_headers = {"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json", "User-Agent": "TuitionPing/1.0"}
        if idempotency_key:
            request_headers["Idempotency-Key"] = idempotency_key
        req = urllib.request.Request(
            "https://api.resend.com/emails",
            data=json.dumps(payload).encode(),
            method="POST",
            headers=request_headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", "replace")[:300]
            print(f"[email] resend responded status={resp.status} body={body}",
                  flush=True)
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")[:500]
        except Exception:
            body = "<unreadable>"
        print(f"[email] send failed: HTTP {e.code} {body}", flush=True)
        return False
    except Exception as e:
        print(f"[email] send failed: {e}", flush=True)
        return False


def needs_verification(provider) -> bool:
    """True when email verification is active and this account hasn't verified."""
    if not EMAIL_ACTIVE:
        return False
    try:
        return not provider["email_verified"]
    except (KeyError, IndexError, TypeError):
        return False


def send_verification_email(provider_id, email, base_url):
    token = store.create_email_token(provider_id)
    link = f"{base_url.rstrip('/')}/verify-email?token={token}"
    send_email(email.strip(), "Verify your TuitionPing email",
               f"<p>Welcome to TuitionPing! Please confirm this is your email:</p>"
               f"<p><a href=\"{link}\">Verify my email</a></p>"
               f"<p>This link expires in 48 hours.</p>")


def send_welcome_email(provider, plan_name):
    """Thank-you + what-to-expect email sent once at signup."""
    from html import escape
    name = escape((provider["name"] or "there").strip().split()[0])
    dashboard = f"{PUBLIC_BASE_URL.rstrip('/')}/dashboard"
    html = f"""<p>Hi {name},</p>
<p>Your TuitionPing account has been created. Here are the next setup steps:</p>
<ol>
<li><b>Verify your email</b> using the separate verification email.</li>
<li><b>Finish checkout</b> if you have not already. In the real service, your 30-day trial starts only after Stripe confirms checkout. A card is required; $0 is charged today, then your selected plan renews after the trial unless you cancel.</li>
<li><b>Add a location, classroom and families</b>, or import a CSV. Obtain permission for tuition texts first. Check amounts, due dates and English or Spanish preferences.</li>
<li><b>Review your reminder wording and schedule.</b> Reminders follow the due date and respect quiet hours and opt-outs.</li>
<li><b>Verify reported payments.</b> A parent replying PAID is a report. Check your payment records, then use Confirm payment received in the dashboard.</li>
</ol>
<p><a href="{dashboard}" style="display:inline-block;background:#00916e;color:#ffffff;text-decoration:none;padding:12px 24px;border-radius:8px;font-weight:600;">Open your dashboard</a></p>
<p><a href="{PUBLIC_BASE_URL}/billing">Review checkout and billing</a> if the dashboard asks you to finish checkout. Questions? Reply to this email.</p>
<p>&mdash; The TuitionPing team</p>"""
    send_email(provider["email"].strip(), "Your TuitionPing account: next setup steps", html)


def is_admin(provider) -> bool:
    try:
        return bool(provider) and (provider["email"] or "").lower() in ADMIN_EMAILS
    except (KeyError, IndexError, TypeError):
        return False


def is_suspended(provider) -> bool:
    try:
        return bool(provider["suspended"])
    except (KeyError, IndexError, TypeError):
        return False


def current_provider(request: Request):
    provider = store.get_provider_by_session(request.cookies.get(SESSION_COOKIE))
    if provider is None:
        return None
    if is_suspended(provider):
        # Belt-and-suspenders: a suspended account's sessions are deleted at
        # suspension time, but reject them here too so no stale session ever
        # grants access.
        return None
    p = dict(provider)
    p["is_admin"] = is_admin(provider)
    return p


def require_login(request: Request):
    provider = current_provider(request)
    if not provider:
        return None, RedirectResponse("/login", status_code=303)
    session_token = request.cookies.get(SESSION_COOKIE, "")
    if session_token:
        request.state.csrf_token = csrf_token_for_session(session_token)
    return provider, None


def require_admin(request: Request):
    provider, redirect = require_login(request)
    if redirect:
        return None, redirect
    if not provider.get("is_admin"):
        return None, HTMLResponse("Not found.", status_code=404)
    return provider, None


def login_response(provider_id: int, request: Request = None):
    token = store.create_session(provider_id)
    resp = RedirectResponse("/dashboard", status_code=303)
    # Railway terminates TLS at its router, so the app always sees http —
    # trust X-Forwarded-Proto to decide the Secure flag. Local dev has no
    # header and keeps working over plain http.
    secure = request is not None and request.headers.get(
        "X-Forwarded-Proto", request.url.scheme).split(",")[0].strip() == "https"
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                    secure=secure, path="/")
    return resp


def twiml(message: str) -> Response:
    """Twilio expects an XML (TwiML) answer to an inbound SMS webhook."""
    xml = (f'<?xml version="1.0" encoding="UTF-8"?><Response>'
           f"<Message>{message}</Message></Response>")
    return Response(content=xml, media_type="application/xml")


# ------------------------------------------------- Twilio authentication ---
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://www.tuitionping.com").rstrip("/")


def twilio_signature_valid(request: Request, form: dict, url_path: str) -> bool:
    """Validate Twilio's X-Twilio-Signature before trusting a callback.

    Uses the canonical public URL (uvicorn runs without proxy headers, so
    request.url would be the internal http address Twilio did not sign).
    Fail-open with a loud log only when no auth token is configured AND the
    app is in demo/local mode; in real production mode without the token,
    unsigned callbacks are rejected rather than accepted.
    """
    if not TWILIO_AUTH_TOKEN:
        if SMS_DEMO_MODE:
            print("[twilio] WARNING: TWILIO_AUTH_TOKEN not set — accepting "
                  "callback without signature validation (dev only)", flush=True)
            return True
        print("[twilio] REJECTED: TWILIO_AUTH_TOKEN not set in real mode — "
              "refusing unsigned callback", flush=True)
        return False
    from twilio.request_validator import RequestValidator
    signature = request.headers.get("X-Twilio-Signature", "")
    url = f"{PUBLIC_BASE_URL}{url_path}"
    params = {k: v for k, v in form.items() if isinstance(v, str)}
    valid = RequestValidator(TWILIO_AUTH_TOKEN).validate(url, params, signature)
    if not valid:
        print(f"[twilio] REJECTED callback with bad signature: {url_path} "
              f"from={params.get('From', '?')}", flush=True)
    return valid


# ------------------------------------------------------------------ landing --
@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    provider = current_provider(request)
    return templates.TemplateResponse(request, "landing.html", {"request": request, "provider": provider,
                                       "plans": billing.PLANS,
                                       "spots_left": billing.founding_spots_left()})


@app.get("/postcard", response_class=HTMLResponse)
def postcard_landing(request: Request):
    """QR landing page for the direct-mail postcard. Logs the visit for
    campaign tracking, drops a 30-day attribution cookie, and shows the
    tightened postcard page (short, mobile-first) with the 10% offer."""
    ip = request.client.host if request.client else ""
    store.log_postcard_visit(ip, request.headers.get("user-agent", ""))
    provider = current_provider(request)
    resp = templates.TemplateResponse(request, "postcard.html",
                                      {"request": request, "provider": provider,
                                       "plans": billing.PLANS,
                                       "spots_left": billing.founding_spots_left()})
    resp.set_cookie("tp_src", "postcard", max_age=30 * 24 * 3600,
                    httponly=True, samesite="lax")
    return resp


# ------------------------------------------------------------- info pages --
@app.get("/support", response_class=HTMLResponse)
def support(request: Request):
    provider = current_provider(request)
    return templates.TemplateResponse(request, "support.html", {"request": request, "provider": provider})


@app.get("/security", response_class=HTMLResponse)
def security_page(request: Request):
    provider = current_provider(request)
    return templates.TemplateResponse(request, "security.html", {"request": request, "provider": provider})


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots_txt():
    return "User-agent: *\nAllow: /\nSitemap: https://www.tuitionping.com/sitemap.xml\n"


@app.get("/sitemap.xml")
def sitemap_xml(request: Request):
    from xml.sax.saxutils import escape
    items = "\n".join(
        '  <url><loc>' + escape("https://www.tuitionping.com" + path) + '</loc>'
        + (f'<lastmod>{updated}</lastmod>' if updated else '') + '</url>'
        for path, (_, updated) in PUBLIC_PAGES.items())
    xml = f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{items}\n</urlset>'
    return Response(content=xml, media_type="application/xml")


@app.get("/demo", response_class=HTMLResponse)
def public_demo(request: Request):
    return templates.TemplateResponse(request, "demo.html", {"request": request, "provider": None})


@app.get("/home-daycare-tuition-reminders", response_class=HTMLResponse)
@app.get("/small-daycare-tuition-reminders", response_class=HTMLResponse)
@app.get("/bilingual-daycare-tuition-reminders", response_class=HTMLResponse)
def audience_page(request: Request):
    return templates.TemplateResponse(request, "audience.html", {"request": request, "provider": None,
                                                               "page": AUDIENCES[request.url.path]})


@app.get("/guide", response_class=HTMLResponse)
def tuition_guide(request: Request):
    """SEO guide: practical, product-honest advice on collecting daycare tuition."""
    provider = current_provider(request)
    return templates.TemplateResponse(request, "guide.html", {"request": request, "provider": provider})


@app.get("/late-fee-policy", response_class=HTMLResponse)
def late_fee_policy(request: Request):
    """SEO lead magnet: copy-paste late fee policy template for daycares."""
    provider = current_provider(request)
    return templates.TemplateResponse(request, "late-fee-policy.html",
                                      {"request": request, "provider": provider})


@app.get("/compare/brightwheel", response_class=HTMLResponse)
def compare_brightwheel(request: Request):
    """SEO comparison page: honest TuitionPing vs brightwheel breakdown."""
    provider = current_provider(request)
    return templates.TemplateResponse(request, "compare-brightwheel.html",
                                      {"request": request, "provider": provider})


@app.get("/guides", response_class=HTMLResponse)
def guides_hub(request: Request):
    """SEO hub: index of all owner guides."""
    provider = current_provider(request)
    return templates.TemplateResponse(request, "guides.html",
                                      {"request": request, "provider": provider})


@app.get("/guides/tuition-collection", response_class=HTMLResponse)
def collection_hub(request: Request):
    return templates.TemplateResponse(request, "collection_hub.html", {"request":request, "provider":None})


@app.get("/tools/daycare-invoice-receipt", response_class=HTMLResponse)
def invoice_tool(request: Request):
    return templates.TemplateResponse(request, "tool_invoice.html", {"request":request, "provider":None})


@app.get("/guides/tuition-reminder-templates", response_class=HTMLResponse)
def guide_templates(request: Request):
    """SEO guide: copy-paste tuition reminder text templates (EN/ES)."""
    provider = current_provider(request)
    return templates.TemplateResponse(request, "guide_templates.html",
                                      {"request": request, "provider": provider})


@app.get("/guides/handling-late-paying-parents", response_class=HTMLResponse)
def guide_late_parents(request: Request):
    """SEO guide: escalation playbook for chronically late-paying parents."""
    provider = current_provider(request)
    return templates.TemplateResponse(request, "guide_late_parents.html",
                                      {"request": request, "provider": provider})


@app.get("/tools/late-fee-calculator", response_class=HTMLResponse)
def tool_late_fee_calc(request: Request):
    """SEO link-magnet: interactive late fee calculator (client-side JS)."""
    provider = current_provider(request)
    return templates.TemplateResponse(request, "tool_late_fee_calc.html",
                                      {"request": request, "provider": provider})


@app.get("/tools/tuition-payment-tracker", response_class=HTMLResponse)
def tool_payment_tracker(request: Request):
    return templates.TemplateResponse(request, "tool_payment_tracker.html",
                                      {"request": request, "provider": current_provider(request)})


@app.get("/about", response_class=HTMLResponse)
def about_page(request: Request):
    return templates.TemplateResponse(request, "about.html",
                                      {"request": request, "provider": current_provider(request)})


@app.get("/suggest", response_class=HTMLResponse)
def suggest_form(request: Request):
    provider, redirect = require_login(request)
    if redirect:
        return redirect
    return templates.TemplateResponse(
        request, "suggest.html",
        {"request": request, "provider": provider, "saved": False,
         "name": "", "email": provider["email"] or ""})


@app.post("/suggest", response_class=HTMLResponse)
def suggest_submit(request: Request, name: str = Form(""), email: str = Form(""),
                   body: str = Form("")):
    provider, redirect = require_login(request)
    if redirect:
        return redirect
    if body.strip():
        store.log_suggestion(provider["id"], name, email, body)
        return templates.TemplateResponse(
            request, "suggest.html",
            {"request": request, "provider": provider, "saved": True,
             "name": "", "email": ""})
    return templates.TemplateResponse(
        request, "suggest.html",
        {"request": request, "provider": provider, "saved": False,
         "name": name, "email": email,
         "error": "Please write your suggestion before sending."})


@app.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    provider = current_provider(request)
    return templates.TemplateResponse(request, "privacy.html", {"request": request, "provider": provider})


@app.get("/terms", response_class=HTMLResponse)
def terms(request: Request):
    provider = current_provider(request)
    return templates.TemplateResponse(request, "terms.html", {"request": request, "provider": provider})


@app.get("/sms-consent", response_class=HTMLResponse)
def sms_consent(request: Request):
    provider = current_provider(request)
    return templates.TemplateResponse(request, "sms_consent.html", {"request": request, "provider": provider})


@app.get("/sms-privacy", response_class=HTMLResponse)
def sms_privacy(request: Request):
    provider = current_provider(request)
    return templates.TemplateResponse(request, "sms_privacy.html", {"request": request, "provider": provider})


# --------------------------------------------------------------------- auth --
@app.get("/signup", response_class=HTMLResponse)
def signup_form(request: Request, ref: str = "", plan: str = "starter"):
    plan = plan if plan in billing.PLANS else "starter"
    p = billing.PLANS[plan]
    # Double-submit CSRF cookie for the pre-login signup form.
    seed = request.cookies.get("csrf_seed", "") or _secrets.token_hex(16)
    request.state.csrf_token = csrf_token_for_seed(seed)
    resp = templates.TemplateResponse(request, "signup.html",
                                      {"request": request, "error": None,
                                       "ref": (ref or "").strip().upper(),
                                       "attribution": request.cookies.get("tp_src", ""),
                                       "plan": plan, "plan_name": p["name"],
                                       "plan_price": p["price"], "cycle": "monthly",
                                       "name": "", "company": "", "email": "",
                                       "heard_about": ""})
    # Seed lives 30 days: the signup form must keep working for tabs left
    # open a long time. A 1-hour expiry caused "Security check failed" on
    # submit for anyone who loaded the page earlier in the day (Oct 3, 2026).
    # Length doesn't weaken the check — the token is an unforgeable HMAC of
    # this random seed, which an attacker can't read cross-site.
    resp.set_cookie("csrf_seed", seed, httponly=False, samesite="lax",
                    path="/", max_age=2592000)
    return resp


@app.post("/signup")
def signup(request: Request, name: str = Form(...), email: str = Form(...),
           password: str = Form(...), company: str = Form(""),
           referral_code: str = Form(""), heard_about: str = Form(""),
           plan: str = Form("starter"), cycle: str = Form("monthly"),
           agree_terms: str = Form(""), attest_consent: str = Form(""),
           marketing_optin: str = Form("")):
    plan = plan if plan in billing.PLANS else "starter"
    cycle = cycle if cycle in billing.CYCLES else "monthly"
    p = billing.PLANS[plan]

    def signup_page(error):
        request.state.csrf_token = csrf_token_for_seed(
            request.cookies.get("csrf_seed", ""))
        return templates.TemplateResponse(request, "signup.html",
                                          {"request": request, "error": error,
                                           "ref": (referral_code or "").strip().upper(),
                                           "attribution": request.cookies.get("tp_src", ""),
                                           "plan": plan, "plan_name": p["name"],
                                           "plan_price": p["price"], "cycle": cycle,
                                           "name": name, "company": company,
                                           "email": email, "heard_about": heard_about},
                                          status_code=400)

    if len(password) < 8:
        return signup_page("Password must be at least 8 characters.")
    if agree_terms != "on":
        return signup_page("Please agree to the Terms of Service and Privacy Policy to continue.")
    if attest_consent != "on":
        return signup_page("Please confirm you have your families' permission to text them about tuition.")
    if store.get_provider_by_email(email):
        return signup_page("That email is already registered. "
                           "Try logging in instead.")
    signup_source = request.cookies.get("tp_src", "")
    provider_id = store.create_provider(name, email, password, company,
                                       heard_about=heard_about,
                                       signup_source=signup_source)
    try:
        growth.bind_account(getattr(request.state, "growth_visitor", ""), provider_id)
    except Exception:
        pass
    # Referral: link the new account to whoever referred them (no self-referrals,
    # invalid codes are ignored silently).
    referrer = store.get_provider_by_referral_code(referral_code)
    if referrer and referrer["id"] != provider_id:
        store.set_referred_by(provider_id, referrer["id"])
    if EMAIL_ACTIVE:
        send_verification_email(provider_id, email, PUBLIC_BASE_URL)
    else:
        store.set_email_verified(provider_id, True)
    provider = store.get_provider(provider_id)
    send_welcome_email(provider, p["name"])
    if marketing_optin == "on":
        try:
            oid = email_engagement.request_email(email, name, "signup", optin=True, kit=False)
            if oid:
                email_engagement.deliver_due(send_email, EMAIL_ACTIVE, limit=1, only_id=oid)
        except Exception:
            print("[email-list] signup confirmation could not be queued", flush=True)
    # Demo mode: everyone starts on a 30-day trial instantly.
    if billing.DEMO_MODE:
        billing.activate_demo_subscription(provider_id, plan)
        return login_response(provider_id, request)
    # Real mode: straight to Stripe Checkout for the chosen plan — the trial
    # ($0 today, card collected by Stripe) starts on confirmed checkout.
    # Without Stripe configured, fall back to the dashboard as before.
    if not billing.stripe_configured():
        return login_response(provider_id, request)
    try:
        # Canonical https://www base: request.base_url sees the internal http
        # scheme (Railway terminates TLS) and whichever host was used, which
        # produced http://tuitionping.com Stripe return URLs (Oct 2026).
        base_url = PUBLIC_BASE_URL
        postcard = request.cookies.get("tp_src", "") == "postcard"
        checkout_url = billing.create_checkout_session(provider, plan, cycle,
                                                       base_url, postcard=postcard)
        growth.milestone(provider_id, "checkout_started")
    except Exception as e:
        print(f"[stripe] signup-checkout error: {e}", flush=True)
        resp = login_response(provider_id, request)
        # Send them to billing to retry checkout instead of stranding them.
        resp.headers["Location"] = "/billing?checkout=error"
        resp.status_code = 303
        return resp
    resp = login_response(provider_id, request)
    resp.headers["Location"] = checkout_url
    resp.status_code = 303
    return resp


@app.get("/verify-email", response_class=HTMLResponse)
def verify_email(request: Request, token: str = ""):
    provider_id = store.verify_email_token(token) if token else None
    if not provider_id:
        return HTMLResponse(
            "That verification link is invalid or expired."
            " <a href='/dashboard'>Continue to your dashboard</a>",
            status_code=400)
    return RedirectResponse("/dashboard?verified=1", status_code=303)


@app.post("/verify-email/resend")
def resend_verification(request: Request):
    provider, redirect = require_login(request)
    if redirect:
        return redirect
    if not EMAIL_ACTIVE or not needs_verification(provider):
        return RedirectResponse("/dashboard", status_code=303)
    send_verification_email(provider["id"], provider["email"],
                            PUBLIC_BASE_URL)
    return RedirectResponse("/dashboard?resent=1", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"request": request, "error": None})


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    ip = _client_ip(request)
    if not _login_allowed(ip):
        return templates.TemplateResponse(
            request, "login.html",
            {"request": request,
             "error": "Too many failed attempts — please wait a few minutes and try again."},
            status_code=429)
    provider = store.get_provider_by_email(email)
    if not provider or not store.verify_password(password, provider["password_hash"]):
        _record_login_failure(ip)
        return templates.TemplateResponse(
            request, "login.html",
            {"request": request, "error": "Wrong email or password."}, status_code=401)
    if is_suspended(provider):
        return templates.TemplateResponse(
            request, "login.html",
            {"request": request,
             "error": "This account has been suspended. Contact support if you think this is a mistake."},
            status_code=403)
    _clear_login_failures(ip)
    return login_response(provider["id"], request)


# ------------------------------------------------------- login throttling --
_LOGIN_ATTEMPTS = {}  # ip -> deque of failure timestamps


def _client_ip(request: Request) -> str:
    # Railway terminates TLS at its router; the real client IP arrives in
    # X-Forwarded-For (uvicorn runs without --proxy-headers, so
    # request.client would only ever see the router).
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return (request.client.host if request.client else "unknown")


def _login_allowed(ip: str) -> bool:
    now = _time.time()
    dq = _LOGIN_ATTEMPTS.setdefault(ip, _deque())
    while dq and now - dq[0] > 600:
        dq.popleft()
    return len(dq) < 10


def _record_login_failure(ip: str):
    _LOGIN_ATTEMPTS.setdefault(ip, _deque()).append(_time.time())


def _clear_login_failures(ip: str):
    _LOGIN_ATTEMPTS.pop(ip, None)


@app.post("/logout")
def logout(request: Request):
    store.delete_session(request.cookies.get(SESSION_COOKIE))
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


# -------------------------------------------------------- password reset --
@app.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_form(request: Request):
    return templates.TemplateResponse(
        request, "forgot_password.html",
        {"request": request, "sent": False, "error": None})


@app.post("/forgot-password")
def forgot_password(request: Request, email: str = Form(...)):
    email = (email or "").strip().lower()
    provider = store.get_provider_by_email(email)
    # Always show the "sent" state so the form can't be used to probe
    # which emails have accounts.
    if provider:
        token = store.create_password_reset_token(provider["id"])
        base = PUBLIC_BASE_URL
        link = f"{base}/reset-password?token={token}"
        send_email(email, "Reset your TuitionPing password",
                   f"<p>Someone requested a password reset for your TuitionPing account.</p>"
                   f"<p><a href=\"{link}\">Choose a new password</a> — this link "
                   f"works once and expires in 1 hour.</p>"
                   f"<p>If you didn't ask for this, you can ignore this email.</p>")
    return templates.TemplateResponse(
        request, "forgot_password.html",
        {"request": request, "sent": True, "error": None})


@app.get("/reset-password", response_class=HTMLResponse)
def reset_password_form(request: Request, token: str = ""):
    return templates.TemplateResponse(
        request, "reset_password.html",
        {"request": request, "token": token, "error": None})


@app.post("/reset-password")
def reset_password(request: Request, token: str = Form(""),
                   password: str = Form(...), password2: str = Form(...)):
    if not token:
        return templates.TemplateResponse(
            request, "reset_password.html",
            {"request": request, "token": "", "error": "This reset link is invalid."},
            status_code=400)
    if len(password) < 8:
        return templates.TemplateResponse(
            request, "reset_password.html",
            {"request": request, "token": token,
             "error": "Password must be at least 8 characters."},
            status_code=400)
    if password != password2:
        return templates.TemplateResponse(
            request, "reset_password.html",
            {"request": request, "token": token,
             "error": "The two passwords don't match."},
            status_code=400)
    provider_id = store.consume_password_reset_token(token)
    if not provider_id:
        return templates.TemplateResponse(
            request, "reset_password.html",
            {"request": request, "token": "",
             "error": "This reset link is invalid or has expired. Please request a new one."},
            status_code=400)
    store.set_password(provider_id, password)
    return RedirectResponse("/login?reset=1", status_code=303)


# ---------------------------------------------------------------- dashboard --
def _trial_days_left(provider_id: int):
    """Days until the trial ends, or None when not trialing / unknown."""
    try:
        sub = billing.subscription_summary(provider_id)
    except Exception:
        return None
    if not sub or sub.get("status") != "trialing" or not sub.get("trial_ends_at"):
        return None
    try:
        end = datetime.fromisoformat(sub["trial_ends_at"])
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        return (end - datetime.now(timezone.utc)).days
    except Exception:
        return None


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, ran: str = ""):
    provider, redirect = require_login(request)
    if redirect:
        return redirect
    day = today()
    month_period = period_of(day)
    store.ensure_next_due_column()

    locations = []
    total_families = 0
    paid_this_month = 0
    total_tuition = 0.0
    collected_tuition = 0.0
    for loc in store.list_locations(provider["id"]):
        classrooms = []
        for classroom in store.list_classrooms(loc["id"]):
            families = []
            for t in store.list_families(classroom["id"]):
                total_families += 1
                total_tuition += store.effective_tuition(t)
                # "On-time rate" counts families paid for the calendar month.
                if t["paid_period"] == month_period:
                    paid_this_month += 1
                    collected_tuition += store.effective_tuition(t)
                next_due_display = ""
                try:
                    nd_raw = t["next_due_date"]
                except (KeyError, IndexError, TypeError):
                    nd_raw = None
                if nd_raw:
                    try:
                        next_due_display = date.fromisoformat(
                            str(nd_raw)[:10]).strftime("%b %d")
                    except ValueError:
                        pass
                try:
                    bday = (t["child_birthday"] or "").strip()
                except (KeyError, IndexError, TypeError):
                    bday = ""
                try:
                    imm_raw = (t["immunization_expires"] or "").strip()
                    imm_delta = (date.fromisoformat(imm_raw[:10]) - day).days
                    imm_display = date.fromisoformat(
                        imm_raw[:10]).strftime("%b %d")
                except (ValueError, KeyError, IndexError, TypeError):
                    imm_delta, imm_display = None, ""
                families.append({**dict(t), "status": family_status(t, day),
                                 "period": open_period(t, day),
                                 "extra": store.outstanding_charges_total(t["id"]),
                                 "next_due_display": next_due_display,
                                 "is_absent_today": store.is_absent_on(
                                     t["id"], day.isoformat()),
                                 "birthday_today": bday == day.strftime("%m-%d"),
                                 "docs_expired": imm_delta is not None and imm_delta <= 0,
                                 "docs_expiring_soon": imm_delta is not None and 0 < imm_delta <= 30,
                                 "docs_expiry_display": imm_display})
            classrooms.append({**dict(loc_classroom(classroom)), "families": families})
        locations.append({**dict(loc), "classrooms": classrooms})

    on_time_rate = round(100 * paid_this_month / total_families) if total_families else 0
    has_sent = store.has_sent_messages(provider["id"])
    sending = store.get_sending_settings(provider["id"])
    sending_display = {**sending,
                       "quiet_start_display": display_time(sending["quiet_start"]),
                       "quiet_end_display": display_time(sending["quiet_end"])}
    setup_state = setup_wizard.snapshot(provider)
    unverified = needs_verification(provider)
    year_stats = store.year_nudged_stats(provider["id"], day.year)
    test_texts_left = (store.TEST_TEXT_DAILY_LIMIT
                       - store.count_test_texts_today(provider["id"],
                                                      datetime.now(timezone.utc).date().isoformat()))
    store.backfill_referral_codes()  # one-time: existing accounts get a code
    ref_stats = store.referral_stats(provider["id"])
    base_url = PUBLIC_BASE_URL
    broadcast_recipients = len(store.get_broadcast_recipients(provider["id"]))
    broadcasts_left = (store.BROADCAST_DAILY_LIMIT
                       - store.count_broadcasts_today(provider["id"], day.isoformat()))
    forecast_total, forecast_count = cash_forecast(provider["id"], day)
    # Today's reminders panel: what's due and not yet sent, and when it goes.
    today_pending = pending_reminders(provider["id"])
    if today_pending and in_quiet_hours(provider["id"]):
        today_next_send = (f"sending at {sending_display['quiet_start_display']} "
                           f"today, when quiet hours end")
    elif today_pending:
        today_next_send = "sending on the next hourly run"
    else:
        today_next_send = ""
    last_run = store.last_reminder_run()
    last_run_display, last_run_stale = None, False
    if last_run:
        try:
            ran_at = datetime.fromisoformat(last_run["ran_at"])
            if ran_at.tzinfo is None:
                ran_at = ran_at.replace(tzinfo=timezone.utc)
            hours = (datetime.now(timezone.utc) - ran_at).total_seconds() / 3600
            last_run_stale = hours > 30
            rel = (f"{int(hours * 60)} min ago" if hours < 1
                   else f"{int(hours)} hr ago" if hours < 48
                   else f"{int(hours / 24)} days ago")
            last_run_display = f"{rel} · {last_run['sent_count']} sent"
        except Exception:
            pass
    return templates.TemplateResponse(request, "dashboard.html", {
        "request": request, "provider": provider, "locations": locations,
        "forecast_total": forecast_total, "forecast_count": forecast_count,
        "today_pending": today_pending, "today_next_send": today_next_send,
        "last_run_display": last_run_display, "last_run_stale": last_run_stale,
        "subscription": billing.subscription_summary(provider["id"]),
        "trial_days_left": _trial_days_left(provider["id"]),
        "plans": billing.PLANS,
        "total_families": total_families, "paid_this_month": paid_this_month,
        "total_tuition": total_tuition, "collected_tuition": collected_tuition,
        "on_time_rate": on_time_rate, "month": day.strftime("%B %Y"),
        "year": day.year, "year_stats": year_stats,
        "test_texts_left": max(test_texts_left, 0),
        "test_sent": request.query_params.get("test") == "sent",
        "owner_phone": store.get_owner_phone(provider["id"]),
        "referral_link": f"{base_url}/signup?ref={ref_stats['code']}",
        "referral_earned": ref_stats["earned_months"],
        "referral_pending": ref_stats["pending"],
        "broadcast_recipients": broadcast_recipients,
        "broadcasts_left": max(broadcasts_left, 0),
        "broadcast_sent": request.query_params.get("broadcast") == "sent",
        "broadcast_n": request.query_params.get("n", "0"),
        "nudge_sent": request.query_params.get("nudge") == "sent",
        "can_blast": plan_at_least_growth(provider["id"]),
        "statement_year": day.year - 1 if day.month <= 3 else day.year,
        "statements_sent": request.query_params.get("statements") == "sent",
        "statements_n": request.query_params.get("n", "0"),
        "statements_year": request.query_params.get("year", ""),
        "demo": SMS_DEMO_MODE, "ran": ran, "sending": sending_display,
        "setup": setup_state, "unverified": unverified,
        "verified_param": request.query_params.get("verified") == "1",
        "resent": request.query_params.get("resent") == "1",
    })


def loc_classroom(classroom):
    return classroom  # tiny helper so the dict() spread reads clearly above


@app.post("/locations/add")
def add_location(request: Request, name: str = Form(...), address: str = Form(""),
                 payment_url: str = Form("")):
    provider, redirect = require_login(request)
    if redirect:
        return redirect
    limit_resp = _location_limit_response(provider)
    if limit_resp:
        return limit_resp
    store.create_location(provider["id"], name, address, payment_url)
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/messages")
def messages_page(request: Request):
    """Standalone message-log page (moved off the dashboard to save space)."""
    provider, redirect = require_login(request)
    if redirect:
        return redirect
    messages = store.list_messages(provider["id"], limit=500)
    total = store.count_messages(provider["id"])
    return templates.TemplateResponse(request, "messages.html",
                                      {"request": request, "provider": provider,
                                       "messages": messages, "total": total})


@app.get("/messages/export")
def export_messages(request: Request):
    """Download the full message log as an Excel workbook."""
    provider, redirect = require_login(request)
    if redirect:
        return redirect
    import io
    import openpyxl
    messages = store.list_messages(provider["id"], limit=None)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Message log"
    ws.append(["Date/Time", "Direction", "Child", "Message", "Status"])
    for m in messages:
        ws.append([
            (m["created_at"] or "")[:16].replace("T", " "),
            "Out" if m["direction"] == "out" else "In",
            m["family_name"] or "—",
            m["body"] or "",
            m["status"] or "",
        ])
    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["B"].width = 11
    ws.column_dimensions["C"].width = 22
    ws.column_dimensions["D"].width = 80
    ws.column_dimensions["E"].width = 20
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return Response(
        content=buf.read(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition":
                 'attachment; filename="tuitionping-message-log.xlsx"'})


@app.get("/locations/edit/{location_id}")
def edit_location_form(request: Request, location_id: int):
    provider, redirect = require_login(request)
    if redirect:
        return redirect
    loc = store.get_location(location_id, provider["id"])
    if not loc:
        return HTMLResponse("Location not found.", status_code=404)
    return templates.TemplateResponse(request, "location_edit.html",
                                      {"request": request, "provider": provider,
                                       "location": dict(loc)})


@app.post("/locations/edit/{location_id}")
def edit_location(request: Request, location_id: int,
                  name: str = Form(...), address: str = Form("")):
    provider, redirect = require_login(request)
    if redirect:
        return redirect
    loc = store.get_location(location_id, provider["id"])
    if not loc:
        return HTMLResponse("Location not found.", status_code=404)
    name = (name or "").strip()
    if not name:
        return HTMLResponse("Location name is required."
                            " <a href='/dashboard'>Go back</a>", status_code=400)
    store.update_location(location_id, provider["id"], name, address)
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/locations/payment-url")
def set_location_payment_url(request: Request, location_id: int = Form(...),
                             payment_url: str = Form("")):
    provider, redirect = require_login(request)
    if redirect:
        return redirect
    loc = store.get_location(location_id, provider["id"])
    if not loc:
        return HTMLResponse("Location not found.", status_code=404)
    store.set_location_payment_url(location_id, provider["id"], payment_url)
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/locations/late-fee")
def set_location_late_fee(request: Request, location_id: int = Form(...),
                          late_fee: str = Form(""), late_fee_days: str = Form("")):
    """Configure the automatic late fee for one location."""
    provider, redirect = require_login(request)
    if redirect:
        return redirect
    loc = store.get_location(location_id, provider["id"])
    if not loc:
        return HTMLResponse("Location not found.", status_code=404)
    store.set_location_late_fee(location_id, provider["id"], late_fee, late_fee_days)
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/broadcast")
def send_broadcast(request: Request, message: str = Form("")):
    """One text to every opted-in family (announcements, closures, etc.)."""
    provider, redirect = require_login(request)
    if redirect:
        return redirect
    pid = provider["id"]
    body = (message or "").strip()
    if not body:
        return HTMLResponse("Message can't be empty. <a href='/dashboard'>Go back</a>",
                            status_code=400)
    if len(body) > 500:
        return HTMLResponse("Keep it under 500 characters. <a href='/dashboard'>Go back</a>",
                            status_code=400)
    if store.count_broadcasts_today(pid, today().isoformat()) >= store.BROADCAST_DAILY_LIMIT:
        return HTMLResponse("Daily broadcast limit reached (5/day). <a href='/dashboard'>Go back</a>",
                            status_code=429)
    recipients = store.get_broadcast_recipients(pid)
    company = (provider.get("company") or "TuitionPing").strip()
    text = f"{company}: {body}"
    if not SMS_DEMO_MODE:
        from sms import send_sms
        for family_id, phone in recipients:
            try:
                send_sms(phone, text, pid, family_id)
            except Exception as exc:
                print(f"[broadcast] failed to {phone}: {exc!r}", flush=True)
    else:
        for family_id, phone in recipients:
            store.log_message(pid, family_id, "out", text, "sent (demo)")
    store.log_broadcast(pid, len(recipients))
    return RedirectResponse(f"/dashboard?broadcast=sent&n={len(recipients)}", status_code=303)


@app.post("/classrooms/add")
def add_classroom(request: Request, location_id: int = Form(...), label: str = Form(...)):
    provider, redirect = require_login(request)
    if redirect:
        return redirect
    loc = store.get_location(location_id, provider["id"])
    if not loc:
        return HTMLResponse("Location not found.", status_code=404)
    store.create_classroom(location_id, label)
    return RedirectResponse("/dashboard", status_code=303)


def _verification_gate(provider):
    """Blocks family-adding actions until email is verified (when active)."""
    if needs_verification(provider):
        return HTMLResponse(
            "Please verify your email before adding families — check your inbox "
            "for the verification link. <a href='/dashboard'>Go back</a>",
            status_code=403)
    return None


def _family_limit_response(provider):
    """Return an upgrade-nudge response when the provider is at their plan's
    family cap, else None."""
    plan = billing.subscription_summary(provider["id"])["plan"] or "starter"
    limit = billing.plan_family_limit(plan)
    if store.count_families_for_provider(provider["id"]) < limit:
        return None
    pname = billing.PLANS.get(plan, billing.PLANS["starter"])["name"]
    nxt = billing.next_plan(plan)
    if nxt:
        nname = billing.PLANS[nxt]["name"]
        nlimit = billing.PLANS[nxt]["families"]
        msg = (f"You've reached your plan's limit of {limit} families on the "
               f"{pname} plan. <a href='/billing'>Upgrade to {nname}</a> "
               f"for up to {nlimit} families.")
    else:
        msg = (f"You've reached the {limit}-family limit on the {pname} plan — "
               f"that's our largest tier.")
    return HTMLResponse(f"{msg} <a href='/dashboard'>Go back</a>", status_code=403)


def _location_limit_response(provider):
    """Return an upgrade-nudge response when the provider is at their plan's
    location cap, else None. Growth and Multi-site are unlimited."""
    plan = billing.subscription_summary(provider["id"])["plan"] or "starter"
    limit = billing.plan_location_limit(plan)
    if limit is None or store.count_locations_for_provider(provider["id"]) < limit:
        return None
    pname = billing.PLANS.get(plan, billing.PLANS["starter"])["name"]
    msg = (f"Your {pname} plan includes {limit} location{'s' if limit != 1 else ''}. "
           f"<a href='/billing'>Upgrade to Growth</a> for unlimited locations.")
    return HTMLResponse(f"{msg} <a href='/dashboard'>Go back</a>", status_code=403)


@app.post("/families/add")
def add_family(request: Request, classroom_id: int = Form(...),
               first_name: str = Form(...), last_name: str = Form(""),
               phone: str = Form(...), tuition_amount: float = Form(...),
               due_day: int = Form(...), consent: str = Form(""),
               next_due: str = Form(""), phone2: str = Form(""),
               language: str = Form("en"), return_to: str = Form("")):
    provider, redirect = require_login(request)
    if redirect:
        return redirect
    gate = _verification_gate(provider)
    if gate:
        return gate
    name = f"{first_name.strip()} {last_name.strip()}".strip()
    next_due_date, error = _validate_family_input(name, phone, tuition_amount,
                                                 due_day, next_due)
    if error:
        return HTMLResponse(f"{error} <a href='/dashboard'>Go back</a>",
                            status_code=400)
    phone_e164 = normalize_us_phone(phone)
    if not phone_e164:
        return HTMLResponse(
            "That phone number doesn't look like a valid 10-digit US number"
            " (e.g. (555) 123-4567). <a href='/dashboard'>Go back</a>",
            status_code=400)
    phone2_e164 = normalize_us_phone(phone2) if phone2.strip() else None
    if phone2.strip() and not phone2_e164:
        return HTMLResponse(
            "The second parent's phone number doesn't look like a valid 10-digit"
            " US number. <a href='/dashboard'>Go back</a>", status_code=400)
    if phone2_e164 == phone_e164:
        phone2_e164 = None  # same number twice is just one number
    if consent != "on":
        return HTMLResponse(
            "You must confirm the family agreed to receive tuition reminders by text."
            " <a href='/dashboard'>Go back</a>", status_code=400)
    if not store.get_classroom_for_provider(classroom_id, provider["id"]):
        return HTMLResponse("Classroom not found.", status_code=404)
    limit_resp = _family_limit_response(provider)
    if limit_resp:
        return limit_resp
    fid = store.create_family(classroom_id, name, phone_e164, tuition_amount, due_day,
                            next_due_date, phone2=phone2_e164, language=language)
    family = store.get_family_for_provider(fid, provider["id"])
    if family:
        send_welcome_text(provider, family)
    return RedirectResponse("/setup?step=families" if return_to == "setup" else "/dashboard", status_code=303)


@app.post("/families/delete")
def delete_family(request: Request, family_id: int = Form(...)):
    provider, redirect = require_login(request)
    if redirect:
        return redirect
    family = store.get_family_for_provider(family_id, provider["id"])
    if not family:
        return HTMLResponse("Family not found.", status_code=404)
    store.delete_family(family_id)
    return RedirectResponse("/dashboard", status_code=303)


def _validate_family_input(name, phone, tuition_amount, due_day, next_due):
    """Shared validation for add/edit family. Returns (next_due_date, error)."""
    if not (1 <= due_day <= 31):
        return None, "Due day must be between 1 and 31."
    if tuition_amount <= 0 or not phone.strip() or not name.strip():
        return None, "Name, phone and a positive tuition amount are required."
    next_due_date = None
    if (next_due or "").strip():
        try:
            nd = date.fromisoformat(next_due.strip())
        except ValueError:
            return None, "Next bill due must be a valid date."
        if nd < today():
            return None, "Next bill due can't be in the past."
        next_due_date = nd.isoformat()
    return next_due_date, None


@app.get("/families/{family_id}/statement", response_class=HTMLResponse)
@app.get("/families/{family_id}/statement/{year}", response_class=HTMLResponse)
def family_statement(request: Request, family_id: int, year: int = None):
    """Printable year-end payment statement for one family — what parents need
    for childcare tax credits. Defaults to the current year."""
    provider, redirect = require_login(request)
    if redirect:
        return redirect
    family = store.get_family_for_provider(family_id, provider["id"])
    if not family:
        return HTMLResponse("Family not found.", status_code=404)
    if year is None:
        year = date.today().year
    payments = store.get_family_payments_for_year(family_id, year)
    total = sum(p["amount"] for p in payments)
    company = provider.get("company") or provider.get("name") or "TuitionPing customer"
    return templates.TemplateResponse(request, "statement.html", {
        "request": request, "provider": provider, "family": family,
        "year": year, "payments": payments, "total": total,
        "company": company, "tax_id": store.get_tax_id(provider["id"]),
        "today": date.today().isoformat(), "public": False})


def plan_at_least_growth(provider_id) -> bool:
    """Growth and Multi-site plans (the statement blast is a Growth+ perk)."""
    plan = billing.subscription_summary(provider_id)["plan"] or "starter"
    return plan in ("growth", "multisite")


@app.post("/families/late-pickup")
async def family_late_pickup(request: Request, family_id: int = Form(...)):
    """One tap: log a late pickup as an extra charge at the location's
    pickup-fee rate. The button only shows when the fee is configured."""
    from datetime import datetime as _dt
    provider, redirect = require_login(request)
    if redirect:
        return redirect
    family = store.get_family_for_provider(family_id, provider["id"])
    if not family:
        return RedirectResponse("/dashboard", status_code=303)
    classroom = store.get_classroom_for_provider(family["classroom_id"], provider["id"])
    location = store.get_location(classroom["location_id"], provider["id"]) if classroom else None
    store.ensure_pickup_fee_column()
    fee = 0.0
    try:
        fee = float(location["pickup_fee"] or 0)
    except (TypeError, ValueError, KeyError, IndexError):
        fee = 0.0
    if fee > 0:
        now = _dt.now()
        label = f"Late pickup {now:%b} {now.day}, {now:%-I:%M %p}"
        store.add_extra_charge(family_id, label, fee)
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/locations/pickup-fee")
async def set_pickup_fee(request: Request, location_id: int = Form(...),
                         pickup_fee: str = Form("0")):
    """Late-pickup fee per location — powers the one-tap late pickup logger."""
    provider, redirect = require_login(request)
    if redirect:
        return redirect
    loc = store.get_location(location_id, provider["id"])
    if not loc:
        return HTMLResponse("Location not found.", status_code=404)
    store.set_location_pickup_fee(location_id, provider["id"], pickup_fee)
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/reports/annual", response_class=HTMLResponse)
@app.get("/reports/annual/{year}", response_class=HTMLResponse)
def annual_report(request: Request, year: int = None):
    """Owner's annual tax summary: everything collected, by month + location."""
    provider, redirect = require_login(request)
    if redirect:
        return redirect
    if year is None:
        year = date.today().year
    payments = store.get_provider_payments_for_year(provider["id"], year)
    months = [0.0] * 12
    by_location = {}
    for p in payments:
        m = p["paid_at"] or ""
        try:
            mi = int(m[5:7]) - 1
        except (ValueError, TypeError):
            continue
        if 0 <= mi < 11 + 1:
            amt = float(p["amount"] or 0)
            months[mi] += amt
            loc = p["location_name"] or "—"
            by_location[loc] = by_location.get(loc, 0.0) + amt
    total = sum(months)
    company = provider.get("company") or provider.get("name") or "TuitionPing customer"
    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    return templates.TemplateResponse(request, "annual_report.html", {
        "request": request, "provider": provider, "year": year,
        "months": list(zip(month_names, months)), "by_location": by_location,
        "total": total, "company": company,
        "unverified": store.get_provider_unverified_payments_for_year(provider["id"], year),
        "tax_id": store.get_tax_id(provider["id"]),
        "today": date.today().isoformat()})


@app.get("/s/{token}", response_class=HTMLResponse)
@app.get("/s/{token}/{year}", response_class=HTMLResponse)
def public_statement(request: Request, token: str, year: int = None):
    """A parent's own tax statement via an unguessable texted link — no login."""
    fam = store.get_family_by_statement_token(token)
    if not fam:
        return HTMLResponse("This link isn't valid.", status_code=404)
    if year is None:
        year = date.today().year
    payments = store.get_family_payments_for_year(fam["id"], year)
    total = sum(p["amount"] for p in payments)
    company = fam["provider_company"] or fam["provider_name"] or "Your daycare"
    return templates.TemplateResponse(request, "statement.html", {
        "request": request, "provider": None, "family": fam,
        "year": year, "payments": payments, "total": total,
        "company": company, "tax_id": store.get_tax_id(fam["provider_id"]),
        "today": date.today().isoformat(), "public": True})


@app.post("/statements/blast")
def blast_statements(request: Request, year: int = Form(...)):
    """Text every opted-in family a link to their year-end tax statement.
    Growth+ only; one blast per day."""
    provider, redirect = require_login(request)
    if redirect:
        return redirect
    pid = provider["id"]
    if not plan_at_least_growth(pid):
        return HTMLResponse(
            "Year-end statement blasts are available on Growth and above."
            " <a href='/billing'>Upgrade</a>", status_code=403)
    if store.statement_blast_sent_today(pid):
        return HTMLResponse("Already sent a blast today — one per day."
                            " <a href='/dashboard'>Go back</a>", status_code=429)
    if year < 2020 or year > date.today().year + 1:
        return HTMLResponse("That year doesn't look right."
                            " <a href='/dashboard'>Go back</a>", status_code=400)
    company = (provider.get("company") or "TuitionPing").strip()
    base = PUBLIC_BASE_URL
    families_sent = 0
    for fam in store.all_families(pid):
        if fam["opted_out"]:
            continue
        if not store.get_family_payments_for_year(fam["id"], year):
            continue  # nothing to state
        token = store.get_or_create_statement_token(fam["id"])
        link = f"{base}/s/{token}/{year}"
        first = fam["name"].split()[0]
        lang = store.family_language(fam)
        text = (f"Hola {first}, su estado de cuenta de cuidado infantil"
                f" de {year} de {company} está listo: {link}"
                if lang == "es" else
                f"Hi {first}, your {year} childcare payment statement from"
                f" {company} is ready: {link}")
        if not SMS_DEMO_MODE:
            from sms import send_sms
            for phone in store.family_phones(fam):
                try:
                    send_sms(phone, text, pid, fam["id"])
                except Exception as exc:
                    print(f"[statement-blast] failed to {phone}: {exc!r}", flush=True)
        else:
            for phone in store.family_phones(fam):
                store.log_message(pid, fam["id"], "out", text, "sent (demo)")
        families_sent += 1
    store.log_statement_blast(pid, year, families_sent)
    return RedirectResponse(
        f"/dashboard?statements=sent&n={families_sent}&year={year}",
        status_code=303)


@app.get("/families/edit/{family_id}", response_class=HTMLResponse)
def edit_family_form(request: Request, family_id: int):
    provider, redirect = require_login(request)
    if redirect:
        return redirect
    family = store.get_family_for_provider(family_id, provider["id"])
    if not family:
        return HTMLResponse("Family not found.", status_code=404)
    store.ensure_family_comms_columns()
    store.ensure_family_notes_column()
    store.ensure_discount_column()
    family = store.get_family_for_provider(family_id, provider["id"])
    fam = dict(family)
    # Split the stored "First Last" name for the two form fields.
    parts = (fam.get("name") or "").strip().split(" ", 1)
    fam["first_name"] = parts[0] if parts else ""
    fam["last_name"] = parts[1] if len(parts) > 1 else ""
    return templates.TemplateResponse(request, "family_edit.html",
                                      {"request": request, "provider": provider,
                                       "family": fam,
                                       "unverified_payments": store.get_unverified_family_payments(family_id),
                                       "charges": store.list_extra_charges(family_id)})


@app.post("/families/edit/{family_id}")
def edit_family(request: Request, family_id: int,
                first_name: str = Form(...), last_name: str = Form(""),
                phone: str = Form(...), tuition_amount: float = Form(...),
                due_day: int = Form(...), next_due: str = Form(""),
                phone2: str = Form(""), language: str = Form("en"),
                snooze_until: str = Form(""), notes: str = Form(""),
                discount_pct: str = Form("0"), child_name: str = Form(""),
                child_birthday: str = Form(""),
                immunization_expires: str = Form("")):
    provider, redirect = require_login(request)
    if redirect:
        return redirect
    family = store.get_family_for_provider(family_id, provider["id"])
    if not family:
        return HTMLResponse("Family not found.", status_code=404)
    name = f"{first_name.strip()} {last_name.strip()}".strip()
    next_due_date, error = _validate_family_input(name, phone, tuition_amount,
                                                  due_day, next_due)
    if error:
        return HTMLResponse(f"{error} <a href='/families/edit/{family_id}'>Go back</a>",
                            status_code=400)
    phone_e164 = normalize_us_phone(phone)
    if not phone_e164:
        return HTMLResponse(
            "That phone number doesn't look like a valid 10-digit US number"
            f" (e.g. (555) 123-4567). <a href='/families/edit/{family_id}'>Go back</a>",
            status_code=400)
    phone2_e164 = normalize_us_phone(phone2) if phone2.strip() else None
    if phone2.strip() and not phone2_e164:
        return HTMLResponse(
            "The second parent's phone number doesn't look like a valid 10-digit"
            f" US number. <a href='/families/edit/{family_id}'>Go back</a>",
            status_code=400)
    if phone2_e164 == phone_e164:
        phone2_e164 = None
    snooze_iso = None
    if snooze_until.strip():
        try:
            snooze_iso = date.fromisoformat(snooze_until.strip()).isoformat()
        except ValueError:
            return HTMLResponse(
                "That pause-until date isn't valid (use YYYY-MM-DD)."
                f" <a href='/families/edit/{family_id}'>Go back</a>",
                status_code=400)
        if snooze_iso < date.today().isoformat():
            return HTMLResponse(
                "The pause-until date is in the past."
                f" <a href='/families/edit/{family_id}'>Go back</a>",
                status_code=400)
    store.update_family(family_id, name, phone_e164, tuition_amount, due_day,
                        next_due_date)
    store.update_family_comms(family_id, phone2_e164, language)
    store.set_family_snooze(family_id, snooze_iso)
    store.set_family_notes(family_id, notes)
    store.set_family_discount(family_id, discount_pct)
    store.set_child_info(family_id, child_name, child_birthday)
    store.set_immunization_expires(family_id, immunization_expires)
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/families/charge")
def add_charge(request: Request, family_id: int = Form(...),
               label: str = Form(...), amount: str = Form(...)):
    """Add a one-time extra charge (late pickup, field trip...)."""
    provider, redirect = require_login(request)
    if redirect:
        return redirect
    family = store.get_family_for_provider(family_id, provider["id"])
    if not family:
        return HTMLResponse("Family not found.", status_code=404)
    try:
        amt = float(amount)
    except (TypeError, ValueError):
        amt = 0
    if amt <= 0 or amt > 100000:
        return HTMLResponse(
            "That charge amount doesn't look right."
            f" <a href='/families/edit/{family_id}'>Go back</a>", status_code=400)
    if not label.strip():
        return HTMLResponse(
            "Give the charge a short label (e.g. 'Late pickup 9/26')."
            f" <a href='/families/edit/{family_id}'>Go back</a>", status_code=400)
    store.add_extra_charge(family_id, label, amt)
    return RedirectResponse(f"/families/edit/{family_id}", status_code=303)


@app.post("/families/charge/remove")
def remove_charge(request: Request, family_id: int = Form(...),
                  charge_id: int = Form(...)):
    provider, redirect = require_login(request)
    if redirect:
        return redirect
    family = store.get_family_for_provider(family_id, provider["id"])
    if not family:
        return HTMLResponse("Family not found.", status_code=404)
    store.remove_extra_charge(charge_id, family_id)
    return RedirectResponse(f"/families/edit/{family_id}", status_code=303)


def test_text_preview(provider_id):
    tpl = store.get_templates(provider_id)
    locs = store.list_locations(provider_id)
    loc_name = locs[0]["name"] if locs else "Sunny Sprouts Daycare"
    pay_url = (locs[0]["payment_url"] or "") if locs else ""
    pay_link = f"Pay online: {pay_url}" if pay_url else ""
    try:
        sample = tpl["tpl_due"].format(name="Alex", amount="1,850.00",
                                       location=loc_name, due_date="Oct 01",
                                       pay_link=pay_link)
        sample = " ".join(sample.split())
    except (KeyError, IndexError, ValueError):
        sample = tpl["tpl_due"]
    return "[Test] " + sample


@app.post("/test-text")
def send_test_text(request: Request, phone: str = Form(...), return_to: str = Form(""), own_number: str = Form("")):
    """Send yourself a sample reminder — 'see exactly what parents get'.
    Rate-limited to 3/day so nobody can burn SMS budget."""
    provider, redirect = require_login(request)
    if redirect:
        return redirect
    if return_to == "setup" and not setup_wizard.snapshot(provider)["preview_done"]:
        return RedirectResponse("/setup?step=preview", status_code=303)
    if return_to == "setup" and own_number != "on":
        return HTMLResponse("Confirm this is your number and you want the test. <a href='/setup?step=test'>Go back</a>", status_code=400)
    to = normalize_us_phone(phone)
    if not to:
        return HTMLResponse("That doesn't look like a valid 10-digit US number."
                            " <a href='/dashboard'>Go back</a>", status_code=400)
    today_iso = datetime.now(timezone.utc).date().isoformat()
    used = store.count_test_texts_today(provider["id"], today_iso)
    if used >= store.TEST_TEXT_DAILY_LIMIT:
        return HTMLResponse(
            "You've used your 3 test texts for today — try again tomorrow."
            " <a href='/dashboard'>Go back</a>", status_code=400)
    sample = test_text_preview(provider["id"])
    from sms import send_sms
    try:
        send_sms(to, sample, provider["id"], None)
    except Exception:
        return HTMLResponse("The test could not be sent. Your setup is saved. <a href='/setup?step=test'>Try again</a> or <a href='/support'>contact support</a>.", status_code=502)
    store.log_test_text(provider["id"])
    if return_to == "setup":
        setup_wizard.mark_test(provider)
    return RedirectResponse("/setup?step=test&test=sent" if return_to == "setup" else "/dashboard?test=sent", status_code=303)


@app.post("/families/unsnooze/{family_id}")
def unsnooze_family(request: Request, family_id: int):
    """Resume reminders for a snoozed family immediately."""
    provider, redirect = require_login(request)
    if redirect:
        return redirect
    family = store.get_family_for_provider(family_id, provider["id"])
    if not family:
        return HTMLResponse("Family not found.", status_code=404)
    store.clear_family_snooze(family_id)
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/families/nudge")
def nudge_family(request: Request, family_id: int = Form(...)):
    """Manually send a family their current reminder right now, outside the
    schedule. Rate-limited to 3/day per family."""
    provider, redirect = require_login(request)
    if redirect:
        return redirect
    family = None
    for f in store.all_families(provider["id"]):
        if f["id"] == family_id:
            family = f
            break
    if not family:
        return HTMLResponse("Family not found.", status_code=404)
    if family["opted_out"]:
        return HTMLResponse("That family opted out of texts."
                            " <a href='/dashboard'>Go back</a>", status_code=400)
    day = provider_local_now(provider["id"]).date()
    if family["paid_period"] == open_period(family, day, skip_paid=False):
        return HTMLResponse("That family is already paid up."
                            " <a href='/dashboard'>Go back</a>", status_code=400)
    if store.count_manual_nudges_today(family_id, day.isoformat()) >= store.MANUAL_NUDGE_DAILY_LIMIT:
        return HTMLResponse(
            "You've already nudged that family 3 times today — try again tomorrow."
            " <a href='/dashboard'>Go back</a>", status_code=400)
    period = open_period(family, day)
    due = due_date_for_family(family, period)
    delta = (due - day).days
    stage = "before" if delta > 0 else "due" if delta == 0 else "late3" if delta > -7 else "late7"
    lang = store.family_language(family)
    templates = store.get_templates(provider["id"])
    fee = store.get_late_fee(family_id, period)
    pay_urls = store.location_payment_urls(provider["id"])
    body = render_template(templates[store.template_key(stage, lang)],
                           family, family["location_name"], due,
                           pay_urls.get(family["location_id"], ""),
                           fee=fee, lang=lang,
                           extra=store.outstanding_charges_total(family_id))
    if SMS_DEMO_MODE:
        for phone in store.family_phones(family):
            store.log_message(provider["id"], family_id, "out",
                              f"[to {phone}] [Nudge] {body}", "sent (demo)")
    else:
        from sms import send_sms
        for phone in store.family_phones(family):
            send_sms(phone, "[Nudge] " + body, provider["id"], family_id)
    store.log_manual_nudge(family_id, day.isoformat())
    return RedirectResponse("/dashboard?nudge=sent", status_code=303)


SAMPLE_CSV = ("name,phone,phone2,language,tuition,due_day,next_due_date\r\n"
              "Jane Smith,+15551234567,,en,1850,1,2026-10-15\r\n"
              "Bob Jones,+15557654321,+15559876543,es,1200,1,\r\n")


@app.get("/families/sample-csv")
def sample_csv(request: Request):
    provider, redirect = require_login(request)
    if redirect:
        return redirect
    return Response(content=SAMPLE_CSV, media_type="text/csv",
                    headers={"Content-Disposition":
                             "attachment; filename=tuitionping-sample.csv"})


def normalize_us_phone(raw: str) -> str | None:
    """Normalize a US phone number to E.164 (+1XXXXXXXXXX).

    Returns None when the number can't be safely used — so we never text
    a wrong number. Accepts 10-digit NANP numbers (adds the +1) and
    11-digit numbers starting with 1. Rejects everything else (short
    numbers, extensions, non-US country codes).
    """
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return None


@app.post("/families/import")
async def import_families(request: Request, classroom_id: int = Form(...),
                          consent: str = Form(""),
                          file: UploadFile = File(...), return_to: str = Form("")):
    import csv
    import io
    provider, redirect = require_login(request)
    if redirect:
        return redirect
    gate = _verification_gate(provider)
    if gate:
        return gate
    if consent != "on":
        return HTMLResponse(
            "You must confirm the families agreed to receive tuition reminders by text."
            " <a href='/dashboard'>Go back</a>", status_code=400)
    if not store.get_classroom_for_provider(classroom_id, provider["id"]):
        return HTMLResponse("Classroom not found.", status_code=404)
    limit_resp = _family_limit_response(provider)
    if limit_resp:
        return limit_resp
    plan = billing.subscription_summary(provider["id"])["plan"] or "starter"
    fam_limit = billing.plan_family_limit(plan)
    fam_current = store.count_families_for_provider(provider["id"])
    raw = await file.read()
    if len(raw) > 1024 * 1024:
        return HTMLResponse("That file is too large (1 MB max)."
                            " <a href='/dashboard'>Go back</a>", status_code=400)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return HTMLResponse("Couldn't read that file — export it as UTF-8 CSV and try again."
                            " <a href='/dashboard'>Go back</a>", status_code=400)
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return HTMLResponse("That CSV is empty."
                            " <a href='/dashboard'>Go back</a>", status_code=400)
    cols = {c.strip().lower(): c for c in reader.fieldnames}
    def col(*names):
        for n in names:
            if n in cols:
                return cols[n]
        return None
    c_name, c_phone = col("name"), col("phone")
    c_tuition = col("tuition", "tuition_amount", "amount")
    c_due = col("due_day", "day")
    c_next = col("next_due_date", "next_due")
    c_phone2 = col("phone2", "phone_2", "second_phone", "parent2_phone")
    c_lang = col("language", "lang")
    if not (c_name and c_phone and c_tuition and c_due):
        return HTMLResponse(
            "Your CSV needs columns: name, phone, tuition, due_day"
            " (next_due_date is optional)."
            " <a href='/families/sample-csv'>Download a sample</a>",
            status_code=400)
    imported, errors = 0, []
    for i, row in enumerate(reader, start=2):
        name = (row.get(c_name) or "").strip()
        phone_raw = row.get(c_phone)
        phone_e164 = normalize_us_phone(phone_raw)
        tuition_raw = (row.get(c_tuition) or "").strip()
        due_raw = (row.get(c_due) or "").strip()
        next_raw = (row.get(c_next) or "").strip() if c_next else ""
        if not any([name, phone_raw, tuition_raw, due_raw]):
            continue  # skip blank rows
        try:
            tuition = float(tuition_raw)
            due_day = int(float(due_raw))
        except ValueError:
            errors.append((i, "tuition and due day must be numbers"))
            continue
        if not phone_e164:
            errors.append(
                (i, f"phone number isn't a valid 10-digit US number: {phone_raw}"))
            continue
        next_due_date, error = _validate_family_input(
            name, phone_e164, tuition, due_day, next_raw)
        if error:
            errors.append((i, error))
            continue
        if fam_current + imported >= fam_limit:
            pname = billing.PLANS.get(plan, billing.PLANS["starter"])["name"]
            errors.append((i, f"Stopped: your {pname} plan allows up to "
                              f"{fam_limit} families — upgrade to import the rest."))
            break
        phone2_e164 = normalize_us_phone(row.get(c_phone2)) if c_phone2 else None
        if c_phone2 and (row.get(c_phone2) or "").strip() and not phone2_e164:
            errors.append((i, f"second phone isn't a valid 10-digit US number: "
                              f"{row.get(c_phone2)}"))
            continue
        if phone2_e164 == phone_e164:
            phone2_e164 = None
        lang = ((row.get(c_lang) or "").strip().lower() if c_lang else "")
        fid = store.create_family(classroom_id, name, phone_e164, tuition, due_day,
                                  next_due_date, phone2=phone2_e164,
                                  language="es" if lang.startswith("es") else "en")
        family = store.get_family_for_provider(fid, provider["id"])
        if family:
            send_welcome_text(provider, family)
        imported += 1
    return templates.TemplateResponse(request, "family_import_result.html",
                                      {"request": request, "provider": provider,
                                       "imported": imported, "errors": errors, "setup_return": return_to == "setup"})


@app.post("/families/confirm-paid")
def confirm_paid(request: Request, family_id: int = Form(...),
                 period: str = Form(...)):
    """Provider verifies the exact period reported by the parent."""
    provider, redirect = require_login(request)
    if redirect:
        return redirect
    family = store.get_family_for_provider(family_id, provider["id"])
    if not family:
        return HTMLResponse("Family not found.", status_code=404)
    if not store.confirm_family_payment(family_id, period):
        # A repeated confirmation is safe; a changed report needs a fresh view.
        family = store.get_family_for_provider(family_id, provider["id"])
        if not family or family["paid_period"] != period or family["paid_source"] != "manual":
            return HTMLResponse(
                'This payment record has changed. <a href="/dashboard">Return to the dashboard</a> and review it again.',
                status_code=409)
    return RedirectResponse("/dashboard?payment_confirmed=1", status_code=303)


@app.post("/families/confirm-logged-payment")
def confirm_logged_payment(request: Request, family_id: int = Form(...),
                           payment_id: int = Form(...)):
    """Provider reviews an original payment report or a legacy ledger record."""
    provider, redirect = require_login(request)
    if redirect:
        return redirect
    if not store.get_family_for_provider(family_id, provider["id"]):
        return HTMLResponse("Family not found.", status_code=404)
    if not store.confirm_logged_family_payment(family_id, payment_id):
        return HTMLResponse("Payment record not found.", status_code=404)
    return RedirectResponse(f"/families/edit/{family_id}?payment_confirmed=1", status_code=303)


@app.post("/families/mark-paid")
def mark_paid(request: Request, family_id: int = Form(...)):
    """Provider manually marks a family paid (e.g. cash/check received)."""
    provider, redirect = require_login(request)
    if redirect:
        return redirect
    family = store.get_family_for_provider(family_id, provider["id"])
    if family:
        # skip_paid=False: pay the actually-open period; if it's already paid
        # this is a no-op (double-click) rather than rolling into next month.
        period = open_period(family, today(), skip_paid=False)
        if family["paid_period"] != period:
            store.mark_family_paid(family_id, period, source="manual")
            maybe_send_review_nudge(provider["id"], family)
    else:
        return HTMLResponse("Family not found.", status_code=404)
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/families/opt-out")
def toggle_opt_out(request: Request, family_id: int = Form(...)):
    provider, redirect = require_login(request)
    if redirect:
        return redirect
    family = store.get_family_for_provider(family_id, provider["id"])
    if family:
        store.set_family_opt_out(family_id, not family["opted_out"])
    else:
        return HTMLResponse("Family not found.", status_code=404)
    return RedirectResponse("/dashboard", status_code=303)


# ----------------------------------------------------------------- settings --
# ----------------------------------------------------------------- settings --
TIMEZONES = ["America/Los_Angeles", "America/Denver", "America/Phoenix",
             "America/Chicago", "America/New_York", "America/Anchorage",
             "Pacific/Honolulu", "UTC"]


def hour_options():
    """24 hourly choices as (value, label), e.g. ('07:00', '7:00 AM')."""
    opts = []
    for h in range(24):
        label = datetime.now().replace(hour=h, minute=0).strftime("%-I:00 %p")
        opts.append((f"{h:02d}:00", label))
    return opts


def display_time(hhmm: str) -> str:
    try:
        h, m = map(int, hhmm.split(":"))
        return datetime.now().replace(hour=h, minute=m).strftime("%-I:%M %p")
    except Exception:
        return hhmm


def template_previews(provider_id, tpl, lang="en"):
    """Fill each template with sample data so the provider sees exactly
    what parents receive. Mirrors reminders.render_template's formatting.
    Covers all 4 stages in the given language."""
    locs = store.list_locations(provider_id)
    sample_location = locs[0]["name"] if locs else "Sunny Sprouts Daycare"
    sample_pay_url = (locs[0]["payment_url"] or "") if locs else ""
    sample_pay_link = f"Pay online: {sample_pay_url}" if sample_pay_url else ""
    if lang == "es":
        stages = [
            ("3 días antes del vencimiento", "before"),
            ("Día de vencimiento", "due"),
            ("3 días de retraso", "late3"),
            ("7 días de retraso", "late7"),
        ]
        lang_label, sample_name = "Español", "María"
    else:
        stages = [
            ("3 days before due", "before"),
            ("Due date", "due"),
            ("3 days late", "late3"),
            ("7 days late", "late7"),
        ]
        lang_label, sample_name = "English", "Jane"
    previews = []
    for stage_label, stage in stages:
        key = store.template_key(stage, lang)
        template = tpl[key] if tpl else ""
        try:
            text = template.format(name=sample_name, amount="1,850.00",
                                   location=sample_location, due_date="Oct 01",
                                   pay_link=sample_pay_link)
            text = " ".join(text.split())
        except (KeyError, IndexError, ValueError):
            text = template
        previews.append({"stage": f"{stage_label} · {lang_label}",
                         "text": text})
    return previews


@app.get("/settings", response_class=HTMLResponse)
def settings_form(request: Request, tpl_lang: str = "en"):
    provider, redirect = require_login(request)
    if redirect:
        return redirect
    if tpl_lang not in ("en", "es"):
        tpl_lang = "en"
    tpl = store.get_templates(provider["id"])
    return templates.TemplateResponse(request, "settings.html", {
        "request": request, "provider": provider, "tpl": tpl,
        "tpl_lang": tpl_lang,
        "tax_id": store.get_tax_id(provider["id"]), "saved": False,
        "saved_sending": False, "saved_profile": False,
        "review_url": store.get_review_url(provider["id"]),
        "previews": template_previews(provider["id"], tpl, tpl_lang),
        "sending": store.get_sending_settings(provider["id"]),
        "owner_phone": store.get_owner_phone(provider["id"]),
        "timezones": TIMEZONES, "hours": hour_options()})


@app.post("/settings", response_class=HTMLResponse)
def save_settings(request: Request,
                  tpl_lang: str = Form("en"),
                  tpl_before: str = Form(None), tpl_due: str = Form(None),
                  tpl_late3: str = Form(None), tpl_late7: str = Form(None),
                  tpl_before_es: str = Form(None), tpl_due_es: str = Form(None),
                  tpl_late3_es: str = Form(None), tpl_late7_es: str = Form(None)):
    provider, redirect = require_login(request)
    if redirect:
        return redirect
    if tpl_lang not in ("en", "es"):
        tpl_lang = "en"
    new_templates = {
        "tpl_before": tpl_before, "tpl_due": tpl_due,
        "tpl_late3": tpl_late3, "tpl_late7": tpl_late7,
        "tpl_before_es": tpl_before_es, "tpl_due_es": tpl_due_es,
        "tpl_late3_es": tpl_late3_es, "tpl_late7_es": tpl_late7_es,
    }
    # Only the visible language's fields are submitted; missing keys keep
    # their current value (see store.save_templates).
    new_templates = {k: v for k, v in new_templates.items() if v is not None}
    # Quick sanity check: every template must keep the {placeholders} it needs.
    for key, tpl in new_templates.items():
        for ph in ("{name}", "{amount}", "{location}", "{due_date}"):
            if ph not in tpl:
                return HTMLResponse(
                    f"Each template must include {ph} ({key})."
                    " <a href='/settings'>Go back</a>",
                    status_code=400)
    store.save_templates(provider["id"], new_templates)
    tpl = store.get_templates(provider["id"])
    return templates.TemplateResponse(request, "settings.html", {
        "request": request, "provider": provider, "tpl": tpl,
        "tpl_lang": tpl_lang,
        "tax_id": store.get_tax_id(provider["id"]), "saved": True,
        "saved_sending": False, "saved_profile": False,
        "review_url": store.get_review_url(provider["id"]),
        "previews": template_previews(provider["id"], tpl, tpl_lang),
        "sending": store.get_sending_settings(provider["id"]),
        "owner_phone": store.get_owner_phone(provider["id"]),
        "timezones": TIMEZONES, "hours": hour_options()})


@app.post("/settings/company", response_class=HTMLResponse)
def save_company(request: Request, company: str = Form(""),
                 tax_id: str = Form(""), review_url: str = Form("")):
    provider, redirect = require_login(request)
    if redirect:
        return redirect
    store.set_company(provider["id"], company)
    store.set_tax_id(provider["id"], tax_id)
    store.set_review_url(provider["id"], review_url)
    tpl = store.get_templates(provider["id"])
    provider = current_provider(request)  # refresh company for the form
    return templates.TemplateResponse(request, "settings.html", {
        "request": request, "provider": provider, "tpl": tpl,
        "tax_id": store.get_tax_id(provider["id"]), "saved": False,
        "saved_sending": False, "saved_profile": True,
        "review_url": store.get_review_url(provider["id"]),
        "previews": template_previews(provider["id"], tpl),
        "sending": store.get_sending_settings(provider["id"]),
        "owner_phone": store.get_owner_phone(provider["id"]),
        "timezones": TIMEZONES, "hours": hour_options()})


@app.post("/settings/sending", response_class=HTMLResponse)
def save_sending(request: Request,
                 timezone: str = Form(...), quiet_start: str = Form(...),
                 quiet_end: str = Form(...), owner_phone: str = Form("")):
    provider, redirect = require_login(request)
    if redirect:
        return redirect
    valid_hours = {v for v, _ in hour_options()}
    if timezone not in TIMEZONES:
        return HTMLResponse("Unknown timezone. <a href='/settings'>Go back</a>",
                            status_code=400)
    if quiet_start not in valid_hours or quiet_end not in valid_hours:
        return HTMLResponse("Pick valid hours. <a href='/settings'>Go back</a>",
                            status_code=400)
    if quiet_start == quiet_end:
        return HTMLResponse("Start and end can't be the same hour."
                            " <a href='/settings'>Go back</a>", status_code=400)
    owner_e164 = normalize_us_phone(owner_phone) if owner_phone.strip() else None
    if owner_phone.strip() and not owner_e164:
        return HTMLResponse("That owner phone number doesn't look like a valid "
                            "10-digit US number. <a href='/settings'>Go back</a>",
                            status_code=400)
    store.save_sending_settings(provider["id"], timezone, quiet_start, quiet_end)
    store.set_owner_phone(provider["id"], owner_e164)
    tpl = store.get_templates(provider["id"])
    return templates.TemplateResponse(request, "settings.html", {
        "request": request, "provider": provider, "tpl": tpl,
        "tax_id": store.get_tax_id(provider["id"]), "saved": False,
        "saved_sending": True, "saved_profile": False,
        "previews": template_previews(provider["id"], tpl),
        "sending": store.get_sending_settings(provider["id"]),
        "owner_phone": store.get_owner_phone(provider["id"]),
        "timezones": TIMEZONES, "hours": hour_options()})


# ------------------------------------------------------------------ billing --
@app.get("/billing", response_class=HTMLResponse)
def billing_page(request: Request):
    provider, redirect = require_login(request)
    if redirect:
        return redirect
    checkout = request.query_params.get("checkout", "")
    if checkout == "success" and billing.stripe_configured():
        # Webhooks can lag; sync straight from Stripe so the page is accurate.
        try:
            billing.sync_subscription_from_stripe(provider["id"])
        except Exception as e:
            print(f"[stripe] return-sync failed: {e}", flush=True)
    return templates.TemplateResponse(request, "billing.html", {
        "request": request, "provider": provider,
        "plans": billing.PLANS,
        "subscription": billing.subscription_summary(provider["id"]),
        "demo": billing.DEMO_MODE,
        "checkout": checkout,
        "stripe_live": billing.stripe_configured(),
        "founding_spots_left": billing.founding_spots_left(),
        "founding_pct": billing.FOUNDING_PCT_OFF,
        "founding_months": billing.FOUNDING_MONTHS,
        "founding_price": billing.founding_price,
        "trial_days": billing.TRIAL_DAYS})


@app.post("/billing/subscribe")
def subscribe(request: Request, plan: str = Form(...), cycle: str = Form("monthly")):
    provider, redirect = require_login(request)
    if redirect:
        return redirect
    if billing.DEMO_MODE:
        billing.activate_demo_subscription(provider["id"], plan)
        return RedirectResponse("/dashboard", status_code=303)
    if not billing.stripe_configured():
        return HTMLResponse(
            "Online payments aren't configured yet. Please contact support.", status_code=501)
    try:
        base_url = PUBLIC_BASE_URL
        postcard = request.cookies.get("tp_src", "") == "postcard"
        checkout_url = billing.create_checkout_session(provider, plan, cycle, base_url,
                                                       postcard=postcard)
        growth.milestone(provider["id"], "checkout_started")
    except Exception as e:
        print(f"[stripe] checkout error: {e}", flush=True)
        return HTMLResponse("Couldn't start checkout. Please try again.", status_code=502)
    return RedirectResponse(checkout_url, status_code=303)


@app.post("/billing/portal")
def billing_portal(request: Request):
    provider, redirect = require_login(request)
    if redirect:
        return redirect
    if billing.DEMO_MODE or not billing.stripe_configured():
        return RedirectResponse("/billing", status_code=303)
    try:
        base_url = PUBLIC_BASE_URL
        portal_url = billing.create_portal_session(provider, base_url)
    except Exception as e:
        print(f"[stripe] portal error: {e}", flush=True)
        return HTMLResponse("Couldn't open the billing portal. Please try again.", status_code=502)
    return RedirectResponse(portal_url, status_code=303)


# ------------------------------------------------------- reminder engine ----
@app.get("/internal/run-reminders")
def internal_run_reminders_get(request: Request, token: str = ""):
    """The daily cron hits this (GET -> JSON)."""
    if not INTERNAL_CRON_TOKEN or token != INTERNAL_CRON_TOKEN:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    sent = run_reminders()
    n = sum(1 for s in sent if "error" not in s and not s.get("deferred"))
    deferred = sum(1 for s in sent if s.get("deferred"))
    store.record_reminder_run(n)
    try:
        email_result = email_engagement.run(send_email, EMAIL_ACTIVE)
    except Exception:
        email_result = {"error": "Email guidance processing failed; check Admin Email."}
    try:
        setup_result = setup_help.notify(send_email, EMAIL_ACTIVE)
    except Exception:
        setup_result = {"error": "Setup notifications could not be processed; check Admin Setup requests."}
    try:
        import referrals
        referral_result = referrals.run()
    except Exception:
        referral_result = {"error": "Referral credits could not be processed; check service logs."}
    return {"date": today().isoformat(), "sent": n, "deferred": deferred,
            "details": sent, "email": email_result, "setup_help": setup_result,
            "referrals": referral_result}


@app.post("/internal/run-reminders")
def internal_run_reminders_post(request: Request):
    """Dashboard 'Run reminders now' button -> runs, then redirects back to
    the dashboard with a human-readable result banner (never a raw JSON page)."""
    provider, redirect = require_login(request)
    if redirect:
        return redirect
    sent = run_reminders(provider_id=provider["id"])
    n = sum(1 for s in sent if "error" not in s and not s.get("deferred"))
    deferred = sum(1 for s in sent if s.get("deferred"))
    errors = sum(1 for s in sent if "error" in s)
    store.record_reminder_run(n)
    if n == 0 and errors == 0 and deferred > 0:
        return RedirectResponse("/dashboard?ran=quiet", status_code=303)
    return RedirectResponse(f"/dashboard?ran={n}", status_code=303)


# ------------------------------------------------------------ twilio webhook --
@app.post("/webhooks/twilio/status")
async def twilio_status(request: Request):
    """Delivery receipts from Twilio (MessageSid + MessageStatus). A 'failed'
    or 'undelivered' receipt flags the family as unreachable on the dashboard;
    'delivered' clears the flag."""
    form = await request.form()
    if not twilio_signature_valid(request, form, "/webhooks/twilio/status"):
        return PlainTextResponse("forbidden", status_code=403)
    sid = (form.get("MessageSid") or "").strip()
    status = (form.get("MessageStatus") or "").strip().lower()
    if not sid:
        return PlainTextResponse("ok")
    msg = store.get_message_by_sid(sid)
    if msg and msg["family_id"]:
        if status in ("failed", "undelivered"):
            store.set_family_phone_bad(msg["family_id"], True)
        elif status == "delivered":
            store.set_family_phone_bad(msg["family_id"], False)
    return PlainTextResponse("ok")


def _pick_family_for_text(families, day):
    """Choose which family an inbound text refers to when several children
    share one parent number. Earliest open due date first (most overdue) —
    deterministic and matches collection order. The reply always names the
    child when there was ambiguity, so a mismatch is visible immediately."""
    def sort_key(f):
        period = open_period(f, day, skip_paid=False)
        return (due_date_for_family(f, period).isoformat(), f["id"])
    return min(families, key=sort_key)


@app.post("/webhooks/twilio/sms")
async def twilio_sms(request: Request):
    """Inbound texts from families. Twilio POSTs form fields From and Body."""
    form = await request.form()
    if not twilio_signature_valid(request, form, "/webhooks/twilio/sms"):
        return PlainTextResponse("forbidden", status_code=403)
    from_number = (form.get("From") or "").strip()
    body = (form.get("Body") or "").strip()
    keyword = body.upper()

    # Attribute the text by the sender's number: find the provider whose
    # family list contains it. (All providers share one Twilio number, so
    # the recipient number can't distinguish them — and the family row
    # already belongs to exactly one provider.)
    provider, families = None, []
    if from_number:
        for p in store.all_providers():
            fams = store.get_families_by_phone(p["id"], from_number)
            if fams:
                provider, families = p, fams
                break

    if not provider:
        # Unknown number — nobody to attribute this to. Acknowledge politely
        # and stop; message_log requires a provider.
        return twiml("Thanks for your message!")

    local_today = provider_local_now(provider["id"]).date()
    family = _pick_family_for_text(families, local_today)
    multi = len(families) > 1

    store.log_message(provider["id"], family["id"], "in", body or "(empty)",
                      "received")
    # Any reply proves a human reads this number — mark it confirmed.
    for f in families:
        store.set_family_confirmed(f["id"])

    lang = store.family_language(family)
    first = family['name'].split()[0]
    # When several children share the number, name the child in money replies
    # so a mismatch is visible immediately.
    child_note = f" ({family['name']})" if multi else ""
    if keyword in ("PAID", "PAGADO"):
        period = open_period(family, local_today, skip_paid=False)
        try:
            period_label = datetime.strptime(period, "%Y-%m").strftime("%B %Y")
        except ValueError:
            period_label = period
        if family["paid_period"] == period:
            # Duplicate PAID text — already recorded, don't roll into next month.
            reply = (f"¡Está al día, {first}! No debe nada para {period_label}{child_note}. 🎉"
                     if lang == "es" else
                     f"You're all paid up, {first}! Nothing owed for {period_label}{child_note}. 🎉")
        else:
            paid_amount = store.mark_family_paid(family["id"], period, source="reply")
            maybe_send_review_nudge(provider["id"], family)
            amt = f"{paid_amount:,.2f}"
            reply = (f"¡Gracias {first}! Anotado: ${amt} de {period_label}{child_note} como pagado en tu cuenta. 🎉"
                     if lang == "es" else
                     f"Thanks {first}! Recorded ${amt} for {period_label}{child_note} as paid on your account. 🎉")
    elif keyword in ("STOP", "UNSUBSCRIBE", "END", "QUIT", "CANCEL"):
        # The subscription is per phone number: opt out every family sharing it.
        for f in families:
            store.set_family_opt_out(f["id"], True)
        reply = ("Se ha dado de baja de los recordatorios de colegiatura de "
                 "TuitionPing y no recibirá más mensajes. Responda START para "
                 "volver a suscribirse."
                 if lang == "es" else
                 "You've been unsubscribed from TuitionPing tuition reminders "
                 "and won't be texted again. Reply START to resubscribe.")
    elif keyword in ("START", "OPTIN", "IN", "YES", "UNSTOP"):
        # YES/UNSTOP are opt-ins Twilio may auto-answer itself; the app must
        # still clear its own opt-out flag even when it stays silent below.
        for f in families:
            store.set_family_opt_out(f["id"], False)
        reply = ("Se ha vuelto a suscribir a los recordatorios de colegiatura "
                 "de TuitionPing. Hasta 4 mensajes por período de facturación. "
                 "Pueden aplicar tarifas de mensajes y datos. Responda HELP "
                 "para ayuda, STOP para darse de baja."
                 if lang == "es" else
                 "You're resubscribed to TuitionPing tuition reminders. Up to "
                 "4 msgs per billing period. Msg & data rates may apply. "
                 "Reply HELP for help, STOP to opt out.")
    elif keyword == "HELP":
        reply = ("Recordatorios de colegiatura de su proveedor. Responda PAID cuando "
                 "haya pagado, SALDO para ver su saldo, AUSENTE si su hijo faltará, "
                 "STOP para darse de baja."
                 if lang == "es" else
                 "Tuition reminders from your provider. Reply PAID when you've "
                 "paid, BAL for your balance, ABSENT if your child will be out, "
                 "STOP to unsubscribe.")
    elif keyword in ("ABSENT", "SICK", "AUSENTE", "ENFERMO"):
        reason = "sick" if keyword in ("SICK", "ENFERMO") else "absent"
        is_new = store.log_absence(family["id"], local_today.isoformat(), reason)
        kid = store.child_display_name(family, lang)
        reply = (f"Gracias, {kid} quedó marcado ausente hoy. ¡Que se mejore pronto!"
                 if lang == "es" else
                 f"Thanks — {kid} is marked absent today. Hope they feel better soon!")
        if not is_new:
            reply = (f"{kid} ya estaba marcado ausente hoy."
                     if lang == "es" else
                     f"{kid} was already marked absent today.")
    elif keyword in ("BAL", "BALANCE", "SALDO"):
        bal_lang = "es" if keyword == "SALDO" else lang
        period = open_period(family, local_today)
        try:
            if bal_lang == "es":
                _es_months = ["", "enero", "febrero", "marzo", "abril", "mayo",
                              "junio", "julio", "agosto", "septiembre",
                              "octubre", "noviembre", "diciembre"]
                _y, _m = int(period[:4]), int(period[5:7])
                period_label = f"{_es_months[_m]} de {_y}"
            else:
                period_label = datetime.strptime(period, "%Y-%m").strftime("%B %Y")
        except (ValueError, IndexError):
            period_label = period
        if family["paid_period"] == open_period(family, local_today, skip_paid=False):
            reply = (f"¡Está al día, {first}! No debe nada para {period_label}. 🎉"
                     if bal_lang == "es" else
                     f"You're all paid up, {first}! Nothing owed for {period_label}. 🎉")
        else:
            fee = store.get_late_fee(family["id"], period)
            extra = store.outstanding_charges_total(family["id"])
            bal = store.effective_tuition(family) + fee + extra
            pay_urls = store.location_payment_urls(provider["id"])
            pay_link = ""
            try:
                url = pay_urls.get(family["location_id"], "")
                if url:
                    pay_link = f" Pay online: {url}"
            except (KeyError, IndexError, TypeError):
                pass
            fee_note = ""
            if fee > 0:
                fee_note = (f" Incluye un recargo de ${fee:,.2f}."
                            if bal_lang == "es" else
                            f" Includes a ${fee:,.2f} late fee.")
            extra_note = ""
            if extra > 0:
                extra_note = (f" Incluye ${extra:,.2f} en cargos adicionales."
                              if bal_lang == "es" else
                              f" Includes ${extra:,.2f} in extra charges.")
            reply = (f"Hola {first}, su saldo para {period_label}{child_note} es ${bal:,.2f}."
                     f"{fee_note}{extra_note}{pay_link}"
                     if bal_lang == "es" else
                     f"Hi {first}, your balance for {period_label}{child_note} is ${bal:,.2f}."
                     f"{fee_note}{extra_note}{pay_link}")
    else:
        reply = (f"Gracias {first} — hemos pasado su mensaje a su proveedor. "
                 f"Responda PAID cuando haya enviado la colegiatura."
                 if lang == "es" else
                 f"Thanks {first} — we've passed your message "
                 f"to your provider. Reply PAID once tuition is sent.")

    # HELP / STOP / START (+ standard synonyms) are answered automatically by
    # Twilio's messaging service with its own confirmation texts — verified
    # Oct 5, 2026 via live message logs: the webhook's TwiML reply for these
    # keywords is fetched but never delivered, the user receives Twilio's
    # text instead, and Twilio's auto-reply never appears in the logs.
    # Staying silent here guarantees exactly one text; replying too would
    # double-text (or get silently dropped). The app still updates its own
    # records above (opt-out flag) and the inbound is already logged.
    # In demo mode there is no Twilio auto-reply, so the app answers itself.
    TWILIO_AUTO_KEYWORDS = frozenset({
        "HELP",
        "STOP", "UNSUBSCRIBE", "CANCEL", "QUIT", "END",
        "START", "UNSTOP",
    })
    if not SMS_DEMO_MODE and keyword in TWILIO_AUTO_KEYWORDS:
        return Response(content='<?xml version="1.0" encoding="UTF-8"?>'
                                '<Response></Response>',
                        media_type="application/xml")

    # Reply via TwiML only: exactly one response per inbound text. (Also
    # sending via the API would double-send, and API sends to opted-out
    # numbers fail with 21610 — TwiML replies are the reliable path.)
    store.log_message(provider["id"], family["id"], "out", reply,
                      "sent (demo)" if SMS_DEMO_MODE else "sent")
    return twiml(reply)


def send_welcome_text(provider, family):
    """Welcome text when a family is added: identifies the daycare, sets
    expectations, gives STOP instructions (carriers require this on first
    contact). Marks the family welcomed."""
    company = (provider.get("company") or "your daycare").strip()
    lang = store.family_language(family)
    first = family["name"].split()[0]
    text = (f"Hola {first}, somos {company} vía TuitionPing. Recibirá hasta "
            f"4 recordatorios de colegiatura por período de facturación. "
            f"Pueden aplicar tarifas de mensajes y datos. Responda HELP para "
            f"ayuda, STOP para darse de baja."
            if lang == "es" else
            f"Hi {first}, this is {company} via TuitionPing. You'll get up to "
            f"4 tuition reminders per billing period here. Msg & data rates "
            f"may apply. Reply HELP for help, STOP to opt out.")
    if not SMS_DEMO_MODE:
        from sms import send_sms
        for phone in store.family_phones(family):
            try:
                send_sms(phone, text, provider["id"], family["id"])
            except Exception as exc:
                print(f"[welcome] failed to {phone}: {exc!r}", flush=True)
    else:
        for phone in store.family_phones(family):
            store.log_message(provider["id"], family["id"], "out",
                              f"[to {phone}] {text}", "sent (demo)")
    store.set_family_welcomed(family["id"])


def maybe_send_review_nudge(provider_id, family):
    """After a family's Nth logged payment, text the provider's Google review
    link — once per family, only when the provider configured a link."""
    url = store.get_review_url(provider_id)
    if not url or store.review_nudge_sent(family["id"]):
        return
    if store.count_family_payments(family["id"]) < store.REVIEW_NUDGE_PAYMENTS:
        return
    lang = store.family_language(family)
    first = family["name"].split()[0]
    text = (f"¡Gracias {first}! Si está contento con nuestro servicio, "
            f"¿nos dejaría una reseña? {url}"
            if lang == "es" else
            f"Thanks {first}! If we've been helpful, "
            f"would you leave us a quick review? {url}")
    if not SMS_DEMO_MODE:
        from sms import send_sms
        for phone in store.family_phones(family):
            try:
                send_sms(phone, text, provider_id, family["id"])
            except Exception as exc:
                print(f"[review-nudge] failed: {exc!r}", flush=True)
    else:
        for phone in store.family_phones(family):
            store.log_message(provider_id, family["id"], "out",
                              f"[to {phone}] {text}", "sent (demo)")
    store.record_review_nudge(family["id"])
    print(f"[review-nudge] sent to family {family['id']}", flush=True)


# ------------------------------------------------------------ stripe webhook --
@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    if billing.DEMO_MODE or not billing.STRIPE_WEBHOOK_SECRET:
        return JSONResponse({"error": "webhook not configured"}, status_code=501)
    try:
        event = billing.verify_webhook(payload, sig)
    except Exception as e:
        print(f"[stripe webhook] signature verification failed: {e}", flush=True)
        return JSONResponse({"error": "invalid signature"}, status_code=400)
    try:
        result = billing.handle_stripe_event(event)
    except Exception as e:
        print(f"[stripe webhook] handler error: {e}", flush=True)
        return JSONResponse({"error": "handler failed"}, status_code=500)
    return result


# ------------------------------------------------------------------- admin --
SPAM_HALLMARKS = [
    "bit.ly", "tinyurl", "t.co/", "goo.gl", "ow.ly", "is.gd", "buff.ly",
    "free money", "you've won", "you won", "claim your prize", "claim now",
    "act now", "limited time offer", "congratulations you", "dear friend",
    "wire transfer", "crypto giveaway",
]


def scan_templates(tpl) -> list:
    """Spam hallmarks in a provider's message templates (flag, don't block)."""
    try:
        text = " ".join(tpl[k] for k in
                        ("tpl_before", "tpl_due", "tpl_late3", "tpl_late7")).lower()
    except (KeyError, IndexError, TypeError):
        return []
    return sorted({h for h in SPAM_HALLMARKS if h in text})


@app.get("/admin", response_class=HTMLResponse)
def admin_customers(request: Request):
    """Customer overview: who they are, how long they've been here, plan."""
    _, redirect = require_admin(request)
    if redirect:
        return redirect
    stats = store.admin_provider_stats()
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    new_count = 0
    for s in stats:
        s["is_new"] = (s.get("created_at") or "") >= week_ago
        if s["is_new"]:
            new_count += 1
    for s in stats:
        sub = s.get("subscription") or {}
        plan = sub.get("plan")
        s["plan_display"] = billing.PLANS.get(plan, {}).get("name", (plan or "—").title()) \
            if plan else "—"
        s["sub_status"] = sub.get("status") or "—"
        trial = (sub.get("trial_ends_at") or "")[:10]
        s["trial_ends"] = trial
    return templates.TemplateResponse(
        request, "admin.html",
        {"request": request, "stats": stats, "new_count": new_count,
         "admin_configured": bool(ADMIN_EMAILS)})


HEARD_ABOUT_LABELS = {
    "postcard": "Postcard in the mail",
    "walkin": "In-person visit",
    "search": "Google search",
    "referral": "Referred by another center",
    "social": "Social media",
    "other": "Other",
}


@app.get("/admin/attribution", response_class=HTMLResponse)
def admin_attribution(request: Request):
    """Campaign tracking: postcard QR visits vs. signups vs. paying customers."""
    _, redirect = require_admin(request)
    if redirect:
        return redirect
    rows = store.admin_attribution_list()
    visits = store.count_postcard_visits()
    attributed = [r for r in rows if (r["signup_source"] or "") == "postcard"]
    customers = [r for r in attributed
                 if (r["sub_status"] or "") in ("trialing", "active")]
    for r in rows:
        r["heard_label"] = HEARD_ABOUT_LABELS.get(r["heard_about"] or "", r["heard_about"] or "—")
        r["src_label"] = "Postcard QR" if (r["signup_source"] or "") == "postcard" else "—"
        r["signed_up"] = (r["created_at"] or "")[:10]
        st = r["sub_status"] or ""
        r["customer_label"] = {"trialing": "Trial", "active": "Active"}.get(st, "—")
    pct = lambda a, b: f"{(100.0 * a / b):.1f}%" if b else "—"
    return templates.TemplateResponse(
        request, "admin_attribution.html",
        {"request": request, "rows": rows, "visits": visits,
         "attributed": len(attributed), "customers": len(customers),
         "signup_rate": pct(len(attributed), visits),
         "customer_rate": pct(len(customers), len(attributed))})


@app.get("/admin/conversions", response_class=HTMLResponse)
def admin_conversions(request: Request, days: int = 28):
    provider, redirect = require_admin(request)
    if redirect:
        return redirect
    response = templates.TemplateResponse(request, "admin_conversions.html", {
        "request": request, "provider": provider, "report": growth.report(90 if days == 90 else 28)})
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Robots-Tag"] = "noindex"
    return response


@app.post("/admin/conversions/reset")
def admin_reset_conversions(request: Request, days: int = Form(28)):
    provider, redirect = require_admin(request)
    if redirect:
        return redirect
    growth.reset_report(provider["id"])
    response = RedirectResponse(f"/admin/conversions?days={90 if days == 90 else 28}&reset=1", status_code=303)
    response.headers["Cache-Control"] = "private, no-store"
    return response


@app.get("/admin/visitors", response_class=HTMLResponse)
def admin_visitors(request: Request):
    """First-party site analytics: individual visitors (hashed IPs) and their
    page timelines. No personal identity is stored."""
    _, redirect = require_admin(request)
    if redirect:
        return redirect
    visits, rollup = store.site_visit_stats()
    for v in visits:
        v["when"] = (v["ts"] or "")[:16].replace("T", " ")
        v["short_id"] = (v["ip_hash"] or "")[:8]
        ua = v["ua"] or ""
        v["device"] = "📱" if ("Mobile" in ua or "Android" in ua or "iPhone" in ua) else "🖥"
        ref = v["referrer"] or ""
        v["ref_label"] = ref.replace("https://", "").replace("http://", "").split("/")[0][:28] or "direct"
    for r in rollup:
        r["short_id"] = (r["ip_hash"] or "")[:8]
        r["first"] = (r["first_ts"] or "")[:16].replace("T", " ")
        r["last"] = (r["last_ts"] or "")[:16].replace("T", " ")
    today = datetime.now().date().isoformat()
    today_visitors = {r["short_id"] for r in rollup if (r["last"] or "")[:10] == today}
    return templates.TemplateResponse(
        request, "admin_visitors.html",
        {"request": request, "visits": visits, "rollup": rollup,
         "total_visits": len(visits), "total_visitors": len(rollup),
         "today_visitors": len(today_visitors)})


@app.get("/admin/abuse", response_class=HTMLResponse)
def admin_abuse(request: Request):
    _, redirect = require_admin(request)
    if redirect:
        return redirect
    stats = store.admin_provider_stats()
    for s in stats:
        s["template_flags"] = scan_templates(store.get_templates(s["id"]))
        sent = s["sent_30d"] or 0
        s["optout_rate"] = (s["optouts"] / sent) if sent else 0
        flags = []
        if sent >= 10 and s["optout_rate"] > 0.05:
            flags.append(("bad", f"High opt-out rate ({s['optout_rate']:.0%} of texts)"))
        if s["families_24h"] >= 20:
            flags.append(("warn", f"{s['families_24h']} families added in 24h"))
        if s["sent_24h"] >= 200:
            flags.append(("warn", f"{s['sent_24h']} texts sent in 24h"))
        for h in s["template_flags"]:
            flags.append(("warn", f"Template wording: “{h}”"))
        if s["suspended"]:
            flags.append(("bad", "Suspended"))
        s["flags"] = flags
    return templates.TemplateResponse(
        request, "admin_abuse.html",
        {"request": request, "stats": stats,
         "admin_configured": bool(ADMIN_EMAILS)})


@app.get("/admin/suggestions", response_class=HTMLResponse)
def admin_suggestions(request: Request):
    _, redirect = require_admin(request)
    if redirect:
        return redirect
    suggestions = store.list_suggestions()
    return templates.TemplateResponse(
        request, "admin_suggestions.html",
        {"request": request, "suggestions": suggestions})


@app.post("/admin/suggestions/delete")
def admin_suggestion_delete(request: Request, suggestion_id: int = Form(...)):
    _, redirect = require_admin(request)
    if redirect:
        return redirect
    store.delete_suggestion(suggestion_id)
    return RedirectResponse("/admin/suggestions", status_code=303)


@app.post("/admin/founding/release")
def admin_founding_release(request: Request, provider_id: int = Form(...)):
    """Free a founding spot without deleting the account (e.g. test checkouts)."""
    _, redirect = require_admin(request)
    if redirect:
        return redirect
    store.set_founding(provider_id, False)
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/suspend")
def admin_suspend(request: Request, provider_id: int = Form(...),
                  suspend: str = Form(...)):
    admin, redirect = require_admin(request)
    if redirect:
        return redirect
    if provider_id == admin["id"]:
        return HTMLResponse("You can't suspend your own account.", status_code=400)
    store.set_provider_suspended(provider_id, suspend == "1")
    if suspend == "1":
        # Log them out everywhere immediately: a suspended account must not
        # keep working through an existing session.
        store.delete_provider_sessions(provider_id)
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/delete")
def admin_delete(request: Request, provider_id: int = Form(...),
                 confirm_email: str = Form("")):
    admin, redirect = require_admin(request)
    if redirect:
        return redirect
    if provider_id == admin["id"]:
        return HTMLResponse("You can't delete your own account.", status_code=400)
    target = None
    for s in store.admin_provider_stats():
        if s["id"] == provider_id:
            target = s
            break
    if not target or (target["email"] or "").lower() != confirm_email.strip().lower():
        return HTMLResponse(
            "Type the account's email exactly to confirm deletion."
            " <a href='/admin'>Go back</a>", status_code=400)
    # Cancel live Stripe billing FIRST. If that fails we stop here rather
    # than orphaning a subscription the customer can no longer manage.
    bill = billing.cancel_provider_billing(provider_id)
    if not bill["ok"]:
        return HTMLResponse(
            f"Account NOT deleted — {bill['error']}. "
            "Resolve billing in Stripe, then try again."
            " <a href='/admin'>Go back</a>", status_code=502)
    store.delete_provider(provider_id)
    return RedirectResponse("/admin", status_code=303)


# ------------------------------------------------------------------ health ----
@app.get("/healthz")
def healthz():
    return {"ok": True, "db": "postgres" if store.USE_PG else "sqlite"}


# TEMPORARY (remove after use): one-time data repair — undo an erroneously
# recorded payment period (e.g. a duplicate parent PAID text that rolled a
# payment into the next month). GET looks up families by phone; POST reverts.
_REOPEN_TOKEN = "egwpKXbLrwIHZHCFdMg9ApA3lFR4MeqB"


@app.get("/internal/reopen-period")
def internal_reopen_lookup(request: Request, token: str = "", phone: str = ""):
    if token != _REOPEN_TOKEN:
        return JSONResponse({"ok": False}, status_code=404)
    out = []
    if phone:
        for p in store.all_providers():
            for f in store.get_families_by_phone(p["id"], phone):
                d = dict(f)
                out.append({"id": d["id"], "name": d["name"],
                            "paid_period": d.get("paid_period"),
                            "paid_source": d.get("paid_source", ""),
                            "next_due_date": d.get("next_due_date"),
                            "due_day": d.get("due_day")})
    return {"ok": True, "families": out}


@app.post("/internal/reopen-period")
async def internal_reopen(request: Request):
    form = await request.form()
    if form.get("token") != _REOPEN_TOKEN:
        return JSONResponse({"ok": False}, status_code=404)
    family_id = int(form.get("family_id") or 0)
    undo_period = (form.get("undo_period") or "").strip()
    restore_period = (form.get("restore_period") or "").strip()
    restore_next_due = (form.get("restore_next_due") or "").strip() or None
    if not family_id or not undo_period or not restore_period:
        return JSONResponse({"ok": False, "error": "missing params"},
                            status_code=400)
    with store.db() as conn:
        cur = conn.execute("SELECT paid_period FROM families WHERE id = ?",
                           (family_id,)).fetchone()
        if not cur or cur["paid_period"] != undo_period:
            return JSONResponse({"ok": False, "error": "paid_period mismatch"},
                                status_code=400)
        conn.execute("UPDATE families SET paid_period = ?,"
                     " paid_source = 'manual', next_due_date = ? WHERE id = ?",
                     (restore_period, restore_next_due, family_id))
        conn.execute("DELETE FROM paid_log WHERE family_id = ? AND period = ?",
                     (family_id, undo_period))
        conn.execute("UPDATE extra_charges SET settled_period = NULL"
                     " WHERE family_id = ? AND settled_period = ?",
                     (family_id, undo_period))
    return {"ok": True, "family_id": family_id,
            "paid_period": restore_period, "next_due_date": restore_next_due}


# TEMPORARY (remove after test): give rob@tuitionping.com a fake Micro
# subscription for walkthroughs. No Stripe objects are touched.
@app.post("/internal/fake-sub")
def internal_fake_sub(request: Request, token: str = "", action: str = ""):
    if token != "IHIlb8jch6mLiwv1Fz0HxCi67yhDepHA":
        return JSONResponse({"ok": False}, status_code=404)
    p = store.get_provider_by_email("rob@tuitionping.com")
    if not p:
        return JSONResponse({"ok": False, "error": "no provider"}, status_code=400)
    now = datetime.now(timezone.utc).isoformat()
    with store.db() as conn:
        if action == "remove":
            conn.execute("DELETE FROM subscriptions WHERE provider_id = ?",
                         (p["id"],))
            return {"ok": True, "removed": True}
        if action != "add":
            return JSONResponse({"ok": False}, status_code=400)
        trial_ends = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        conn.execute(
            "INSERT INTO subscriptions (provider_id, plan, status,"
            " trial_ends_at, updated_at) VALUES (?,?,?,?,?)"
            " ON CONFLICT(provider_id) DO UPDATE SET plan='micro',"
            " status='trialing', trial_ends_at=excluded.trial_ends_at,"
            " updated_at=excluded.updated_at",
            (p["id"], "micro", "trialing", trial_ends, now))
    return {"ok": True, "added": True, "plan": "micro", "status": "trialing"}


# Callbacks stay dynamic so tests and runtime configuration use the same sender.
import email_routes
email_engagement.BASE = PUBLIC_BASE_URL
email_routes.register(app, templates, require_admin,
                      lambda *args, **kwargs: send_email(*args, **kwargs),
                      lambda: EMAIL_ACTIVE)

setup_wizard.register(app, templates, require_login=require_login, location_limit=_location_limit_response,
                      timezones=TIMEZONES, hours=hour_options, preview_texts=template_previews,
                      test_preview=test_text_preview, sms_demo=SMS_DEMO_MODE, needs_verification=needs_verification)

partner_resources.register(app, templates, require_admin)

setup_help.register(app, templates, require_admin,
                    lambda *args, **kwargs: send_email(*args, **kwargs), lambda: EMAIL_ACTIVE)
