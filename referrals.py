"""Earn referral credits after a live, fully paid service month has elapsed.

The hourly reminder cron reconciles missed webhooks and delivers due credits.
Legacy checkout rewards are preserved; they never earn a second reward.
"""
import calendar
from datetime import datetime, timezone

import store


def now_ts():
    return int(datetime.now(timezone.utc).timestamp())


def ensure_tables():
    store.ensure_referral_columns()
    store.ensure_referral_rewards()
    store.ensure_stripe_columns()
    with store.db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS referral_progress (
            referee_id INTEGER PRIMARY KEY, referrer_id INTEGER NOT NULL,
            invoice_id TEXT NOT NULL DEFAULT '', subscription_id TEXT NOT NULL DEFAULT '',
            period_start BIGINT NOT NULL DEFAULT 0, eligible_at BIGINT NOT NULL DEFAULT 0,
            next_check_at BIGINT NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'waiting'
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS referral_credits (
            referee_id INTEGER NOT NULL, beneficiary_id INTEGER NOT NULL,
            customer_id TEXT NOT NULL, amount_cents INTEGER NOT NULL,
            transaction_id TEXT NOT NULL DEFAULT '', granted_at TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (referee_id, beneficiary_id)
        )""")


def _dict(value):
    import billing
    return billing._as_dict(value or {})


def _id(value):
    return value if isinstance(value, str) else _dict(value).get('id')


def subscription_id(invoice):
    details = _dict(_dict(invoice.get('parent')).get('subscription_details'))
    return _id(invoice.get('subscription') or details.get('subscription'))


def _next_month(timestamp):
    dt = datetime.fromtimestamp(timestamp, timezone.utc)
    year, month = (dt.year + 1, 1) if dt.month == 12 else (dt.year, dt.month + 1)
    return int(dt.replace(year=year, month=month,
                          day=min(dt.day, calendar.monthrange(year, month)[1])).timestamp())


def _qualifying_period(invoice, stripe):
    """Only full recurring TuitionPing periods count, never trials/prorations."""
    import billing
    if (invoice.get('livemode') is not True or invoice.get('status') != 'paid'
            or (invoice.get('amount_paid') or 0) <= 0
            or invoice.get('paid_out_of_band') or not invoice.get('id')
            or not subscription_id(invoice)
            or invoice.get('billing_reason') not in ('subscription_create', 'subscription_cycle')):
        return None
    lines = _dict(invoice.get('lines'))
    data = lines.get('data') or []
    if lines.get('has_more'):
        data = stripe.Invoice.list_lines(invoice['id'], limit=100).auto_paging_iter()
    periods = []
    for raw in data:
        line = _dict(raw)
        parent = _dict(line.get('parent'))
        details = _dict(parent.get('subscription_item_details'))
        if line.get('type') != 'subscription' and parent.get('type') != 'subscription_item_details':
            continue
        if line.get('proration') or details.get('proration') or (line.get('amount') or 0) <= 0:
            continue
        price = line.get('price') or _dict(_dict(line.get('pricing')).get('price_details')).get('price')
        price = _dict(stripe.Price.retrieve(price)) if isinstance(price, str) else _dict(price)
        lookup = price.get('lookup_key')
        if not billing.plan_from_lookup(lookup):
            continue
        recurring = _dict(price.get('recurring'))
        period = _dict(line.get('period'))
        start, end = period.get('start') or 0, period.get('end') or 0
        if not start or end < _next_month(start) or recurring.get('interval_count', 1) != 1:
            continue
        interval = recurring.get('interval')
        if interval not in ('month', 'year'):
            continue
        periods.append((start, end if interval == 'month' else _next_month(start)))
    return min(periods) if periods else None


def record_paid_invoice(provider_id, invoice):
    import billing
    referrer_id = store.get_referred_by(provider_id)
    if not referrer_id or referrer_id == provider_id or store.referral_reward_granted(provider_id):
        return
    invoice = _dict(invoice)
    sub = dict(store.get_subscription(provider_id) or {})
    if (sub.get('stripe_customer_id') != _id(invoice.get('customer'))
            or sub.get('stripe_subscription_id') != subscription_id(invoice)):
        return
    period = _qualifying_period(invoice, billing._stripe_lib())
    if not period:
        return
    ensure_tables()
    start, eligible = period
    with store.db() as conn:
        conn.execute("""INSERT INTO referral_progress
            (referee_id, referrer_id, invoice_id, subscription_id, period_start, eligible_at, state)
            VALUES (?,?,?,?,?,?,'pending') ON CONFLICT(referee_id) DO UPDATE SET
            invoice_id = excluded.invoice_id, subscription_id = excluded.subscription_id,
            period_start = excluded.period_start, eligible_at = excluded.eligible_at,
            next_check_at = 0, state = 'pending'
            WHERE (referral_progress.period_start = 0 OR excluded.period_start < referral_progress.period_start)
            AND referral_progress.state NOT IN ('complete', 'ineligible')
            AND NOT EXISTS (SELECT 1 FROM referral_credits WHERE referee_id = excluded.referee_id)
            """, (provider_id, referrer_id, invoice['id'], subscription_id(invoice), start, eligible))


def _payment_reversed(invoice, stripe):
    if (invoice.get('pre_payment_credit_notes_amount') or 0) > 0 or (invoice.get('post_payment_credit_notes_amount') or 0) > 0:
        return True
    charges = []
    if invoice.get('charge'):
        charges.append(_dict(stripe.Charge.retrieve(_id(invoice['charge']))))
    intents = [invoice['payment_intent']] if invoice.get('payment_intent') else []
    if not charges and not intents:
        # Current Stripe API moved payment references off the Invoice object.
        payments = stripe.InvoicePayment.list(invoice=invoice['id'], status='paid', limit=100)
        for raw in payments.auto_paging_iter():
            payment = _dict(_dict(raw).get('payment'))
            if payment.get('type') == 'payment_intent':
                intents.append(payment.get('payment_intent'))
            elif payment.get('type') == 'charge':
                charges.append(_dict(stripe.Charge.retrieve(_id(payment.get('charge')))))
    for intent in intents:
        obj = _dict(stripe.PaymentIntent.retrieve(_id(intent), expand=['latest_charge']))
        charge = obj.get('latest_charge')
        if isinstance(charge, str):
            charge = stripe.Charge.retrieve(charge)
        if charge:
            charges.append(_dict(charge))
    # Without a verifiable Stripe charge, keep the referral pending.
    if not charges:
        raise ValueError('Paid invoice has no verifiable Stripe charge')
    return any(c.get('refunded') or (c.get('amount_refunded') or 0) > 0 or c.get('disputed') for c in charges)


def _deliver_credit(referee_id, beneficiary_id, stripe):
    """Persist fixed request parameters; serialize delivery on each credit row."""
    import billing
    sub = dict(store.get_subscription(beneficiary_id) or {})
    customer = sub.get('stripe_customer_id')
    if not customer:
        return False
    plan = sub.get('plan')
    if plan not in billing.PLANS:
        return False
    with store.db() as conn:
        previous = conn.execute('SELECT amount_cents FROM referral_credits WHERE referee_id=? AND beneficiary_id=?', (referee_id,beneficiary_id)).fetchone()
    # Retried credits retain their original amount through any catalog change.
    amount = previous['amount_cents'] if previous else billing.monthly_credit_cents(sub,stripe)
    with store.db() as conn:
        conn.execute("""INSERT INTO referral_credits
            (referee_id, beneficiary_id, customer_id, amount_cents) VALUES (?,?,?,?)
            ON CONFLICT(referee_id, beneficiary_id) DO NOTHING""",
            (referee_id, beneficiary_id, customer, amount))
    with store.db() as conn:
        if not store.USE_PG:
            conn.execute('BEGIN IMMEDIATE')
        suffix = ' FOR UPDATE' if store.USE_PG else ''
        row = dict(conn.execute('SELECT * FROM referral_credits WHERE referee_id = ? AND beneficiary_id = ?' + suffix,
                                (referee_id, beneficiary_id)).fetchone())
        if row['transaction_id']:
            return True
        key = f'tp-referral-v2-{referee_id}-{beneficiary_id}'
        # Stripe forgets idempotency keys after 24 hours. Metadata reconciliation
        # recovers a successful request whose response or local commit was lost.
        transaction_id = None
        for raw in stripe.Customer.list_balance_transactions(row['customer_id'], limit=100).auto_paging_iter():
            transaction = _dict(raw)
            if _dict(transaction.get('metadata')).get('tuitionping_referral_credit') == key:
                if transaction.get('amount') != -row['amount_cents'] or transaction.get('currency') != 'usd':
                    raise ValueError('Referral transaction does not match persisted credit')
                transaction_id = transaction['id']
                break
        if not transaction_id:
            transaction = stripe.Customer.create_balance_transaction(
                row['customer_id'], amount=-row['amount_cents'], currency='usd',
                description='TuitionPing referral credit — one month after the first paid month',
                metadata={'tuitionping_referral_credit': key}, idempotency_key=key)
            transaction_id = _dict(transaction)['id']
        conn.execute('UPDATE referral_credits SET transaction_id = ?, granted_at = ? WHERE referee_id = ? AND beneficiary_id = ?',
                     (transaction_id, store.now_iso(), referee_id, beneficiary_id))
    return True


def run(limit=50):
    """Hourly cron: recover paid invoices, then issue credits after eligibility."""
    import billing
    if not billing.stripe_configured():
        return {'granted': 0, 'errors': 0, 'disabled': True}
    ensure_tables()
    timestamp = now_ts()
    with store.db() as conn:
        conn.execute("""INSERT INTO referral_progress (referee_id, referrer_id)
            SELECT p.id, p.referred_by FROM providers p
            JOIN subscriptions s ON s.provider_id = p.id
            WHERE p.referred_by IS NOT NULL AND p.referred_by <> p.id
            AND s.stripe_subscription_id IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM referral_rewards r WHERE r.referee_id = p.id)
            ON CONFLICT(referee_id) DO NOTHING""")
        rows = conn.execute("""SELECT * FROM referral_progress p
            WHERE p.state IN ('waiting', 'pending') AND p.next_check_at <= ?
            AND (p.eligible_at = 0 OR p.eligible_at <= ?)
            AND NOT EXISTS (SELECT 1 FROM referral_rewards r WHERE r.referee_id = p.referee_id)
            ORDER BY p.next_check_at, p.referee_id LIMIT ?""", (timestamp, timestamp, limit)).fetchall()
    stripe = billing._stripe_lib()
    result = {'granted': 0, 'errors': 0}
    for raw in rows:
        row = dict(raw)
        referee_id = row['referee_id']
        try:
            sub = dict(store.get_subscription(referee_id) or {})
            if not row['invoice_id']:
                invoices = stripe.Invoice.list(subscription=sub['stripe_subscription_id'], status='paid', limit=100)
                for invoice in invoices.auto_paging_iter():
                    record_paid_invoice(referee_id, invoice)
                with store.db() as conn:
                    row = dict(conn.execute('SELECT * FROM referral_progress WHERE referee_id = ?', (referee_id,)).fetchone())
            if not row['invoice_id'] or row['eligible_at'] > timestamp:
                continue
            invoice = _dict(stripe.Invoice.retrieve(row['invoice_id']))
            remote_sub = _dict(stripe.Subscription.retrieve(row['subscription_id']))
            period = _qualifying_period(invoice, stripe)
            ended = remote_sub.get('ended_at') or 0
            # canceled_at can be the date cancellation was requested, rather
            # than when service ended; use ended_at/cancel_at for service loss.
            canceled_early = bool(ended and ended < row['eligible_at'])
            if remote_sub.get('status') == 'canceled' and not ended:
                canceled_early = (remote_sub.get('cancel_at') or remote_sub.get('canceled_at') or 0) < row['eligible_at']
            if (remote_sub.get('livemode') is not True or _id(remote_sub.get('customer')) != sub.get('stripe_customer_id')
                    or period != (row['period_start'], row['eligible_at']) or canceled_early
                    or _payment_reversed(invoice, stripe)):
                with store.db() as conn:
                    conn.execute("UPDATE referral_progress SET state = 'ineligible' WHERE referee_id = ?", (referee_id,))
                continue
            # Both benefits are independently durable. A failed referrer credit
            # never loses the reward or duplicates the referee's credit.
            completed = []
            for beneficiary in (row['referrer_id'], referee_id):
                try:
                    completed.append(_deliver_credit(referee_id, beneficiary, stripe))
                except Exception:
                    completed.append(False)
                    result['errors'] += 1
                    print(f'[referral] credit delivery pending for referral {referee_id}', flush=True)
            if all(completed):
                store.record_referral_reward(row['referrer_id'], referee_id)
                with store.db() as conn:
                    conn.execute("UPDATE referral_progress SET state = 'complete' WHERE referee_id = ?", (referee_id,))
                result['granted'] += 1
        except Exception:
            result['errors'] += 1
            print(f'[referral] reconciliation pending for referral {referee_id}', flush=True)
        finally:
            with store.db() as conn:
                # Waiting accounts need only a daily missed-webhook check.
                delay = 86400 if row['state'] == 'waiting' else 3600
                conn.execute('UPDATE referral_progress SET next_check_at = ? WHERE referee_id = ?',
                             (timestamp + delay, referee_id))
    return result
