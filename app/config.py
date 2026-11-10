import os
from datetime import timedelta, timezone

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# APP_URL: use the explicit value, otherwise fall back to Railway's generated domain.
APP_URL = os.getenv("APP_URL", "").strip().rstrip("/")
if not APP_URL:
    _railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if _railway_domain:
        APP_URL = f"https://{_railway_domain}"
if APP_URL and not APP_URL.startswith(("http://", "https://")):
    # People often paste "xyz.up.railway.app" without the scheme.
    APP_URL = f"https://{APP_URL}"

MAIN_CHANNEL_ID = os.getenv("MAIN_CHANNEL_ID", "").strip()
BACKUP_CHANNEL_URL = os.getenv("BACKUP_CHANNEL_URL", "https://t.me/Atrangii_re_new").strip()

# Database: explicit path > Railway volume > local ./data
_volume = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
DATABASE_PATH = os.getenv("DATABASE_PATH", "").strip() or (
    os.path.join(_volume, "bot.db") if _volume else "./data/bot.db"
)

ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().lstrip("-").isdigit()
}

# When true, /api/* only answers requests coming from inside Telegram
# (the Mini App's signed initData is verified on the server).
REQUIRE_TG_AUTH = _bool("REQUIRE_TG_AUTH", False)

# Send "new post" notifications to users (can be switched off completely).
NOTIFY_ENABLED = _bool("NOTIFY_ENABLED", True)
# Seconds to wait before notifying, so an album / burst of posts becomes ONE notification.
NOTIFY_DELAY = _float("NOTIFY_DELAY", 8.0)

# Instantly message every admin when someone starts / blocks / unblocks the bot.
ADMIN_ALERTS = _bool("ADMIN_ALERTS", False)


def _hhmm(name: str):
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        h, m = (int(x) for x in raw.split(":"))
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except ValueError:
        pass
    return None


def _utc_offset(raw: str):
    """'+05:30' -> timedelta. Falls back to India (+05:30) when empty/invalid."""
    raw = (raw or "").strip() or "+05:30"
    try:
        sign = -1 if raw.startswith("-") else 1
        h, _, m = raw.lstrip("+-").partition(":")
        return sign * timedelta(hours=int(h), minutes=int(m or 0))
    except ValueError:
        return timedelta(hours=5, minutes=30)


# All dates shown by the bot (user lists, reports, ...) use this timezone. Default: India (IST).
_offset = _utc_offset(os.getenv("TIMEZONE_OFFSET", ""))
LOCAL_TZ = timezone(_offset)
TZ_LABEL = "IST" if _offset == timedelta(hours=5, minutes=30) else (
    "UTC" + ("+" if _offset >= timedelta(0) else "-")
    + f"{abs(int(_offset.total_seconds())) // 3600:02d}:{abs(int(_offset.total_seconds())) // 60 % 60:02d}"
)

# Daily statistics report to admins at "HH:MM" in the timezone above (default IST). Empty = off.
DAILY_REPORT_TIME = _hhmm("DAILY_REPORT_TIME")

POSTS_PAGE_SIZE = max(5, min(_int("POSTS_PAGE_SIZE", 20), 50))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Copy .env.example to .env and set it.")
if not APP_URL.startswith("https://"):
    raise RuntimeError(
        "APP_URL must be an HTTPS URL (Telegram Mini Apps require HTTPS). "
        "On Railway, generate a public domain and set APP_URL to it."
    )
