from datetime import datetime, timezone

from app.config import LOCAL_TZ, TZ_LABEL


def fmt_local(iso, with_time=True) -> str:
    """UTC ISO string from the database -> '10 Nov 2026, 10:54 PM IST'."""
    try:
        d = datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return "-"
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    d = d.astimezone(LOCAL_TZ)
    day = f"{d.day} {d:%b %Y}"
    if not with_time:
        return day
    hour12 = d.hour % 12 or 12
    return f"{day}, {hour12}:{d:%M} {'AM' if d.hour < 12 else 'PM'} {TZ_LABEL}"


def local_now() -> datetime:
    return datetime.now(LOCAL_TZ)


def local_day_start_utc_iso() -> str:
    """Start of today in the local timezone (midnight IST), as a UTC ISO string."""
    start = local_now().replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).isoformat()
