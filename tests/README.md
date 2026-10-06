# Late-fee calculator checks

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
