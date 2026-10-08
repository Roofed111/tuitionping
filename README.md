# TuitionPing v1 — working prototype

Automated tuition reminders + late-nudge texts for small providers.
Website for the provider, plain SMS for the family — nobody installs anything.

## What's in the box

| File | Job |
|---|---|
| `app.py` | FastAPI routes: landing, auth, dashboard, settings, billing, webhooks |
| `store.py` | SQLite data layer (providers, locations, classrooms, families, logs, …) |
| `reminders.py` | The daily engine — `run_reminders()` decides who gets texted today |
| `sms.py` | Sends SMS; in demo mode it prints to console + logs instead |
| `billing.py` | Plans + demo subscribe; real Stripe hooks go here later |
| `templates/` | Server-rendered pages (no frontend build step) |

## Run locally

```bash
cd ~/workspace/tuitionping
pip install -r requirements.txt
uvicorn app:app --reload
# open http://localhost:8000
```

Everything works with zero credentials: SMS is simulated (`sent (demo)` in the
message log + printed in the terminal), and Subscribe buttons activate plans
instantly with a 14-day trial.

**Time-travel for testing:** `TUITION_PING_TODAY=2026-10-04 uvicorn app:app --reload`
makes the reminder engine behave as if it is that date — handy for watching all
four reminder stages fire.

## The reminder engine

`run_reminders()` (in `reminders.py`) runs once a day and sends:

- **−3 days** — friendly "tuition is due soon" reminder
- **due date** — "tuition is due today"
- **+3 / +7 days** — late nudges, only while that month is still unpaid

Each (family, month, stage) is recorded in `reminder_log`, so re-running never
double-texts. Families can reply `PAID` (marks them paid), `STOP` (opts out,
honored forever), `START` (re-subscribes), or `HELP`.

## Go live (later, with Rob)

1. **Twilio** — buy a number; set `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`,
   `TWILIO_FROM_NUMBER`, and `DEMO_MODE=0`. Point the number's messaging webhook
   at `https://<your-domain>/webhooks/twilio/sms` (HTTP POST).
2. **A2P 10DLC** — register in the Twilio console (~$15, 5–10 day approval)
   before texting real families.
3. **Stripe** — set `STRIPE_SECRET_KEY` and `DEMO_MODE=0`; point the Stripe
   webhook at `https://<your-domain>/webhooks/stripe`.
4. **Daily cron** — on Railway, add a Cron Job service on schedule
   `0 9 * * *` that calls `https://<your-domain>/internal/run-reminders?token=...`
   (set `INTERNAL_CRON_TOKEN` to the same value). That is the whole "worker".

## Deploy to Railway

New project → deploy from this repo. `railway.toml` sets the start command and
health check (`/healthz`). Add env vars in the Railway dashboard. Railway
assigns a public domain automatically.

## Notes

- SQLite locally; Postgres in production when `DATABASE_URL` is set (Railway).
  `store.py` picks the backend automatically — `/healthz` reports which one is live.
- No secrets are stored in code. Ever.
- Demo data: none is seeded — sign up and add a location to see it work.

## Email list, campaigns and trial follow-ups

Open **Admin → Email** (`/admin/email`). This is the documented, persistent
email list; production records live in the existing Postgres database, not a
temporary spreadsheet. Visitors can request the free collection kit from
`/guides` or `/email-kit`. Direct downloads remain available.

- Marketing is an unchecked, optional checkbox. A request stores consent
  wording/version, source and timestamp; the email confirmation completes
  enrollment. Account creation and kit delivery do not grant marketing consent.
- **Export confirmed subscribers (CSV)** includes only confirmed, unsuppressed
  marketing subscribers and their consent records. Do not add pending,
  account-only, unsubscribed or suppressed contacts to an external mailing tool.
  Refresh the export immediately before using it. External tools must honor
  their own unsubscribe/suppression records too.
