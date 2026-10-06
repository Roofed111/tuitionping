"""Billing.

DEMO_MODE (default ON): Stripe is mocked. The "Subscribe" button on the
/billing page instantly activates the plan with a 30-day trial — no keys,
no cards, no network calls.

Real mode: set DEMO_MODE=0 and STRIPE_SECRET_KEY + STRIPE_WEBHOOK_SECRET.
Subscribing redirects to Stripe Checkout (30-day trial, card collected but
not charged until trial ends). Webhook events from Stripe activate and sync
subscriptions via POST /webhooks/stripe.

Founding members: the first FOUNDING_SPOTS providers to subscribe get
FOUNDING_PCT_OFF% off for FOUNDING_MONTHS months. In real mode this is
delivered via a Stripe coupon whose ID is in FOUNDING_COUPON_ID
(create it in Stripe at launch: 50% off, repeating, 6 months).
"""
import os
from datetime import datetime, timezone

from store import get_subscription, set_subscription, set_stripe_ids
from store import claim_founding_spot as _claim_spot
from store import founding_claimed_count as _claimed_count
from store import is_founding as _is_founding
from store import set_founding as _set_founding

DEMO_MODE = os.getenv("DEMO_MODE", "1") == "1"
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

PLANS = {
    "micro":     {"name": "Micro",      "price": 19, "families": 10,  "blurb": "Up to 10 families · 1 location"},
    "starter":   {"name": "Starter",    "price": 29, "families": 30,  "blurb": "Up to 30 families · 1 location"},
    "growth":    {"name": "Growth",     "price": 59, "families": 75,  "blurb": "Up to 75 families · unlimited locations"},
    "multisite": {"name": "Multi-site", "price": 99, "families": 200, "blurb": "Up to 200 families · unlimited locations"},
}

PLAN_ORDER = ["micro", "starter", "growth", "multisite"]


def plan_family_limit(plan: str) -> int:
    """Max families allowed on a plan (unknown plans fall back to starter)."""
    return PLANS.get(plan, PLANS["starter"])["families"]


def plan_location_limit(plan: str):
    """Max locations allowed on a plan; None means unlimited."""
    key = plan if plan in PLANS else "starter"
    return {"micro": 1, "starter": 1}.get(key)


def next_plan(plan: str) -> str | None:
    """The next tier up from plan, or None when already on the largest tier."""
    try:
        i = PLAN_ORDER.index(plan)
    except ValueError:
        return "starter"
    return PLAN_ORDER[i + 1] if i + 1 < len(PLAN_ORDER) else None

TRIAL_DAYS = 30
CYCLES = ("monthly", "annual")

# Founding-member program: first N subscribers get P% off for M months.
FOUNDING_SPOTS = 25
FOUNDING_PCT_OFF = 50
FOUNDING_MONTHS = 6
FOUNDING_COUPON_ID = os.getenv("FOUNDING_COUPON_ID", "")
# Postcard campaign: 10% off for 3 months, auto-applied when the customer
# arrived via the /postcard QR landing page. Stripe coupon b9rSblgJ
# ("Postcard 10% off 3 months", percent_off=10, duration=repeating,
# duration_in_months=3), created Oct 5, 2026. (Replaces UO2hwmNA, which was
# 10% off the first invoice only.)
POSTCARD_COUPON_ID = os.getenv("POSTCARD_COUPON_ID", "b9rSblgJ")

_stripe = None
_price_cache = {}


def stripe_configured():
    return bool(STRIPE_SECRET_KEY) and not DEMO_MODE


def _stripe_lib():
    global _stripe
    if _stripe is None:
        import stripe
        stripe.api_key = STRIPE_SECRET_KEY
        _stripe = stripe
    return _stripe


def price_id(plan: str, cycle: str = "monthly") -> str:
    """Resolve a Stripe Price ID from its lookup key (cached)."""
    key = (plan, cycle)
    if key not in _price_cache:
        s = _stripe_lib()
        lookup = f"tuitionping_{plan}_{cycle}"
        prices = s.Price.list(lookup_keys=[lookup], limit=1)
        if not prices.data:
            raise ValueError(f"no Stripe price with lookup key {lookup}")
        _price_cache[key] = prices.data[0].id
    return _price_cache[key]


def activate_demo_subscription(provider_id: int, plan: str) -> dict:
    """What the demo Subscribe button does: start the 30-day trial instantly,
    claiming a founding spot when any remain."""
    if plan not in PLANS:
        raise ValueError(f"unknown plan: {plan}")
    trial_ends = (datetime.now(timezone.utc).timestamp() + TRIAL_DAYS * 86400)
    trial_ends_iso = datetime.fromtimestamp(trial_ends, timezone.utc).isoformat()
    set_subscription(provider_id, plan, "trialing", trial_ends_at=trial_ends_iso)
    founding = _claim_spot(provider_id, FOUNDING_SPOTS)
    return {"plan": plan, "status": "trialing", "trial_ends_at": trial_ends_iso,
            "founding": founding}


