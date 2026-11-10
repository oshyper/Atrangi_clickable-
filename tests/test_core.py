"""Run with:  python -m unittest discover -s tests -v   (no Telegram / network needed)"""
import os
import sqlite3
import sys
import tempfile
import time
import unittest
import hashlib
import hmac
from urllib.parse import urlencode

_tmp = tempfile.mkdtemp()
os.environ.update(BOT_TOKEN="123:TEST", APP_URL="https://example.com",
                  DATABASE_PATH=os.path.join(_tmp, "t.db"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, security  # noqa: E402
from app.timeutil import fmt_local  # noqa: E402


class DbTests(unittest.TestCase):
    def setUp(self):
        for ext in ("", "-wal", "-shm"):
            try:
                os.remove(db.DATABASE_PATH + ext)
            except FileNotFoundError:
                pass
        db.init_db()

    def test_migrates_old_schema(self):
        os.remove(db.DATABASE_PATH)
        c = sqlite3.connect(db.DATABASE_PATH)
        c.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,"
                  " created_at TEXT NOT NULL, last_seen TEXT NOT NULL)")
        c.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id TEXT NOT NULL,"
                  " message_id INTEGER NOT NULL, text TEXT DEFAULT '', media_type TEXT DEFAULT '',"
                  " file_id TEXT DEFAULT '', caption TEXT DEFAULT '', created_at TEXT NOT NULL,"
                  " UNIQUE(channel_id, message_id))")
        c.execute("INSERT INTO users VALUES (1,'a','A','2026-01-01','2026-01-01')")
        c.execute("INSERT INTO posts (channel_id,message_id,text,created_at) VALUES ('-1',1,'old','2026-01-01')")
        c.commit(); c.close()
        db.init_db()
        self.assertEqual(db.get_post(1)["text"], "old")
        self.assertEqual(db.get_notify_users(), [1])

    def test_add_dedupe_and_edit(self):
        self.assertTrue(db.add_post(-100, 5, text="hi", posted_at="2026-01-01T00:00:00+00:00"))
        self.assertFalse(db.add_post(-100, 5, text="again"))
        self.assertTrue(db.update_post(-100, 5, text="edited"))
        p = db.get_post(1)
        self.assertEqual(p["text"], "edited")
        self.assertTrue(p["edited_at"])
        self.assertFalse(db.update_post(-100, 999, text="x"))

    def test_album_never_split_across_pages(self):
        for i in range(1, 4):
            db.add_post(-100, i, text=f"p{i}")
        for i in range(4, 9):  # album of 5 messages
            db.add_post(-100, i, media_group_id="G1", file_id=f"f{i}", media_type="photo")
        db.add_post(-100, 9, text="newest")
        rows, more = db.get_posts_page(limit=4)  # would cut the album in half
        ids = {r["message_id"] for r in rows}
        self.assertTrue({4, 5, 6, 7, 8} <= ids)
        self.assertTrue(more)
        rows2, more2 = db.get_posts_page(limit=4, before_id=min(r["id"] for r in rows))
        self.assertEqual({r["message_id"] for r in rows2}, {1, 2, 3})
        self.assertFalse(more2)

    def test_users_block_mute_stats(self):
        db.upsert_user(1, "a", "A"); db.upsert_user(2, "b", "B"); db.upsert_user(3, "c", "C")
        db.set_user_blocked(2, True)
        db.set_notify(3, False)
        self.assertEqual(db.get_broadcast_users(), [1, 3])
        self.assertEqual(db.get_notify_users(), [1])
        s = db.member_stats()
        self.assertEqual((s["total"], s["blocked"], s["muted"], s["active_7d"]), (3, 1, 1, 3))
        db.upsert_user(2, "b", "B")  # interacting again clears the block flag
        self.assertEqual(db.get_broadcast_users(), [1, 2, 3])

    def test_upsert_returns_new_flag(self):
        self.assertTrue(db.upsert_user(1, "a", "A"))
        self.assertFalse(db.upsert_user(1, "a", "A"))

    def test_user_lists_filters_and_pages(self):
        for i in range(1, 26):
            db.upsert_user(i, f"user{i}", f"Name{i}")
        for i in (3, 4, 5):
            db.set_user_blocked(i, True)
        db.set_notify(6, False)
        rows, total, page = db.get_users_page("all", 1, 10)
        self.assertEqual((len(rows), total, page), (10, 25, 1))
        rows, total, page = db.get_users_page("all", 99, 10)   # clamped to last page
        self.assertEqual((len(rows), page), (5, 3))
        self.assertEqual(db.get_users_page("blocked")[1], 3)
        self.assertEqual(db.get_users_page("active")[1], 22)
        self.assertEqual(db.get_users_page("muted")[1], 1)
        self.assertEqual(db.get_users_page("new")[1], 25)
        self.assertEqual(db.get_users_page("bogus")[1], 25)    # unknown filter falls back to all

    def test_stats_growth_and_find(self):
        db.upsert_user(1, "Alice", "Alice"); db.upsert_user(2, "bob", "Bobby")
        db.set_user_blocked(2, True)
        s = db.member_stats()
        self.assertEqual((s["total"], s["new_24h"], s["blocked_7d"], s["active_24h"]), (2, 2, 1, 2))
        self.assertEqual(db.find_users("@alice")[0]["user_id"], 1)
        self.assertEqual(db.find_users("2")[0]["username"], "bob")
        self.assertEqual(db.find_users("Bob")[0]["user_id"], 2)
        self.assertEqual(db.find_users("nobody"), [])

    def test_ban_admins_settings_posts(self):
        db.upsert_user(1, "a", "A"); db.upsert_user(2, "b", "B")
        db.ban_user(2)
        db.ban_user(99)  # user who never started the bot can be banned in advance
        self.assertTrue(db.is_banned(2) and db.is_banned(99) and not db.is_banned(1))
        self.assertEqual(db.get_broadcast_users(), [1])
        self.assertEqual(db.get_notify_users(), [1])
        self.assertEqual(db.get_users_page("banned")[1], 2)
        self.assertEqual(db.member_stats()["banned"], 2)
        self.assertTrue(db.unban_user(2)); self.assertFalse(db.unban_user(2))
        self.assertTrue(db.add_admin(5, 1)); self.assertFalse(db.add_admin(5, 1))
        self.assertEqual([r["user_id"] for r in db.get_admin_rows()], [5])
        self.assertTrue(db.remove_admin(5)); self.assertFalse(db.remove_admin(5))
        self.assertIsNone(db.get_setting("welcome_text"))
        db.set_setting("welcome_text", "Hi"); db.set_setting("welcome_text", "Hello")
        self.assertEqual(db.get_setting("welcome_text"), "Hello")
        db.delete_setting("welcome_text"); self.assertEqual(db.get_setting("welcome_text", "x"), "x")
        db.add_post(-100, 1, text="single")
        for i in (2, 3):
            db.add_post(-100, i, media_group_id="G", file_id="f", media_type="photo")
        self.assertEqual(len(db.recent_posts(10)), 3)
        self.assertEqual(db.delete_post(3), 2)   # whole album removed
        self.assertEqual(db.delete_post(1), 1)
        self.assertEqual(db.delete_post(1), 0)

    def test_ist_formatting(self):
        self.assertEqual(fmt_local("2026-11-10T17:24:00+00:00"), "10 Nov 2026, 10:54 PM IST")
        self.assertEqual(fmt_local("2026-11-05T18:30:00+00:00"), "6 Nov 2026, 12:00 AM IST")
        self.assertEqual(fmt_local("2026-11-05T03:35:00", False), "5 Nov 2026")
        self.assertEqual(fmt_local(None), "-")

    def test_backup(self):
        db.upsert_user(1, "a", "A")
        dest = os.path.join(_tmp, "backup.db")
        db.backup_db(dest)
        self.assertEqual(sqlite3.connect(dest).execute("select count(*) from users").fetchone()[0], 1)


