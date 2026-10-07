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

The latest registered partner landing in the same browser within 30 days
before signup is copied into `growth_partner_accounts` once. Later partner
visits do not change that account's credit. Existing first-touch Conversions
reporting remains separate. Partners shows aggregate browser visits, demo use,
downloads and distinct accounts/trials/first real reminders/paid subscriptions
for 28- or 90-day signup/touch cohorts. Existing live billing and SMS milestones
provide those business outcomes. A link association is not proof of causation.
Opt-out, bot, logged-in browsing and retention rules remain in force. Browser
attribution history expires after 90 days; partner definitions remain saved.
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