def founding_spots_left() -> int:
    return max(0, FOUNDING_SPOTS - _claimed_count())


def founding_price(plan: str, cycle: str = "monthly") -> float:
    """What a founding member pays for the first FOUNDING_MONTHS months."""
    base = PLANS[plan]["price"]
    if cycle == "annual":
        base *= 10
    return round(base * (100 - FOUNDING_PCT_OFF) / 100, 2)


def subscription_summary(provider_id: int) -> dict:
    sub = get_subscription(provider_id)
    if not sub:
        return {"plan": None, "status": "none", "founding": False}
    return {"plan": sub["plan"], "status": sub["status"],
            "trial_ends_at": sub["trial_ends_at"],
            "founding": _is_founding(provider_id)}


def sync_subscription_from_stripe(provider_id: int):
    """Pull the provider's latest Stripe subscription state straight from the
    API. Used when they return from Checkout so the page reflects reality
    even if the webhook hasn't arrived yet."""
    from store import ensure_stripe_columns
    ensure_stripe_columns()
    sub = get_subscription(provider_id)
    try:
        customer_id = sub["stripe_customer_id"] if sub else None
    except (KeyError, IndexError, TypeError):
        customer_id = None
    if not customer_id:
        return None
    s = _stripe_lib()
    subs = _as_dict(s.Subscription.list(customer=customer_id, limit=1))
    data = subs.get("data") or []
    if not data:
        return None
    _sync_from_subscription(provider_id, data[0])
    return subscription_summary(provider_id)


def cancel_provider_billing(provider_id: int) -> dict:
    """Cancel a provider's live Stripe subscription before their local data
    is deleted. Returns {"ok": True} when there is nothing to cancel or the
    cancellation succeeded, {"ok": False, "error": ...} when Stripe could not
    be reached or refused — in which case the caller must NOT delete locally,
    or the customer keeps getting billed with no account to manage it."""
    from store import ensure_stripe_columns, get_subscription
    ensure_stripe_columns()
    sub = get_subscription(provider_id)
    try:
        sub_id = sub["stripe_subscription_id"] if sub else None
    except (KeyError, IndexError, TypeError):
        sub_id = None
    if not sub_id:
        return {"ok": True, "canceled": False}
    if not stripe_configured():
        return {"ok": False,
                "error": "a Stripe subscription is on file but Stripe is not "
                         "configured — refusing to delete until billing is resolved"}
    s = _stripe_lib()
    try:
        current = _as_dict(s.Subscription.retrieve(sub_id))
        if (current.get("status") or "") != "canceled":
            s.Subscription.delete(sub_id)
    except Exception as e:
        # A stale ID pointing at a subscription Stripe no longer knows is
        # safe to treat as already gone; anything else is a real failure.
        if "No such subscription" not in str(e):
            return {"ok": False, "error": f"Stripe cancellation failed: {e}"}
    return {"ok": True, "canceled": True}


def _get_or_create_customer(provider: dict) -> str:
    """Return the Stripe customer ID for this provider, creating one if needed."""
    sub = get_subscription(provider["id"])
    if sub and sub["stripe_customer_id"]:
        return sub["stripe_customer_id"]
    s = _stripe_lib()
    customer = s.Customer.create(
        email=provider["email"],
        name=provider["name"] or "",
        metadata={"provider_id": str(provider["id"])},
    )
    set_stripe_ids(provider["id"], customer_id=customer.id)
    return customer.id


def create_portal_session(provider: dict, base_url: str) -> str:
    """Create a Stripe Customer Portal session so the provider can self-serve:
    update card, view invoices, or cancel. Returns the redirect URL."""
    s = _stripe_lib()
    customer_id = _get_or_create_customer(provider)
    session = s.billing_portal.Session.create(
        customer=customer_id,
        return_url=f"{base_url}/billing",
    )
    return session.url