def _signed(params, token):
    check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    return urlencode({**params, "hash": hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()})


class SecurityTests(unittest.TestCase):
    def test_init_data(self):
        good = _signed({"auth_date": str(int(time.time())), "user": '{"id":7}', "query_id": "x"}, "123:TEST")
        self.assertEqual(security.validate_init_data(good, "123:TEST")["user"]["id"], 7)
        self.assertIsNone(security.validate_init_data(good, "999:OTHER"))
        self.assertIsNone(security.validate_init_data(good.replace("query_id=x", "query_id=y"), "123:TEST"))
        old = _signed({"auth_date": str(int(time.time()) - 10 * 86400)}, "123:TEST")
        self.assertIsNone(security.validate_init_data(old, "123:TEST"))
        self.assertIsNone(security.validate_init_data("", "123:TEST"))
        self.assertIsNone(security.validate_init_data("garbage", "123:TEST"))

    def test_media_token(self):
        t = security.make_media_token("123:TEST")
        self.assertTrue(security.verify_media_token("123:TEST", t))
        self.assertFalse(security.verify_media_token("other", t))
        self.assertFalse(security.verify_media_token("123:TEST", "1.abc"))
        self.assertFalse(security.verify_media_token("123:TEST", "junk"))


if __name__ == "__main__":
    unittest.main()