- Save a valid business mailing address in Email settings before enabling
  campaigns or setup follow-ups. A registered PO box/business mailbox is fine.
  Leaving it empty pauses those sends. Requested resource and confirmation
  emails still work through the existing Resend sender configuration.
- Write tips or real product improvements in **Create a marketing campaign**.
  Save a draft, review the rendered message and recipient count, then explicitly
  queue it. The audience is snapshotted once; repeat submissions do not add
  recipients or resend. Every email is sent individually with its own opt-out.
  Campaign calls to action carry UTM attribution without recipient identities.
- The existing authenticated hourly cron calls the reminder endpoint and then
  processes email guidance/outbox messages. No new cron or secret is needed.
  The admin can process a batch immediately using the same eligibility checks.
  This sends at most 20 messages per run. Errors in email processing do not
  block the tuition-reminder result.
- Follow-ups target verified, unsuspended accounts: unfinished checkout after
  24 hours (only during the first seven days); no families after one trial day;
  no scheduled reminder after three trial days; billing review within three
  days of trial end. Each stage is sent at most once, with at least 48 hours
  between setup guidance emails. Current progress is checked again at send.
  Active/canceled/expired accounts and scheduled cancellations are excluded.
  Live trial billing is refreshed before guidance; a failed billing refresh
  defers that guidance. Optional guidance has opt-out
  links; unsubscribing leaves necessary security/billing notices and SMS alone.
- The durable outbox claims deliveries atomically and uses stable Resend
  idempotency keys/payloads. Failed requests get up to three attempts within
  23 hours. Interrupted/unknown deliveries outside that window become
  `uncertain` and require review in the email provider, rather than an automatic
  duplicate. Provider acceptance is not proof of delivery or an open.
- Use **Suppress all optional sends** for complaints or bounced addresses. It
  blocks all queued messages for that contact, including resource requests.
  Check the sending provider's delivery/bounce/complaint logs when monitoring
  campaigns; this screen reports API acceptance rather than webhook delivery.

Consent/preference/queue behavior is covered by
`python -m unittest discover -s tests -p 'test_*.py'`. Tests use a temporary
SQLite database and mocked email delivery; they never email real contacts.

## Four-step setup and public document resources

The dashboard links to `/setup`: program details, families, preview, test text.
The program step configures the selected location and shared account sending
hours, and creates a Families group only when that location has none. Existing
locations, groups and families are retained. Family add and CSV import reuse
existing verified-email, consent, ownership and plan checks; each saved family
still receives the existing welcome text. Import results link back to setup.

Preview uses fictional names and amounts with the account's actual templates,
program details and payment link. The displayed test is the same payload sent
by the existing test-text action. Review and test completion are saved in
`setup_progress`; changing program, families, sending settings or templates
invalidates the earlier review/test. GET requests never send texts. Sending a
test requires an explicit own-number attestation in setup, is limited to three
per UTC day (matching the log), and records completion only after acceptance.
Acceptance is not delivery. No reminder runs are required to finish setup.

Public `/tools/daycare-invoice-receipt` creates USD invoices and receipts in
browser memory. It stores no form data, sends no document fields, and never
updates account billing or payment ledgers. Print / save as PDF is provided.
Money uses integer cents; partial payments are separate from previous verified
payments. A receipt requires a positive amount and provider verification.
Overpayments, excessive credits and invalid dates/quantities are rejected.
Changing fields clears the previous output. The optional analytics event stores
only invoice or receipt, never document content. `/guides/tuition-collection`
is the English bill-to-receipt resource hub, linked to the tools and `/demo`.

## Partner resource distribution

Public `/partners` and `/partners/download` provide a nine-file kit: an HTML
handout, share-ready newsletter/group/bookkeeper copy and workshop outline,
monthly checklist, existing Word/PDF policy and bilingual reminder templates,
and the Excel tracker. This is resource sharing, without referral commissions.
Register an organization in **Admin → Partners** (`/admin/partners`). Names and
codes appear publicly; use business labels without private contact details.
Copy its assigned link and download its personalized kit. Distribute that
link or personalized handout; the generic kit has no assigned partner credit.
The kit's tool links land on the assigned resource page before the user opens
the selected tool, preserving attribution in a new browser. Existing included
PDF/Word/XLSX documents are generic resources; use the personalized handout
and share-ready copy to distribute tracked links.