def create_checkout_session(provider: dict, plan: str, cycle: str, base_url: str,
                            postcard: bool = False) -> str:
    """Create a Stripe Checkout session for a plan; returns the redirect URL.

    Discount priority: founding members (50% off 6mo) first, then the postcard
    campaign (10% off for 3 months). Everyone else can type a promo code."""
    if plan not in PLANS:
        raise ValueError(f"unknown plan: {plan}")
    if cycle not in CYCLES:
        cycle = "monthly"
    s = _stripe_lib()
    customer_id = _get_or_create_customer(provider)
    founding = _claim_spot(provider["id"], FOUNDING_SPOTS)
    source = "founding" if founding else ("postcard" if postcard else "direct")
    session_args = dict(
        customer=customer_id,
        mode="subscription",
        line_items=[{"price": price_id(plan, cycle), "quantity": 1}],
        subscription_data={
            "trial_period_days": TRIAL_DAYS,
            "metadata": {"provider_id": str(provider["id"]), "plan": plan,
                         "founding": "1" if founding else "0", "source": source},
        },
        success_url=f"{base_url}/billing?checkout=success",
        cancel_url=f"{base_url}/billing?checkout=cancelled",
        allow_promotion_codes=True,
    )
    if founding and FOUNDING_COUPON_ID:
        # Founding members get 50% off for 6 months via the launch coupon.
        # Stripe forbids allow_promotion_codes alongside discounts entirely
        # (even set to False), so drop the key rather than disabling it.
        session_args["discounts"] = [{"coupon": FOUNDING_COUPON_ID}]
        session_args.pop("allow_promotion_codes", None)
    elif postcard and POSTCARD_COUPON_ID:
        # Postcard campaign: 10% off for 3 months, auto-applied.
        session_args["discounts"] = [{"coupon": POSTCARD_COUPON_ID}]
        session_args.pop("allow_promotion_codes", None)
    session = s.checkout.Session.create(**session_args)
    return session.url


def _as_dict(obj):
    """Stripe API objects aren't real dicts in recent stripe-python versions
    (.get() raises); normalize to a plain dict at the boundary."""
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if isinstance(obj, dict):
        return obj
    return dict(obj)


def _sync_from_subscription(provider_id: int, sub) -> None:
    """Write a Stripe Subscription object's state into our subscriptions table."""
    sub = _as_dict(sub)
    meta = _as_dict(sub.get("metadata") or {})
    plan = meta.get("plan") or _plan_from_price(sub)
    status = {"trialing": "trialing", "active": "active",
              "past_due": "past_due", "canceled": "canceled",
              "unpaid": "past_due"}.get(sub.get("status"), sub.get("status"))
    trial_ends = None
    if sub.get("trial_end"):
        trial_ends = datetime.fromtimestamp(sub["trial_end"], timezone.utc).isoformat()
    set_subscription(provider_id, plan or "starter", status, trial_ends_at=trial_ends)
    set_stripe_ids(provider_id, customer_id=sub.get("customer"),
                   subscription_id=sub.get("id"))
    try:
        import email_engagement
        email_engagement.note_subscription(provider_id, sub)
    except Exception:
        # Optional email preferences must never block billing synchronization.
        pass
    if meta.get("founding") in ("1", "0"):
        _set_founding(provider_id, meta.get("founding") == "1")
    # Existing subscription callbacks/return-sync also confirm conversions,
    # so measurement does not depend on adding new Stripe webhook event types.
    if sub.get("livemode") is True:
        import growth
        if status == "trialing":
            growth.milestone(provider_id, "trial_started")
        elif status == "active" and sub.get("latest_invoice") and growth.needs_milestone(provider_id, "paid_customer"):
            try:
                invoice = sub["latest_invoice"]
                if isinstance(invoice, str):
                    invoice = _stripe_lib().Invoice.retrieve(invoice)
                invoice = _as_dict(invoice)
                if (invoice.get("livemode") is True and invoice.get("status") == "paid"
                        and (invoice.get("amount_paid") or 0) > 0):
                    growth.milestone(provider_id, "paid_customer")
            except Exception:
                print("[analytics] paid invoice confirmation unavailable", flush=True)


def _plan_from_price(sub) -> str | None:
    sub = _as_dict(sub)
    try:
        items = _as_dict(sub.get("items") or {})
        data = items.get("data") or []
        price_id_ = _as_dict(data[0]).get("price", {})
        price_id_ = _as_dict(price_id_).get("id")
        s = _stripe_lib()
        price = _as_dict(s.Price.retrieve(price_id_))
        lk = price.get("lookup_key") or ""
        for plan in PLANS:
            if lk == f"tuitionping_{plan}_monthly" or lk == f"tuitionping_{plan}_annual":
                return plan
    except Exception:
        pass
    return None


def verify_webhook(payload: bytes, sig_header: str):
    s = _stripe_lib()
    return s.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)


