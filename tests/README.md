# Late-fee calculator checks

## Verified payment statements

`test_payment_confirmation.py` checks that parent PAID reports are excluded from
private/public statements, annual collected totals, reminder recovery totals and
statement text recipients until a provider verifies receipt. It covers repeat and
stale confirmations, original amount/date preservation, later fees, historical
review, ownership/CSRF checks and migration from the old ledger schema. Legacy
records are preserved; only the family's explicitly confirmed current period is
backfilled as verified. Older records without evidence require individual review
from the annual report or family edit page. SMS is mocked and databases are temporary.

## Referral credit checks

`test_referrals.py` uses a temporary database and simulated Stripe responses.
It checks referral-link attribution, checkout/trial exclusion, first paid month
boundaries, annual and month-end dates, old/current Stripe invoice shapes,
missed and duplicate webhooks, refunds/disputes, early versus end-of-period
cancellation, independent credit retries, lost responses after 24 hours,
concurrent delivery, fixed retry amounts, legacy reward preservation,
dashboard pending/issued counts and the authenticated hourly cron integration.
No live Stripe transactions, customer accounts, SMS or emails are created.

The application remains a Python app. Node and jsdom are development-only;
no package.json is added, so Railway keeps its existing build selection.

From the repository root:

```sh
node --test tests/late-fee-math.test.cjs
python -m venv /tmp/tuitionping-test-env
/tmp/tuitionping-test-env/bin/pip install -r requirements.txt httpx
/tmp/tuitionping-test-env/bin/python -m unittest discover -s tests -p 'test_*.py'
npm install --prefix /tmp/tuitionping-test-ui jsdom@30.1.2
NODE_PATH=/tmp/tuitionping-test-ui/node_modules node --test tests/late-fee-ui.test.cjs
```

The math suite covers all five models, grace boundaries, caps, credits, cents,
DST/leap-day date arithmetic, invalid inputs and monotonic fee growth. The DOM
suite checks controls, invalid-state clearing, URL round trips, copying,
printing dispatch and CSV contents. The route suite uses a temporary database
and simulated providers to check public access, assets, metadata, sitemap and
referral logging without storing calculation query parameters.

These are logic checks, not browser visual tests. Before publishing, inspect
the page at desktop and narrow mobile widths, use the keyboard through the
form, and inspect the print preview. Check that comparison tables scroll within
their cards on mobile and that the tool is accessible without signing in.

Fee definitions are explicit: all days are calendar days, grace days are never
retroactively charged, weekly fees count started weeks, percentages apply to
unpaid tuition once, and credits are assumed to precede fee accrual. The tool
cannot reconstruct a dated history of partial payments or determine legal
applicability. It does not alter account fee rules or create invoices.

## Public resources and workbook

## Demo and acquisition conversions

The public `/demo` is a browser-only fictional roster. It has no account,
SMS-send or payment endpoint. Run its DOM workflow with:

```sh
NODE_PATH=/tmp/tuitionping-ui/node_modules node --test tests/demo-ui.test.cjs
```

`test_growth.py` checks public access, signed identity/token validation,
first-touch attribution, query exclusion, opt-out/bot filtering, per-account
deduplication, retention, admin access and live/test/zero-dollar billing
boundaries. Billing and Twilio are mocked; tests send no real messages and
create no external accounts or charges. Conversion reporting is at
`/admin/conversions`. Public campaign labels use `utm_source`, `utm_medium`,
and `utm_campaign`; the signup dropdown remains a separate self-report.
Business milestones start with newly attributed accounts, rather than
backfilling earlier customers. `invoice.paid`/`invoice.payment_succeeded`
are supported; existing subscription callbacks and billing return-sync
also confirm a positive paid invoice. No Stripe webhook settings change
is required. First scheduled tuition reminder records Twilio acceptance, not delivery.

The three audience pages have distinct workflows, examples and questions.
They are linked from the homepage/resources and included in the sitemap.

### Resource workbook checks

The Python suite also checks marketing redirects, auth/download indexing headers,
free access for expired subscribers, sitemap dates, resource links and ZIP contents.
For formula checks on the actual shipped spreadsheet (development dependencies only):

```sh
npm install --prefix /tmp/tuitionping-workbook-qa xlsx xlsx-calc
NODE_PATH=/tmp/tuitionping-workbook-qa/node_modules node --test tests/payment-tracker.test.cjs
```

To rebuild the downloads, install development-only `reportlab`, `python-docx` and
`openpyxl`, then run `python scripts/build_resource_downloads.py`. Shared policy and
message content lives in `content/provider-resources.json`. Keep the workbook
Google Sheets import check manual: the Worked example should show $190 and 7 days.
