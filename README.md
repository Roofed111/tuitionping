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
