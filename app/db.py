import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import DATABASE_PATH

Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)

POST_FIELDS = (
    "text", "media_type", "file_id", "caption", "html_text", "html_caption",
    "media_group_id", "mime_type", "file_name", "file_size", "thumb_id",
    "posted_at", "channel_username",
)
_EDITABLE_FIELDS = (
    "text", "media_type", "file_id", "caption", "html_text", "html_caption",
    "mime_type", "file_name", "file_size", "thumb_id",
)


@contextmanager
def conn():
    """Open a connection, commit on success, ALWAYS close (the old code leaked connections)."""
    c = sqlite3.connect(DATABASE_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _ensure_columns(c, table, columns):
    existing = {r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, definition in columns.items():
        if name not in existing:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def init_db():
    with conn() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")

        c.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            created_at TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            is_blocked INTEGER NOT NULL DEFAULT 0,
            blocked_at TEXT,
            last_broadcast_at TEXT,
            last_broadcast_status TEXT DEFAULT '',
            notify INTEGER NOT NULL DEFAULT 1,
            banned INTEGER NOT NULL DEFAULT 0,
            banned_at TEXT
        )""")
        # Safe migration for databases created by older versions.
        _ensure_columns(c, "users", {
            "is_blocked": "INTEGER NOT NULL DEFAULT 0",
            "blocked_at": "TEXT",
            "last_broadcast_at": "TEXT",
            "last_broadcast_status": "TEXT DEFAULT ''",
            "notify": "INTEGER NOT NULL DEFAULT 1",
            "banned": "INTEGER NOT NULL DEFAULT 0",
            "banned_at": "TEXT",
        })
        c.execute("""CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            added_by INTEGER,
            added_at TEXT NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""")

        c.execute("""CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT NOT NULL,
            message_id INTEGER NOT NULL,
            text TEXT DEFAULT '',
            media_type TEXT DEFAULT '',
            file_id TEXT DEFAULT '',
            caption TEXT DEFAULT '',
            html_text TEXT DEFAULT '',
            html_caption TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE(channel_id, message_id)
        )""")
        _ensure_columns(c, "posts", {
            "html_text": "TEXT DEFAULT ''",
            "html_caption": "TEXT DEFAULT ''",
            "media_group_id": "TEXT DEFAULT ''",
            "mime_type": "TEXT DEFAULT ''",
            "file_name": "TEXT DEFAULT ''",
            "file_size": "INTEGER DEFAULT 0",
            "thumb_id": "TEXT DEFAULT ''",
            "posted_at": "TEXT DEFAULT ''",
            "channel_username": "TEXT DEFAULT ''",
            "edited_at": "TEXT DEFAULT ''",
        })
        c.execute("CREATE INDEX IF NOT EXISTS idx_posts_group ON posts(channel_id, media_group_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_users_blocked ON users(is_blocked, notify)")


# ----------------------------------------------------------------- users

def upsert_user(user_id, username, first_name) -> bool:
    """Create or refresh a user. Returns True when this is a brand-new user."""
    now = _now()
    with conn() as c:
        is_new = c.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone() is None
        c.execute("""INSERT INTO users
                     (user_id,username,first_name,created_at,last_seen,is_blocked)
                     VALUES(?,?,?,?,?,0)
                     ON CONFLICT(user_id) DO UPDATE SET
                     username=excluded.username,
                     first_name=excluded.first_name,
                     last_seen=excluded.last_seen,
                     is_blocked=0,
                     blocked_at=NULL""",
                  (user_id, username or "", first_name or "", now, now))
        return is_new


def set_user_blocked(user_id, blocked: bool):
    now = _now()
    with conn() as c:
        if blocked:
            c.execute("UPDATE users SET is_blocked=1, blocked_at=COALESCE(blocked_at,?) WHERE user_id=?",
                      (now, user_id))
        else:
            c.execute("UPDATE users SET is_blocked=0, blocked_at=NULL WHERE user_id=?", (user_id,))


def set_notify(user_id, enabled: bool):
    with conn() as c:
        c.execute("UPDATE users SET notify=? WHERE user_id=?", (1 if enabled else 0, user_id))


def mark_user_broadcast(user_id, status):
    now = _now()
    with conn() as c:
        if status == "sent":
            c.execute("""UPDATE users
                         SET last_broadcast_at=?, last_broadcast_status=?, is_blocked=0,
                             blocked_at=NULL
                         WHERE user_id=?""", (now, status, user_id))
        elif status == "blocked":
            c.execute("""UPDATE users
                         SET last_broadcast_at=?, last_broadcast_status=?,
                             is_blocked=1, blocked_at=COALESCE(blocked_at,?)
                         WHERE user_id=?""", (now, status, now, user_id))
        else:
            c.execute("""UPDATE users
                         SET last_broadcast_at=?, last_broadcast_status=?
                         WHERE user_id=?""", (now, status, user_id))


def get_broadcast_users():
    """Everyone who has not blocked the bot (admin broadcasts ignore the mute setting)."""
    with conn() as c:
        return [int(r["user_id"]) for r in c.execute(
            "SELECT user_id FROM users WHERE is_blocked=0 AND banned=0 ORDER BY user_id"
        ).fetchall()]


def get_notify_users():
    """Users who should receive 'new post' notifications."""
    with conn() as c:
        return [int(r["user_id"]) for r in c.execute(
            "SELECT user_id FROM users WHERE is_blocked=0 AND banned=0 AND notify=1 ORDER BY user_id"
        ).fetchall()]


def member_stats(today_start=None):
    now = datetime.now(timezone.utc)

    def ago(days=0, hours=0):
        return (now - timedelta(days=days, hours=hours)).isoformat()

    with conn() as c:
        def n(sql, *params):
            return c.execute(sql, params).fetchone()["n"]

        total = n("SELECT COUNT(*) n FROM users")
        blocked = n("SELECT COUNT(*) n FROM users WHERE is_blocked=1")
        return {
            "total": total,
            "active": total - blocked,
            "blocked": blocked,
            "muted": n("SELECT COUNT(*) n FROM users WHERE is_blocked=0 AND notify=0"),
            "banned": n("SELECT COUNT(*) n FROM users WHERE banned=1"),
            "new_today": n("SELECT COUNT(*) n FROM users WHERE created_at >= ?", today_start or ago(hours=24)),
            "new_24h": n("SELECT COUNT(*) n FROM users WHERE created_at >= ?", ago(hours=24)),
            "new_7d": n("SELECT COUNT(*) n FROM users WHERE created_at >= ?", ago(days=7)),
            "new_30d": n("SELECT COUNT(*) n FROM users WHERE created_at >= ?", ago(days=30)),
            "blocked_7d": n("SELECT COUNT(*) n FROM users WHERE is_blocked=1 AND blocked_at >= ?", ago(days=7)),
            "active_24h": n("SELECT COUNT(*) n FROM users WHERE last_seen >= ?", ago(hours=24)),
            "active_7d": n("SELECT COUNT(*) n FROM users WHERE last_seen >= ?", ago(days=7)),
            "posts": n("SELECT COUNT(*) n FROM posts"),
        }


USER_FILTERS = ("all", "active", "blocked", "muted", "new", "banned")


def get_users_page(flt="all", page=1, per_page=10):
    """Returns (rows, total, page). `page` is clamped to the valid range."""
    flt = flt if flt in USER_FILTERS else "all"
    where, params, order = {
        "all": ("1=1", (), "created_at DESC"),
        "active": ("is_blocked=0", (), "last_seen DESC"),
        "blocked": ("is_blocked=1", (), "blocked_at DESC"),
        "muted": ("is_blocked=0 AND notify=0", (), "last_seen DESC"),
        "banned": ("banned=1", (), "banned_at DESC"),
        "new": ("created_at >= ?", ((datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(),),
                "created_at DESC"),
    }[flt]
    with conn() as c:
        total = c.execute(f"SELECT COUNT(*) n FROM users WHERE {where}", params).fetchone()["n"]
        pages = max(1, -(-total // per_page))
        page = max(1, min(int(page), pages))
        rows = c.execute(
            f"SELECT * FROM users WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
            (*params, per_page, (page - 1) * per_page),
        ).fetchall()
        return [dict(r) for r in rows], total, page


def get_user(user_id):
    with conn() as c:
        r = c.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        return dict(r) if r else None


def find_users(query, limit=5):
    """Find by numeric id, @username (exact) or first-name prefix."""
    q = (query or "").strip().lstrip("@")
    if not q:
        return []
    with conn() as c:
        if q.isdigit():
            rows = c.execute("SELECT * FROM users WHERE user_id=?", (int(q),)).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM users WHERE lower(username)=lower(?) OR lower(first_name) LIKE lower(?) || '%' "
                "ORDER BY (lower(username)=lower(?)) DESC, last_seen DESC LIMIT ?",
                (q, q, q, limit),
            ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------- ban / admins / settings

def ban_user(user_id):
    now = _now()
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO users (user_id,username,first_name,created_at,last_seen) VALUES (?,?,?,?,?)",
                  (user_id, "", "", now, now))
        c.execute("UPDATE users SET banned=1, banned_at=? WHERE user_id=?", (now, user_id))


def unban_user(user_id) -> bool:
    with conn() as c:
        return c.execute("UPDATE users SET banned=0, banned_at=NULL WHERE user_id=? AND banned=1",
                         (user_id,)).rowcount == 1


def is_banned(user_id) -> bool:
    with conn() as c:
        r = c.execute("SELECT banned FROM users WHERE user_id=?", (user_id,)).fetchone()
        return bool(r and r["banned"])


def touch_user(user_id):
    """Record that the user opened the Mini App (keeps 'active' statistics honest)."""
    with conn() as c:
        c.execute("UPDATE users SET last_seen=? WHERE user_id=?", (_now(), user_id))


def add_admin(user_id, added_by) -> bool:
    with conn() as c:
        return c.execute("INSERT OR IGNORE INTO admins (user_id,added_by,added_at) VALUES (?,?,?)",
                         (user_id, added_by, _now())).rowcount == 1


def remove_admin(user_id) -> bool:
    with conn() as c:
        return c.execute("DELETE FROM admins WHERE user_id=?", (user_id,)).rowcount == 1


def get_admin_rows():
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM admins ORDER BY added_at").fetchall()]


def get_setting(key, default=None):
    with conn() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default


def set_setting(key, value):
    with conn() as c:
        c.execute("INSERT INTO settings (key,value) VALUES (?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def delete_setting(key):
    with conn() as c:
        c.execute("DELETE FROM settings WHERE key=?", (key,))


# ----------------------------------------------------------------- posts

def add_post(channel_id, message_id, **fields) -> bool:
    """Insert a post. Returns True if it was new, False if it already existed."""
    values = {k: fields.get(k) or (0 if k == "file_size" else "") for k in POST_FIELDS}
    cols = ["channel_id", "message_id", "created_at", *POST_FIELDS]
    params = [str(channel_id), message_id, _now(), *(values[k] for k in POST_FIELDS)]
    with conn() as c:
        cur = c.execute(
            f"INSERT OR IGNORE INTO posts ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            params,
        )
        return cur.rowcount == 1


def update_post(channel_id, message_id, **fields) -> bool:
    """Apply an edit made in the channel. Returns True if we had this post stored."""
    sets = [f"{k}=?" for k in _EDITABLE_FIELDS]
    params = [fields.get(k) or (0 if k == "file_size" else "") for k in _EDITABLE_FIELDS]
    with conn() as c:
        cur = c.execute(
            f"UPDATE posts SET {','.join(sets)}, edited_at=? WHERE channel_id=? AND message_id=?",
            [*params, _now(), str(channel_id), message_id],
        )
        return cur.rowcount == 1


def get_posts_page(limit=20, before_id=None):
    """Newest-first page of raw rows (+ has_more). Albums are never split across pages."""
    with conn() as c:
        if before_id:
            rows = c.execute("SELECT * FROM posts WHERE id < ? ORDER BY id DESC LIMIT ?",
                             (before_id, limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM posts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        rows = [dict(r) for r in rows]
        if not rows:
            return [], False

        last = rows[-1]
        if last["media_group_id"]:
            extra = c.execute(
                "SELECT * FROM posts WHERE channel_id=? AND media_group_id=? AND id < ? ORDER BY id DESC",
                (last["channel_id"], last["media_group_id"], last["id"]),
            ).fetchall()
            rows.extend(dict(r) for r in extra)

        min_id = min(r["id"] for r in rows)
        has_more = c.execute("SELECT 1 FROM posts WHERE id < ? LIMIT 1", (min_id,)).fetchone() is not None
        return rows, has_more


def latest_post_id():
    with conn() as c:
        return c.execute("SELECT COALESCE(MAX(id),0) n FROM posts").fetchone()["n"]


def get_post(post_id):
    with conn() as c:
        r = c.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
        return dict(r) if r else None


def backup_db(dest_path):
    """Consistent copy of the live database (safe while the bot is running)."""
    src = sqlite3.connect(DATABASE_PATH)
    dst = sqlite3.connect(dest_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def recent_posts(limit=10):
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM posts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


def delete_post(message_id) -> int:
    """Remove a post (and the whole album it belongs to) from the Mini App. Returns rows deleted."""
    deleted = 0
    with conn() as c:
        for r in c.execute("SELECT channel_id, media_group_id FROM posts WHERE message_id=?",
                           (message_id,)).fetchall():
            if r["media_group_id"]:
                cur = c.execute("DELETE FROM posts WHERE channel_id=? AND media_group_id=?",
                                (r["channel_id"], r["media_group_id"]))
            else:
                cur = c.execute("DELETE FROM posts WHERE channel_id=? AND message_id=?",
                                (r["channel_id"], message_id))
            deleted += cur.rowcount
    return deleted