The invoice generator and partner landing include a collapsible two-minute
invoice/partial-payment receipt tutorial, with first-party MP4 playback,
English WebVTT captions, a poster and `preload="none"`. The video is silent
and uses real tool captures with fictional data. Its YouTube viewing link is
`https://youtu.be/B17VIKwHMJI`; the page does not load third-party embeds.
The handout and share-ready copy include a personalized `resource=invoice-tutorial`
link, establishing partner attribution before the viewer opens the tool/video.
The tutorial stays out of printed invoices and receipts. Existing acquisition
reporting tracks visits, document generation and customer milestones; this
tutorial does not report plays as homepage walkthrough events.

The latest registered partner landing in the same browser within 30 days
before signup is copied into `growth_partner_accounts` once. Later partner
visits do not change that account's credit. Existing first-touch Conversions
reporting remains separate. Partners shows aggregate browser visits, demo use,
downloads and distinct accounts/trials/first real reminders/paid subscriptions
for 28- or 90-day signup/touch cohorts. Existing live billing and SMS milestones
provide those business outcomes. A link association is not proof of causation.
Opt-out and logged-in browsing exclusions remain in force. Known Bot and
Likely Bot attribution is excluded from customer statistics. Raw browser
attribution history and partner definitions remain saved for auditing.
No recipient contacts or family data are shared with partners, and creating a
kit or partner link sends no outreach message.

Validation: Python unittest discovery; `NODE_PATH=/tmp/tuitionping-ui/node_modules
node --test tests/invoice-ui.test.cjs` for exact cents, partial receipts,
verification, stale output, escaping, local-only data and print dispatch.

## Setup-help requests and support follow-up

`/setup-help` is available without login or checkout. The homepage, signup,
Support and setup wizard link to it. The form asks for name, email, optional
program name, family-count range, account stage, topic and a brief optional
note. Explicit permission authorizes a response about this request only; no
marketing contact, customer account, parent SMS or checkout is created.

Requests are stored in `setup_help_requests` and visible only through
**Admin → Setup requests** (`/admin/setup-help`). New requests queue an email
notification to `rob@tuitionping.com` through the existing Resend sender. The
request stays saved if email delivery is disabled or fails. Notification
acceptance is not inbox delivery. The existing hourly reminder scheduler also
processes pending setup notifications; the admin inbox can retry due entries.
Atomic claims and a stable idempotency key prevent concurrent duplicate sends.
Stale claims can be retried within 23 hours of the first attempt; uncertain
older sends stop and require reviewing the email provider. This inbox never
emails a requester automatically.

Reply using the request's **Reply by email** link. The private guide and first
reply draft are included in the inbox. Ask program size, billing frequency,
payment method and account stage; make sure the actual billing schedule is
supported. Recommend the capacity/location plan, and let the provider create
and verify their account and complete their own Stripe checkout. Help explain
the CSV columns using sample rows; real family information stays in the
provider's file and is uploaded by them after confirming SMS consent. Saving
families sends the existing welcome text. Walk through program details →
families → preview → test text, using their own number for the test. Explain
that a parent PAID reply must be checked against their payment records before
confirming receipt. Never request passwords, card details or emailed family
lists. Confirm they can proceed, then mark Replied, Helping, Complete or Not
proceeding and save private next-action notes. Older requests remain accessible
through pagination.

CSRF protects public and admin forms; public requests also use a honeypot,
five-request hourly hashed-IP limit, field limits, and 16 KiB body cap. Forms
and inbox responses are private/no-store/noindex. Acquisition reporting counts
accepted setup-help requests without storing form content in analytics and
honors the existing DNT/GPC exclusions. Request contact and operational notes
remain outside the marketing list. Privacy disclosures cover these records
and the existing document/partner section is rendered in the visible content.

