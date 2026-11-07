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
            notify INTEGER NOT NULL DEFAULT 1
        )""")
        # Safe migration for databases created by older versions.
        _ensure_columns(c, "users", {
            "is_blocked": "INTEGER NOT NULL DEFAULT 0",
            "blocked_at": "TEXT",
            "last_broadcast_at": "TEXT",
            "last_broadcast_status": "TEXT DEFAULT ''",
            "notify": "INTEGER NOT NULL DEFAULT 1",
        })

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

def upsert_user(user_id, username, first_name):
    now = _now()
    with conn() as c:
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
            "SELECT user_id FROM users WHERE is_blocked=0 ORDER BY user_id"
        ).fetchall()]


def get_notify_users():
    """Users who should receive 'new post' notifications."""
    with conn() as c:
        return [int(r["user_id"]) for r in c.execute(
            "SELECT user_id FROM users WHERE is_blocked=0 AND notify=1 ORDER BY user_id"
        ).fetchall()]


def member_stats():
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    with conn() as c:
        total = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        blocked = c.execute("SELECT COUNT(*) n FROM users WHERE is_blocked=1").fetchone()["n"]
        muted = c.execute("SELECT COUNT(*) n FROM users WHERE is_blocked=0 AND notify=0").fetchone()["n"]
        recent = c.execute("SELECT COUNT(*) n FROM users WHERE last_seen >= ?", (cutoff,)).fetchone()["n"]
        posts = c.execute("SELECT COUNT(*) n FROM posts").fetchone()["n"]
        return {
            "total": total, "active": total - blocked, "blocked": blocked,
            "muted": muted, "active_7d": recent, "posts": posts,
        }


def get_members(limit=100, blocked=None):
    with conn() as c:
        if blocked is None:
            rows = c.execute(
                "SELECT * FROM users ORDER BY last_seen DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM users WHERE is_blocked=? ORDER BY last_seen DESC LIMIT ?",
                (1 if blocked else 0, limit)
            ).fetchall()
        return [dict(r) for r in rows]


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
