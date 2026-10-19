"""Pacific display of admin timestamps; stored timestamps remain UTC."""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")


def pacific_datetime(value):
    """Read ISO timestamps, including older naive UTC values."""
    if not value:
        return None
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(PACIFIC)
    except (ValueError, TypeError, OverflowError):
        return None


def _calendar_date(value):
    # A calendar date has no time zone and must not move to the previous day.
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str) and len(value.strip()) == 10:
        try:
            return date.fromisoformat(value.strip()).isoformat()
        except ValueError:
            pass
    return None


def pacific_date(value):
    calendar_date = _calendar_date(value)
    if calendar_date:
        return calendar_date
    dt = pacific_datetime(value)
    return dt.date().isoformat() if dt else "—"


def pacific_time(value):
    calendar_date = _calendar_date(value)
    if calendar_date:
        return calendar_date
    dt = pacific_datetime(value)
    if not dt:
        return "—"
    clock = dt.strftime("%I:%M:%S %p %Z").lstrip("0")
    return f"{dt:%Y-%m-%d} {clock}"


def pacific_iso(value):
    """Keep machine-readable admin exports precise, with the Pacific offset."""
    dt = pacific_datetime(value)
    return dt.isoformat() if dt else ""


def pacific_today():
    return datetime.now(PACIFIC).date()
