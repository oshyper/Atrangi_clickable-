import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from app.config import DATABASE_PATH

Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)

def conn():
    c = sqlite3.connect(DATABASE_PATH)
    c.row_factory = sqlite3.Row
    return c

def _now():
    return datetime.now(timezone.utc).isoformat()

def init_db():
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            created_at TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            is_blocked INTEGER NOT NULL DEFAULT 0,
            blocked_at TEXT,
            last_broadcast_at TEXT,
            last_broadcast_status TEXT DEFAULT ''
        )""")
        # Safe migration for databases created by older versions.
        cols = {r["name"] for r in c.execute("PRAGMA table_info(users)").fetchall()}
        for name, definition in [
            ("is_blocked", "INTEGER NOT NULL DEFAULT 0"),
            ("blocked_at", "TEXT"),
            ("last_broadcast_at", "TEXT"),
            ("last_broadcast_status", "TEXT DEFAULT ''"),
            ("html_text", "TEXT DEFAULT ''"),
            ("html_caption", "TEXT DEFAULT ''"),
        ]:
            if name not in cols:
                c.execute(f"ALTER TABLE users ADD COLUMN {name} {definition}")

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
        post_cols = {r["name"] for r in c.execute("PRAGMA table_info(posts)").fetchall()}
        for name, definition in [("html_text", "TEXT DEFAULT ''"), ("html_caption", "TEXT DEFAULT ''")]:
            if name not in post_cols:
                c.execute(f"ALTER TABLE posts ADD COLUMN {name} {definition}")

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
    with conn() as c:
        return [int(r["user_id"]) for r in c.execute(
            "SELECT user_id FROM users WHERE is_blocked=0 ORDER BY user_id"
        ).fetchall()]

def member_stats():
    with conn() as c:
        total = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        blocked = c.execute("SELECT COUNT(*) n FROM users WHERE is_blocked=1").fetchone()["n"]
        active = total - blocked
        recent = c.execute(
            "SELECT COUNT(*) n FROM users WHERE last_seen >= datetime('now','-7 days')"
        ).fetchone()["n"]
        return {"total": total, "active": active, "blocked": blocked, "active_7d": recent}

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

def add_post(channel_id, message_id, text="", media_type="", file_id="", caption="", html_text="", html_caption=""):
    now = _now()
    with conn() as c:
        c.execute("""INSERT OR IGNORE INTO posts
                     (channel_id,message_id,text,media_type,file_id,caption,created_at)
                     VALUES(?,?,?,?,?,?,?)""",
                  (str(channel_id), message_id, text or "", media_type or "",
                   file_id or "", caption or "", html_text or "", html_caption or "", now))

def count_recent_posts(channel_id, since_message_id=None):
    with conn() as c:
        if since_message_id is None:
            row = c.execute("SELECT COUNT(*) n FROM posts WHERE channel_id=?", (str(channel_id),)).fetchone()
        else:
            row = c.execute("SELECT COUNT(*) n FROM posts WHERE channel_id=? AND message_id>?",
                            (str(channel_id), since_message_id)).fetchone()
        return row["n"]

def get_posts(limit=50):
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM posts ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()]

def get_post(post_id):
    with conn() as c:
        r = c.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
        return dict(r) if r else None

def user_count():
    with conn() as c:
        return c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]

def latest_message_id(channel_id):
    with conn() as c:
        r = c.execute("SELECT MAX(message_id) n FROM posts WHERE channel_id=?", (str(channel_id),)).fetchone()
        return r["n"]
