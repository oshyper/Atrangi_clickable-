"""Telegram Mini App initData validation and short-lived media tokens."""
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


def validate_init_data(init_data: str, bot_token: str, max_age: int = 86400):
    """Verify Telegram WebApp initData. Returns {'user':..., 'auth_date':...} or None."""
    if not init_data or len(init_data) > 8192:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None

    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        return None

    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        return None
    if max_age and time.time() - auth_date > max_age:
        return None

    user = None
    if "user" in pairs:
        try:
            user = json.loads(pairs["user"])
        except ValueError:
            user = None
    return {"user": user, "auth_date": auth_date}


def _sig(bot_token: str, exp: int) -> str:
    key = hashlib.sha256(b"atrangii-media:" + bot_token.encode()).digest()
    return hmac.new(key, str(exp).encode(), hashlib.sha256).hexdigest()


def make_media_token(bot_token: str, ttl: int = 6 * 3600) -> str:
    """Token is constant inside a time bucket so the browser can cache media URLs."""
    exp = (int(time.time()) // ttl + 2) * ttl
    return f"{exp}.{_sig(bot_token, exp)}"


def verify_media_token(bot_token: str, token: str) -> bool:
    try:
        exp_s, sig = token.split(".", 1)
        exp = int(exp_s)
    except (ValueError, AttributeError):
        return False
    if exp < time.time():
        return False
    return hmac.compare_digest(_sig(bot_token, exp), sig)
