"""The reminder engine — the heart of TuitionPing.

run_reminders() is meant to run once a day (Railway cron job, see README).
For each family it figures out where today falls relative to their tuition due
date and fires the matching stage:

    -3 days  -> "before" reminder   ("tuition is due in 3 days")
     due date -> "due" reminder     ("tuition is due today")
    +3 days  -> "late3" nudge       (only if that period is still unpaid)
    +7 days  -> "late7" nudge       (only if that period is still unpaid)
The reminder_log table guarantees each (family, period, stage) fires exactly
once — re-running the engine never double-texts anyone.

Set TUITION_PING_TODAY=YYYY-MM-DD to pretend it is a different date (used by the
automated test, and handy for demos).
"""
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from store import (all_providers, all_families, get_templates,
                   get_sending_settings, get_owner_phone, digest_sent_today,
                   mark_digest_sent, last_digest_sent_at, count_unreported_suggestions,
                   mark_suggestions_reported, location_payment_urls,
                   location_late_fees, apply_late_fee, get_late_fee,
                   family_language, family_phones, is_snoozed,
                   template_key, effective_tuition, outstanding_charges_total,
                   mark_reminder_sent, reminder_already_sent,
                   child_display_name, birthday_sent_this_year,
                   mark_birthday_sent, get_absences_for_date,
                   families_with_docs_due, immunization_notice_sent,
                   mark_immunization_notice)
from sms import send_sms

# (stage name, days until the due date: +3 = three days before it's due,
#  0 = due today, -3 / -7 = three / seven days overdue)
STAGE_LABELS = {
    "before": "due in 3 days",
    "due": "due today",
    "late3": "3 days overdue",
    "late7": "7 days overdue",
}


def pending_reminders(provider_id) -> list[dict]:
    """Tuition texts due today that haven't gone out yet.

    Powers the dashboard 'Today's reminders' panel. Mirrors _remind_family's
    due-detection exactly (same stages, same skip rules) without sending
    anything. Each item: {family, stage, stage_label, due}.
    """
    pday = provider_local_now(provider_id).date()
    periods = [shift_month(period_of(pday), d) for d in (-1, 0, 1)]
    pending: list[dict] = []
    for family in all_families(provider_id):
        try:
            if family["opted_out"] or is_snoozed(family, pday.isoformat()):
                continue
            # Same open-period guard as the send engine: a period from before
            # the child was enrolled must never show as a pending reminder.
            open_p = open_period(family, pday)
            for period in periods:
                if period != open_p:
                    continue
                due = due_date_for_family(family, period)
                delta = (due - pday).days
                for stage, offset in STAGES:
                    if delta != offset:
                        continue
                    if stage in LATE_STAGES and family["paid_period"] == period:
                        continue
                    if reminder_already_sent(family["id"], period, stage):
                        continue
                    pending.append({
                        "family": family["name"],
                        "stage": stage,
                        "stage_label": STAGE_LABELS[stage],
                        "due": due.isoformat(),
                    })
        except Exception:
            continue
    return pending
STAGES = (("before", 3), ("due", 0), ("late3", -3), ("late7", -7))
LATE_STAGES = ("late3", "late7")


def today() -> date:
    override = os.getenv("TUITION_PING_TODAY", "")
    if override:
        y, m, d = map(int, override.split("-"))
        return date(y, m, d)
    return date.today()