Validation: `python -m unittest discover -s tests -p 'test_*.py'` covers public
CSRF, request persistence, duplicate submits, validation/rate controls,
support notifications, failed-send recovery, retry-window limits, cron
auth, private admin access/status editing, escaping and tracking opt-outs.
All delivery is mocked with temporary providers and SQLite records.


## Customer referral account credits

The existing “give a month, get a month” program is recorded by the signup
referral code. Checkout never grants a reward. `referrals.py` accepts a live,
positive paid invoice for a full recurring TuitionPing month/year (not a trial,
zero-dollar invoice, manual payment, proration or subscription adjustment).
Monthly qualification uses the invoice line’s service-period end; annual
qualification uses one calendar month from its paid service-period start.

The existing hourly `/internal/run-reminders` cron runs referral reconciliation.
It scans missing paid invoices daily, then rechecks due invoices and service
termination against Stripe before issuing credit. Refunds, credit notes,
disputes and early service termination disqualify the reward. Stripe failures
remain pending; the cron response reports referral grant/error counts. No new
webhook event subscription or cron service is required.

Both accounts receive their own plan’s monthly list price in USD account credit,
including annual accounts. The credit applies to future invoices and carries
forward. Delivery has one durable row per referral/beneficiary, frozen request
parameters, database locking, a Stripe idempotency key and transaction-metadata
reconciliation for retries beyond Stripe’s 24-hour key retention. Each benefit
is counted only once it is issued. Existing `referral_rewards` rows are legacy
rewards and never trigger a second grant; no historical credits are clawed back.


## Landing-page product walkthrough

The homepage embeds a 48-second illustrated H.264 MP4 in `static/media/`,
showing a scheduled reminder, the parent’s PAID report, an external payment
record check and the actual **Confirm payment received** dashboard action.
All data is fictional; no account, text or payment is created by playback.
On-screen explanations work without audio; English WebVTT captions and an
HTML transcript are available. Native controls, inline mobile playback, a
poster and `preload="none"` keep the initial page load light. The hero’s video
link leads to the player, followed by the existing interactive demo and setup
help. A downloadable MP4 can be reused in partner packages and outreach.

Rebuild locally with Pillow and ffmpeg using `python3 scripts/build_walkthrough.py`.
These are development tools; no production dependencies were added.
First-party `video_started` and `video_completed` events appear in the existing
growth report. Completion requires approximately 85% of content time played;
a seek directly to the end does not count. Analytics are browser estimates,
respect existing privacy opt-outs and do not represent paid conversions.

## Human and automated visitor analytics

`traffic.py` classifies the existing `growth_visitors` identifiers and
`site_visits` hit log; it does not create a second analytics collector.
Additive SQLite/Postgres migration runs before deployment health checks.
New visitor fields record classification, JSON reasons, 0–100 bot risk,
User-Agent, referrer hostname, keyed network hash, last seen/page, hit/page
counts, browser execution, automation flag, identity kind, historical evidence,
classification version and pending review. `site_visits` gains `visitor_id`,
`status_code` and `request_type`. `traffic_classifications` records decision
changes for auditing. Indexes support recent hits, distinct paths, classes,
clusters, historical migration and pending review. No original rows are reset
or deleted; conversion/account/partner links no longer expire after 90 days.

The maintainable local User-Agent list identifies crawlers, previews, monitors,
scanners, HTTP libraries and detectable browser automation as **Known Bot**.
An explicit `navigator.webdriver` signal also identifies browser automation.
**Likely Bot** requires risk at least 55 and two independent signal groups:
velocity, repetition, probing, identical-network/browser/page sessions beginning
in the same second, missing User-Agent, and absent browser execution after a
20-second grace period. Velocity uses at most the most recent 64 requests in
an observed minute. A cluster requires at least 12 browser identifiers with
matching network, User-Agent, landing and start second; shared-network traffic
with confirmed execution is not removed by the cluster alone. JS confirmation
reduces risk but cannot override an obvious crawler or an excessive burst.

