"""Outbound SMS.

DEMO_MODE (default ON): nothing leaves this machine. Every "send" is printed
to the console and recorded in the message log as "sent (demo)" — so the whole
product can be tested end-to-end with zero credentials.

Real mode: set DEMO_MODE=0 plus TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and
TWILIO_FROM_NUMBER, and texts go out through Twilio for real.
"""
import os

DEMO_MODE = os.getenv("DEMO_MODE", "1") == "1"

# Where Twilio POSTs delivery receipts (delivered/failed/undelivered) so bad
# numbers get flagged on the dashboard.
STATUS_CALLBACK_URL = (os.getenv("PUBLIC_BASE_URL", "https://www.tuitionping.com")
                        + "/webhooks/twilio/status")


def _real_credentials_present():
    return bool(os.getenv("TWILIO_ACCOUNT_SID")
                and os.getenv("TWILIO_AUTH_TOKEN")
                and os.getenv("TWILIO_FROM_NUMBER"))


def send_sms(to: str, body: str, provider_id=None, family_id=None, *, tuition_reminder=False):
    """Send one SMS. Always logs to message_log; returns a small result dict."""
    from store import log_message  # lazy import: store must not import sms

    if DEMO_MODE or not _real_credentials_present():
        # Demo path — visible in the terminal and in the dashboard's message log.
        print(f"[DEMO SMS] to={to}\n  {body}", flush=True)
        log_message(provider_id, family_id, "out", body, "sent (demo)")
        return {"demo": True, "to": to}

    # Real path — Twilio.
    from twilio.rest import Client
    client = Client(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
    msg = client.messages.create(
        to=to,
        from_=os.getenv("TWILIO_FROM_NUMBER"),
        body=body,
        status_callback=STATUS_CALLBACK_URL,
    )
    log_message(provider_id, family_id, "out", body, f"sent ({msg.sid})",
                twilio_sid=msg.sid)
    if tuition_reminder and provider_id and family_id:
        import growth
        growth.milestone(provider_id, "first_reminder")
    return {"demo": False, "sid": msg.sid}