def _maybe_grant_referral_reward(referee_id: int, sub: dict):
    """'Give a month, get a month': when a referred customer completes checkout,
    credit one month's plan price to both Stripe customer balances (applies to
    their next invoice, stacking with any coupons). Idempotent per referee."""
    from store import (get_referred_by, referral_reward_granted,
                       record_referral_reward, get_subscription)
    if referral_reward_granted(referee_id):
        return
    referrer_id = get_referred_by(referee_id)
    if not referrer_id:
        return
    s = _stripe_lib()
    sub = _as_dict(sub)

    def _sub_field(provider_id, field):
        row = get_subscription(provider_id)
        try:
            return row[field] if row else None
        except (KeyError, IndexError, TypeError):
            return None

    def credit_free_month(provider_id, customer_id):
        plan = _sub_field(provider_id, "plan") or "starter"
        cents = int(PLANS.get(plan, PLANS["starter"])["price"] * 100)
        s.Customer.create_balance_transaction(
            customer_id, amount=-cents, currency="usd",
            description="Referral reward — one free month of TuitionPing")

    referee_done = False
    try:
        if sub.get("customer"):
            credit_free_month(referee_id, sub["customer"])
            referee_done = True
    except Exception as exc:
        print(f"[referral] referee credit failed: {exc!r}", flush=True)
    if referee_done:
        try:
            if (_sub_field(referrer_id, "stripe_customer_id")
                    and _sub_field(referrer_id, "status") not in ("canceled", "none", None)):
                credit_free_month(referrer_id, _sub_field(referrer_id, "stripe_customer_id"))
        except Exception as exc:
            print(f"[referral] referrer credit failed: {exc!r}", flush=True)
        record_referral_reward(referrer_id, referee_id)
        print(f"[referral] free month granted: referrer={referrer_id}"
              f" referee={referee_id}", flush=True)


def handle_stripe_event(event) -> dict:
    """Apply a verified Stripe event to our subscription state."""
    event = _as_dict(event)
    etype = event.get("type", "unknown")
    print(f"[stripe webhook] {etype}", flush=True)
    s = _stripe_lib()

    if etype == "checkout.session.completed":
        session = _as_dict((event.get("data") or {}).get("object") or {})
        sess_meta = _as_dict(session.get("metadata") or {})
        provider_id = int(sess_meta.get("provider_id") or 0)
        sub_id = session.get("subscription")
        if sub_id:
            sub = _as_dict(s.Subscription.retrieve(sub_id))
            sub_meta = _as_dict(sub.get("metadata") or {})
            lid = int(sub_meta.get("provider_id") or provider_id or 0)
            if lid:
                _sync_from_subscription(lid, sub)
                _maybe_grant_referral_reward(lid, sub)
                if session.get("livemode") is True:
                    import growth
                    growth.milestone(lid, "checkout_completed")
                    if sub.get("status") == "trialing":
                        growth.milestone(lid, "trial_started")
        return {"received": True, "type": etype}

    if etype in ("customer.subscription.updated", "customer.subscription.deleted"):
        sub = _as_dict((event.get("data") or {}).get("object") or {})
        sub_meta = _as_dict(sub.get("metadata") or {})
        lid = int(sub_meta.get("provider_id") or 0)
        if not lid:
            # fall back: find provider by customer id
            from store import get_provider_by_stripe_customer
            provider = get_provider_by_stripe_customer(sub.get("customer"))
            lid = provider["id"] if provider else 0
        if lid:
            if etype == "customer.subscription.deleted":
                current = get_subscription(lid) or {}
                set_subscription(lid, current["plan"] if current else "starter", "canceled")
                set_stripe_ids(lid, subscription_id=sub.get("id"))
            else:
                _sync_from_subscription(lid, sub)
        return {"received": True, "type": etype}

    if etype in ("invoice.paid", "invoice.payment_succeeded"):
        inv = _as_dict((event.get("data") or {}).get("object") or {})
        if inv.get("livemode") is True and (inv.get("amount_paid") or 0) > 0:
            # Support both old and current Stripe invoice shapes.
            parent = _as_dict(inv.get("parent") or {})
            details = _as_dict(parent.get("subscription_details") or {})
            if inv.get("subscription") or details.get("subscription"):
                from store import get_provider_by_stripe_customer
                provider = get_provider_by_stripe_customer(inv.get("customer"))
                if provider:
                    import growth
                    growth.milestone(provider["id"], "paid_customer")
        return {"received": True, "type": etype}

    if etype == "invoice.payment_failed":
        # A card failed. Stripe usually follows with customer.subscription.updated
        # (status -> past_due), but sync immediately so the dashboard banner shows
        # without waiting. Service keeps running during past_due — the banner
        # points the owner at the Billing page to update their card.
        inv = _as_dict((event.get("data") or {}).get("object") or {})
        sub_id = inv.get("subscription")
        try:
            if sub_id:
                sub = _as_dict(s.Subscription.retrieve(sub_id))
                meta = _as_dict(sub.get("metadata") or {})
                lid = int(meta.get("provider_id") or 0)
                if not lid:
                    from store import get_provider_by_stripe_customer
                    provider = get_provider_by_stripe_customer(sub.get("customer"))
                    lid = provider["id"] if provider else 0
                if lid:
                    _sync_from_subscription(lid, sub)
        except Exception as exc:
            print(f"[stripe webhook] invoice.payment_failed sync failed: {exc!r}",
                  flush=True)
        return {"received": True, "type": etype}

    return {"received": True, "type": etype, "ignored": True}