A normal browser with confirmed execution and no suspicious signals is
**Human**. Uncertain new traffic is **Likely Human**. One page, brief visits,
no referrer, direct/QR sources, refreshes, Safari/iPhone, privacy preferences,
and missing JS alone never establish a bot. Known Bot is retained; a Likely
Bot decision does not disappear merely because time passed, but a later calm,
browser-confirmed visit may recover. Decision changes remain in the audit log.

`growth.js` sends one signed `browser_verified` beacon after two render frames.
It updates an already-recorded visitor/path without adding a page view, hit,
visitor or conversion event. Existing cookie signing, event validation and
DNT/GPC opt-outs remain intact. Anonymous opt-out/crawler hits use a keyed
30-minute network/browser estimate instead of setting a new analytics cookie.
Raw IPs are not stored; new referrers retain the hostname only and failed URLs
retain a safe category, without query values or private scanner paths.

A bounded background task runs every 30 seconds to migrate 250 historical
network/User-Agent groups and review 250 pending sessions. Admin reports also
advance those batches. Historical known crawlers are Known Bot; historical
normal browser User-Agents are Likely Human. Insufficient evidence stays
**Unknown / Unclassified**. Old network hits cannot be reliably matched to
old conversion cookies, so those cookie cohorts remain available in Unknown
audit instead of inventing a link. Shared networks, cleared cookies, UA spoofing,
and automation that mimics a normal browser remain estimation limits.

**Admin → Visitors** defaults to Human / Likely Human, with today/total cards
for human, automated and all traffic. It offers All/Human/Automated/Unknown
filters, expandable reasons, a request-spike indicator, and server-side sorting
of all eight visitor columns across 100-row pages. Rates in Conversions and
Postcard attribution use the same eligible visitor IDs in numerator and
denominator. Partner customer reporting also excludes Known/Likely Bot
attribution. Raw conversion events stay available in Conversions audit filters;
server business milestones remain deduplicated. Paid customer events require
live, positive subscription payment confirmation, never a parent's PAID reply.
All Admin display and today boundaries use `America/Los_Angeles`, including
23/25-hour DST days and PST/PDT labels. Classification neither blocks traffic
nor changes search crawling, product flows, CAPTCHA or network rules.

Validation: `python -m unittest discover -s tests -p 'test_*.py' -q`; install
jsdom, xlsx and xlsx-calc outside the repository, then
`NODE_PATH=/tmp/tuitionping-traffic-ui/node_modules node --test tests/*.test.cjs`.
`tests/test_traffic.py` covers browser/QR/privacy/one-page cases, known clients,
rapid and simultaneous sessions, delayed review, history, concurrent hits,
filters/sorting/pagination, raw conversion retention, verified billing,
partner/QR filtering, exact visitor rates, Pacific/DST and fail-open behavior.
`tests/growth-ui.test.cjs` verifies render timing, one beacon, signed token,
privacy exclusions, explicit automation, existing clicks and silent failure.

### Scanner reconnaissance and durable automated counts (classification v2)

The local scanner catalog recognizes WordPress login/admin/XML-RPC/REST,
plugin/theme/include paths, configuration backups, secret files, Git exposure,
PHP execution/scanner scripts, application admin panels, and explicit exploit
query signatures. Reconnaissance targets plus an unsuccessful response on this
non-WordPress app are Likely Bot (risk 75). Explicit secret-file/code-execution
or exploit-payload requests are Known Bot (risk 95); known scanner User-Agents
remain Known Bot (risk 100). Normal typos/404s, direct/QR traffic and single-page
browser visits are still eligible. JS execution cannot wash away scanner evidence.

Existing version-one normalized probe records are reviewed in bounded batches
and moved to Automated without deleting hits, cookies, account links or events.
Their coarse target family is retained; an exact historical target is never
invented. Three additive visitor fields store scanner kind, JSON evidence and
stable automated network key; hit fields add method, safe probe family and
catalog target. Strong scanner requests are logged for all HTTP methods,
including failed CSRF/authorization responses and requests carrying a bogus
login cookie. Request bodies and arbitrary query payloads are never retained.

