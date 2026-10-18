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