def period_of(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def shift_month(period: str, delta: int) -> str:
    """'2026-09' shifted by delta months -> '2026-10' etc."""
    y, m = map(int, period.split("-"))
    m += delta
    while m < 1:
        m += 12
        y -= 1
    while m > 12:
        m -= 12
        y += 1
    return f"{y:04d}-{m:02d}"


def due_date_for(period: str, due_day: int) -> date:
    """The calendar date tuition is due for a period. due_day is the day of the
    month (1-31); in short months it clamps to the last day (e.g. due day 31
    in February -> Feb 28)."""
    import calendar
    y, m = map(int, period.split("-"))
    last = calendar.monthrange(y, m)[1]
    return date(y, m, min(due_day, last))


def _override_date(family) -> date | None:
    """One-time 'next bill due' date for mid-cycle signups, if one is set."""
    try:
        raw = family["next_due_date"]
    except (KeyError, IndexError, TypeError):
        return None
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def due_date_for_family(family, period: str) -> date:
    """Due date for a period, honoring a one-time next-bill override.

    The override replaces the monthly cadence for exactly one billing cycle —
    the period containing the override date — then the engine clears it.
    """
    ov = _override_date(family)
    if ov and period_of(ov) == period:
        return ov
    return due_date_for(period, family["due_day"])


def _expire_override(family, day: date):
    """Clear a next-bill override once it has served its purpose: the bill
    was paid, or all four stages (through +7 days late) had their chance."""
    from store import clear_next_due_date
    ov = _override_date(family)
    if not ov:
        return
    if family["paid_period"] == period_of(ov) or ov < day - timedelta(days=7):
        clear_next_due_date(family["id"])


def open_period(family, day: date, skip_paid: bool = True) -> str:
    """The tuition period the family currently owes for.

    Normally that's the period of the most recent due date on or before today —
    unless that period is already paid, in which case it's the next one.
    (Paying early for next month must not make this month look unpaid, and an
    old unpaid month stays "open" until it is actually paid.)

    A future one-time "next bill due" override replaces the monthly cadence:
    the provider said explicitly when the next bill is due, so the family
    owes for the override's period.

    A family can never owe for a period whose due date passed before they were
    added — a family created today must not show up as "Late (28d)".

    skip_paid=False returns the period of the most recent due date regardless
    of payment; family_status uses it to decide whether to show "Paid".
    """
    ov = _override_date(family)
    if ov is not None and ov >= day:
        candidate = period_of(ov)
    elif day.day >= family["due_day"]:
        candidate = period_of(day)
    else:
        candidate = shift_month(period_of(day), -1)
    created = family["created_at"][:10]  # "YYYY-MM-DD" prefix of the ISO timestamp
    while due_date_for_family(family, candidate).isoformat() < created:
        candidate = shift_month(candidate, +1)
    if skip_paid and family["paid_period"] == candidate:
        return shift_month(candidate, +1)
    return candidate


def family_status(family, day: date) -> dict:
    """Human-friendly status for the dashboard: Paid / Reported paid /
    Due in N days / Due today / Late (N days) / Opted out.

    A parent texting PAID is self-reported, not verified — it shows as
    "Reported paid" so the provider knows to confirm it in their own
    payment records. Only a manual "Mark paid" shows as "Paid"."""
    if family["opted_out"]:
        return {"label": "Opted out", "kind": "muted"}
    # "Paid" is judged against the most recent due period even if it is paid
    # (open_period skips paid periods, so it can never report "Paid" itself).
    if family["paid_period"] == open_period(family, day, skip_paid=False):
        try:
            src = family["paid_source"] or ""
        except (KeyError, IndexError, TypeError):
            src = ""
        if src == "reply":
            return {"label": "Reported paid", "kind": "paid",
                    "title": "Parent replied PAID — self-reported, not verified. "
                             "Confirm it arrived in your payment account."}
        return {"label": "Paid", "kind": "paid"}
    period = open_period(family, day)
    due = due_date_for_family(family, period)
    delta = (due - day).days
    if delta > 0:
        return {"label": f"Due in {delta}d", "kind": "upcoming"}
    if delta == 0:
        return {"label": "Due today", "kind": "due"}
    return {"label": f"Late ({-delta}d)", "kind": "late"}


def render_template(template: str, family, location_name: str, due: date,
                  pay_url: str = "", fee: float = 0.0, lang: str = "en",
                  extra: float = 0.0) -> str:
    pay_link = f"Pay online: {pay_url}" if pay_url else ""
    total = effective_tuition(family) + float(fee or 0) + float(extra or 0)
    body = template.format(
        name=family["name"].split()[0],          # first name feels personal
        amount=f"{total:,.2f}",
        location=location_name,
        due_date=due.strftime("%b %d"),
        pay_link=pay_link,
    )
    if fee and fee > 0:
        note = (f"Incluye un recargo de ${fee:,.2f}." if lang == "es"
                else f"Includes a ${fee:,.2f} late fee.")
        body = f"{body} {note}"
    if extra and extra > 0:
        note = (f"Incluye ${extra:,.2f} en cargos adicionales." if lang == "es"
                else f"Includes ${extra:,.2f} in extra charges.")
        body = f"{body} {note}"
    return " ".join(body.split())  # collapse the gap when there's no pay link


def provider_local_now(provider_id) -> datetime:
    """Current time in the provider's own timezone (falls back to PT)."""
    tz_name = get_sending_settings(provider_id)["timezone"]
    try:
        return datetime.now(ZoneInfo(tz_name))
    except Exception:
        return datetime.now(ZoneInfo("America/Los_Angeles"))


def in_quiet_hours(provider_id) -> bool:
    """True when texts should NOT go out right now for this provider."""
    s = get_sending_settings(provider_id)
    now = provider_local_now(provider_id).strftime("%H:%M")
    start, end = s["quiet_start"], s["quiet_end"]
    if start == end:
        return False
    if start < end:          # e.g. 07:00–21:00
        return not (start <= now < end)
    return not (now >= start or now < end)   # window wraps midnight


def cash_forecast(provider_id, day: date, days_ahead: int = 7):
    """Tuition expected from unpaid families: overdue now + due within
    days_ahead. Returns (total_dollars, family_count). Skips opted-out,
    snoozed, and paid-up families."""
    total, count = 0.0, 0
    day_iso = day.isoformat()
    for family in all_families(provider_id):
        if family["opted_out"] or is_snoozed(family, day_iso):
            continue
        if family["paid_period"] == open_period(family, day, skip_paid=False):
            continue  # paid up — nothing expected
        period = open_period(family, day)
        due = due_date_for_family(family, period)
        if (due - day).days > days_ahead:
            continue
        total += (effective_tuition(family) + get_late_fee(family["id"], period)
                  + outstanding_charges_total(family["id"]))
        count += 1
    return total, count


def _maybe_send_owner_digest(provider, pday, sent, quiet=False):
    """One morning text per day to the owner's own phone: families still unpaid,
    plus a nudge if customers submitted new feature suggestions since the last
    digest. Only when the owner set their number in Settings, only at/after 9am
    local, only outside quiet hours, and only if there's something to say."""
    if quiet:
        return
    owner_phone = get_owner_phone(provider["id"])
    if not owner_phone:
        return
    day_iso = pday.isoformat()
    if digest_sent_today(provider["id"], day_iso):
        return
    if provider_local_now(provider["id"]).hour < 9:
        return  # mornings only — the cron runs hourly
    late = []
    for family in all_families(provider["id"]):
        if family["opted_out"] or is_snoozed(family, day_iso):
            continue
        if family_status(family, pday)["kind"] != "late":
            continue
        due = due_date_for_family(family, open_period(family, pday))
        late.append((family, (pday - due).days))
    last_sent = last_digest_sent_at(provider["id"])
    new_sugg = count_unreported_suggestions(provider["id"])
    absences = get_absences_for_date(provider["id"], day_iso)
    docs_due = families_with_docs_due(provider["id"], day_iso)
    docs_expired = [f for f, d in docs_due if d <= 0 and not f["opted_out"]]
    if not late and not new_sugg and not absences and not docs_expired:
        return
    parts = []
    if docs_due:
        entries = []
        for f, d in sorted(docs_due, key=lambda x: x[1])[:4]:
            kid = child_display_name(f, "en")
            entries.append(f"{kid} (expired)" if d <= 0 else
                           f"{kid} (expires {date.fromisoformat(str(f['immunization_expires'])[:10]).strftime('%b %d')})")
        if len(docs_due) > 4:
            entries.append(f"+{len(docs_due) - 4} more")
        parts.append("\U0001f4cb immunization records: " + ", ".join(entries))
    if absences:
        names = [child_display_name(row, "en") for row in absences[:4]]
        if len(absences) > 4:
            names.append(f"+{len(absences) - 4} more")
        parts.append(f"\U0001f937 absent today: " + ", ".join(names))
    if late:
        n = len(late)
        total = sum(effective_tuition(f) + outstanding_charges_total(f["id"])
                    for f, _ in late)
        entries = [f"{f['name']} (${effective_tuition(f):,.0f}, {d}d late)"
                   for f, d in late[:4]]
        if len(late) > 4:
            entries.append(f"+{len(late) - 4} more")
        parts.append(f"{n} {'family' if n == 1 else 'families'} still unpaid "
                     f"(${total:,.0f}): " + "; ".join(entries))
    if new_sugg:
        parts.append(f"\U0001f4a1 {new_sugg} new feature "
                     f"{'suggestion' if new_sugg == 1 else 'suggestions'}"
                     " — see Admin \u2192 Suggestions")
    forecast_total, forecast_n = cash_forecast(provider["id"], pday)
    if forecast_total > 0:
        parts.append(f"\U0001f4b0 ${forecast_total:,.0f} expected from "
                     f"{forecast_n} {'family' if forecast_n == 1 else 'families'}"
                     f" in the next 7 days")
    body = "TuitionPing: " + " · ".join(parts)
    send_sms(owner_phone, body, provider["id"], None)
    mark_digest_sent(provider["id"], day_iso)
    if new_sugg:
        mark_suggestions_reported(provider["id"])
    sent.append({"family": f"{len(late)} families (owner digest)", "phone": owner_phone,
                 "period": period_of(pday), "stage": "owner_digest"})


def _maybe_send_immunization(provider, pday, sent, quiet=False):
    """Immunization-record compliance texts. Warn once when the record is
    30 days (or less) from expiring, and once when it has expired.
    Deferred during quiet hours (not marked sent, so a later run picks it up).
    Updating the expiry date restarts the cycle for that family."""
    if quiet:
        return
    try:
        company = (provider["company"] or "").strip()
    except (KeyError, IndexError, TypeError):
        company = ""
    sig = f" — {company}" if company else ""
    for family, delta in families_with_docs_due(provider["id"], pday.isoformat()):
        if family["opted_out"]:
            continue
        kind = "warn" if delta > 0 else "expired"
        if immunization_notice_sent(family["id"], kind):
            continue
        lang = family_language(family)
        kid = child_display_name(family, lang)
        try:
            exp_display = date.fromisoformat(
                str(family["immunization_expires"])[:10]).strftime("%b %d")
        except (ValueError, KeyError, IndexError, TypeError):
            exp_display = ""
        if kind == "warn":
            text = (f"Aviso: el registro de vacunación de {kid} vence el "
                    f"{exp_display}. Por favor envíenos la copia actualizada "
                    f"antes de esa fecha{sig}."
                    if lang == "es" else
                    f"Heads up: {kid}'s immunization record expires on "
                    f"{exp_display}. Please send us the updated copy before "
                    f"then{sig}.")
        else:
            text = (f"El registro de vacunación de {kid} venció el "
                    f"{exp_display}. Por favor envíenos la copia actualizada "
                    f"lo antes posible{sig}."
                    if lang == "es" else
                    f"{kid}'s immunization record expired on {exp_display}. "
                    f"Please send us the updated copy as soon as possible{sig}.")
        for phone in family_phones(family):
            send_sms(phone, text, provider["id"], family["id"])
        mark_immunization_notice(family["id"], kind)
        sent.append({"family": family["name"], "phone": family["phone"],
                     "period": pday.isoformat(), "stage": f"immunization_{kind}"})


def _suspended(provider) -> bool:
    try:
        return bool(provider["suspended"])
    except (KeyError, IndexError, TypeError):
        return False


def _maybe_send_birthdays(provider, pday, sent, quiet=False):
    """One 🎂 text per family per year when the child's birthday is today.
    Deferred during quiet hours (not marked sent, so a later run picks it up)."""
    if quiet:
        return
    mmdd = pday.strftime("%m-%d")
    try:
        company = (provider["company"] or "").strip()
    except (KeyError, IndexError, TypeError):
        company = ""
    for family in all_families(provider["id"]):
        try:
            bday = (family["child_birthday"] or "").strip()
        except (KeyError, IndexError, TypeError):
            bday = ""
        if not bday or bday != mmdd:
            continue
        if family["opted_out"] or birthday_sent_this_year(family["id"], pday.year):
            continue
        lang = family_language(family)
        kid = child_display_name(family, lang)
        co = f" at {company}" if company else ""
        co_es = f" de {company}" if company else ""
        text = (f"\U0001f382 ¡Feliz cumpleaños, {kid}! De parte de todos"
                f"{co_es} \U0001f389"
                if lang == "es" else
                f"\U0001f382 Happy birthday, {kid}! From all of us{co} \U0001f389")
        for phone in family_phones(family):
            send_sms(phone, text, provider["id"], family["id"])
        mark_birthday_sent(family["id"], pday.year)
        sent.append({"family": family["name"], "phone": family["phone"],
                     "period": pday.isoformat(), "stage": "birthday"})


def run_reminders(day: date | None = None, provider_id: int | None = None) -> list[dict]:
    """Send every reminder that is due on `day`. Returns what was sent.

    provider_id scopes the run to one provider (the dashboard button);
    without it, every provider is processed (the cron).

    Each provider runs on their OWN local calendar date (from their timezone
    setting) — unless the caller pins a day (tests, manual runs) or
    TUITION_PING_TODAY is set. A provider whose local date differs from the
    server's still gets the right stages.

    Deferred-for-quiet-hours reminders are NOT marked sent, so a later run
    the same local day — after the window opens — picks them up. The cron
    runs hourly for exactly this reason.
    """
    # Pinned when the caller passes a day explicitly or the test override is
    # set; otherwise every provider uses their own local date below.
    pinned = day is not None or bool(os.getenv("TUITION_PING_TODAY"))
    shared_day = day or today()
    sent: list[dict] = []

    providers = [p for p in all_providers()
                 if (provider_id is None or p["id"] == provider_id)
                 and not _suspended(p)]
    for provider in providers:
        pday = shared_day if pinned else provider_local_now(provider["id"]).date()
        # Look at last month, this month, next month so late nudges for a due
        # date early in the month (e.g. Sep 27 + 7 = Oct 4) are not missed.
        periods = [shift_month(period_of(pday), d) for d in (-1, 0, 1)]
        templates = get_templates(provider["id"])
        pay_urls = location_payment_urls(provider["id"])
        fee_cfg = location_late_fees(provider["id"])
        quiet = in_quiet_hours(provider["id"])
        for family in all_families(provider["id"]):
            try:
                _expire_override(family, pday)
                _remind_family(family, provider["id"], templates, pday, periods, sent, pay_urls, quiet, fee_cfg)
            except Exception as exc:  # one bad family must never kill the run
                print(f"[reminders] family {family['id']} failed: {exc!r}", flush=True)
                sent.append({"family": family["name"], "phone": family["phone"],
                             "error": str(exc)})
        try:
            _maybe_send_owner_digest(provider, pday, sent, quiet)
        except Exception as exc:
            print(f"[reminders] owner digest failed: {exc!r}", flush=True)
        try:
            _maybe_send_birthdays(provider, pday, sent, quiet)
        except Exception as exc:
            print(f"[reminders] birthdays failed: {exc!r}", flush=True)
        try:
            _maybe_send_immunization(provider, pday, sent, quiet)
        except Exception as exc:
            print(f"[reminders] immunization failed: {exc!r}", flush=True)
    return sent


def _remind_family(family, provider_id, templates, day, periods, sent,
                   pay_urls=None, quiet=False, fee_cfg=None):
    pay_urls = pay_urls or {}
    fee_cfg = fee_cfg or {}
    """All reminder logic for a single family (may raise; caller catches)."""
    if family["opted_out"]:
        return  # STOP was honored — never text them again
    if is_snoozed(family, day.isoformat()):
        return  # provider paused this family until a later date
    lang = family_language(family)
    # Only the family's currently-open period can trigger reminders. Without
    # this, a child added mid-month with a later due day would get "past due"
    # texts for last month's period — a bill from before they were enrolled.
    open_p = open_period(family, day)
    for period in periods:
        if period != open_p:
            continue
        due = due_date_for_family(family, period)
        delta = (due - day).days
        for stage, offset in STAGES:
            if delta != offset:
                continue
            # Late nudges only make sense while the period is unpaid.
            if stage in LATE_STAGES and family["paid_period"] == period:
                continue
            if reminder_already_sent(family["id"], period, stage):
                continue
            # Automatic late fee: once the grace period passes, add the
            # location's configured fee to what this family owes (ledger
            # action — happens even if the text itself is deferred).
            fee = 0.0
            if delta < 0:
                cfg_amount, cfg_days = fee_cfg.get(family["location_id"], (0.0, 0))
                if cfg_amount > 0 and cfg_days > 0 and -delta >= cfg_days:
                    if apply_late_fee(family["id"], period, cfg_amount):
                        print(f"[reminders] late fee ${cfg_amount:,.2f} applied:"
                              f" family {family['id']} period {period}", flush=True)
                fee = get_late_fee(family["id"], period)
            if quiet:
                # Due, but held: don't send now, don't mark sent — the next
                # run inside sending hours will pick it up.
                sent.append({"family": family["name"], "phone": family["phone"],
                             "period": period, "stage": stage,
                             "deferred": "quiet hours"})
                continue
            body = render_template(templates[template_key(stage, lang)],
                                   family, family["location_name"], due,
                                   pay_urls.get(family["location_id"], ""),
                                   fee=fee, lang=lang,
                                   extra=outstanding_charges_total(family["id"]))
            phones = family_phones(family)
            for phone in phones:
                send_sms(phone, body, provider_id, family["id"])
            mark_reminder_sent(family["id"], period, stage)
            sent.append({"family": family["name"], "phone": family["phone"],
                         "phones": len(phones),
                         "period": period, "stage": stage})