Anonymous scanner identifiers are stable across 30-minute slots and User-Agent
rotation. Known client identifiers are stable across client versions. Automated
Today/Total and All Traffic visitor metrics group bots by the existing keyed
network hash even when they rotate signed browser cookies. Raw identifiers and
every hit are retained, so the table counts matching records while automated
cards count network groups. Human visitors sharing the same network are not
classified by someone else's scan. Human conversions exclude scanner-linked
records; All Traffic audit visitor denominators also use stable network groups.
The request audit now paginates every matching retained request (100 per page),
with method, response, safe scan target and per-request User-Agent. Visitor
sorting and Pacific timestamps remain unchanged. Changing networks can still
create new source groups; these are estimates, not identified people.

Validation: test_scanner_traffic.py covers single WordPress probes, stronger
exploit targets, encoded paths, other application scans, normal missing routes,
120 anonymous sessions across time/UA changes, 15 rotating signed cookies,
original record preservation/reclassification, shared-network humans, POST/CSRF
scans, query privacy, raw conversions, and full request-audit pagination.

### Stronger engagement counts and detailed sources

Admin Visitors and Conversions default to **Engaged**, rather than combining
every Human/Likely Human estimate. The main cards and conversion denominator
require `engagement_confirmed`: existing browser execution plus a signed,
path-matched interaction after eight visible seconds, scrolling after thirty
visible seconds, or a retained server-side account milestone for a real provider.
The client ignores synthetic input and hidden-tab time and sends no keystrokes,
coordinates, form values or personal identity. Its engagement event does not
create visitors, hits or page views. Sophisticated automation can imitate these
signals; the UI describes evidence, not certainty. Known/Likely Bot always
overrides a positive engagement flag. Account activity does not turn an account
into a paid customer: the existing positive, live Stripe checks still apply.

Browser-only and Unconfirmed filters preserve visits without enough engagement
evidence. All likely people retains the former broader report. Historical quiet
visits are not retroactively claimed as engaged or relabeled as bots. Existing
server-side account records can establish engagement in bounded report batches.
Pacific timestamps, sorting, conversion reset boundaries and all audit data stay
intact. Additional visitor columns are `engagement_confirmed`, `engaged_at`,
`engagement_reason`, and `attribution_json`, plus one supporting index.

`acquisition.py` centralizes first-touch Google/Bing/other search, paid marker,
social, QR, referral and unavailable-source labels. It retains safe public UTM
content/term labels, referring host, a whitelisted search-referrer path, and the
attribution basis. No arbitrary referral queries, search strings, click IDs or
private URL paths are saved. First-touch detail is immutable. A campaign term is
explicitly labeled a campaign keyword, never a visitor's proven organic query.
Source quality compares engaged, browser-only, unconfirmed, demo and account
counts in the acquisition cohort, excluding bots.

Conversions includes a dated, aggregate Google Search Console keyword report
with clicks, impressions, calculated CTR and average position. The initial
GSC Wizard/API snapshot returned no query rows for 2026-09-08 through 2026-10-05;
the empty state does not claim zero people visited. Google anonymizes some
queries, and this report cannot associate a keyword with a visitor. It does not
auto-refresh: Admin can open Search Console or upload its Queries.csv and date
range. Protected uploads accept up to 512 KB / 1,000 rows, reject invalid metrics
and personal contact strings, and append immutable `search_query_imports`
snapshots. They do not reset conversion or visitor records. No third-party API
is called while serving a public page.

Validation: 167 Python regression tests and the browser engagement DOM tests
cover stricter conversion rates, browser-only/quiet one-page visitors, visible
timing, bot overrides, signed event validation, first-touch preservation, search
versus Gmail/maps/social/referral, marker/query privacy, keyword import limits,
Admin protection and Pacific time.
