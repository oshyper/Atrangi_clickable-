import asyncio
import csv
import html
import io
import logging
import os
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand, BotCommandScopeChat, BufferedInputFile, CallbackQuery, ChatMemberUpdated, FSInputFile,
    InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo,
)
from aiogram.utils.text_decorations import html_decoration

from app.config import (
    ADMIN_ALERTS, ADMIN_IDS, APP_URL, DAILY_REPORT_TIME, DATABASE_PATH, REQUIRE_TG_AUTH, BACKUP_CHANNEL_URL, BOT_TOKEN, MAIN_CHANNEL_ID,
    NOTIFY_DELAY, NOTIFY_ENABLED,
)
from app.db import (
    add_admin, add_post, backup_db, ban_user, delete_post, delete_setting, find_users,
    get_admin_rows, get_broadcast_users, get_notify_users, get_setting, get_user, get_users_page,
    init_db, is_banned, mark_user_broadcast, member_stats, recent_posts, remove_admin, set_notify,
    set_setting, set_user_blocked, unban_user, update_post, upsert_user,
)
from app.timeutil import TZ_LABEL, fmt_local, local_day_start_utc_iso, local_now

log = logging.getLogger("bot")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
# This bot is only meant to be used in private chats.
dp.message.filter(F.chat.type == "private")

# Admins who sent /broadcast and are now expected to send the message to broadcast.
broadcast_waiting: set[int] = set()
# Admins who asked to cancel a running broadcast.
broadcast_cancel: set[int] = set()
# Admins with a broadcast currently running (prevents accidental double sends).
broadcast_running: set[int] = set()


# ----------------------------------------------------------------- helpers

# Changes on every deploy. Added to the Mini App URL so Telegram's in-app browser can
# never keep showing an old cached copy of the page after an update.
APP_VERSION = (os.getenv("RAILWAY_GIT_COMMIT_SHA", "")[:8] or str(int(time.time())))


def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Open Channel", web_app=WebAppInfo(url=f"{APP_URL}/?v={APP_VERSION}"))],
        [InlineKeyboardButton(text="Backup Channel", url=BACKUP_CHANNEL_URL)],
    ])


START_TIME = time.time()


def admin_ids() -> set[int]:
    """Primary owners (ADMIN_IDS env) + admins added from inside the bot."""
    return set(ADMIN_IDS) | {r["user_id"] for r in get_admin_rows()}


def is_owner_id(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def is_admin_id(user_id: int) -> bool:
    return user_id in ADMIN_IDS or user_id in admin_ids()


def is_admin(message: Message) -> bool:
    return bool(message.from_user) and is_admin_id(message.from_user.id)


def is_owner(message: Message) -> bool:
    return bool(message.from_user) and is_owner_id(message.from_user.id)


class BanMiddleware(BaseMiddleware):
    """Silently ignore every message from banned users (admins can never be banned)."""

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user and not is_admin_id(user.id) and is_banned(user.id):
            return None
        return await handler(event, data)


dp.message.outer_middleware(BanMiddleware())


async def safe_send(user_id: int, text: str, **kwargs) -> str:
    """Send a message with rate-limit handling. Returns 'sent' | 'blocked' | 'failed'."""
    for _ in range(3):
        try:
            await bot.send_message(user_id, text, **kwargs)
            return "sent"
        except TelegramRetryAfter as e:
            await asyncio.sleep(float(e.retry_after) + 0.5)
        except TelegramForbiddenError:
            return "blocked"
        except TelegramBadRequest:
            return "failed"
        except Exception:
            log.exception("send_message to %s failed", user_id)
            return "failed"
    return "failed"


async def safe_copy(user_id: int, from_chat_id: int, message_id: int) -> str:
    """copy_message (keeps text/caption/media) with rate-limit handling."""
    for _ in range(3):
        try:
            await bot.copy_message(chat_id=user_id, from_chat_id=from_chat_id, message_id=message_id)
            return "sent"
        except TelegramRetryAfter as e:
            await asyncio.sleep(float(e.retry_after) + 0.5)
        except TelegramForbiddenError:
            return "blocked"
        except TelegramBadRequest:
            return "failed"
        except Exception:
            log.exception("copy_message to %s failed", user_id)
            return "failed"
    return "failed"


async def alert_admins(text: str):
    """Instant admin notification (only when ADMIN_ALERTS=true)."""
    if not ADMIN_ALERTS:
        return
    for admin_id in admin_ids():
        await safe_send(admin_id, text, parse_mode="HTML")


# --------------------------------------------------------------- broadcast

async def send_broadcast_report(admin_id, total, sent, blocked, failed, cancelled=False):
    status = "Cancelled" if cancelled else "Completed"
    await safe_send(
        admin_id,
        f"Broadcast {status}\n\n"
        f"Total: <b>{total}</b>\n"
        f"Sent: <b>{sent}</b>\n"
        f"Blocked the bot: <b>{blocked}</b>\n"
        f"Other failures: <b>{failed}</b>",
        parse_mode="HTML",
    )


async def broadcast_message(from_chat_id: int, message_id: int, admin_id: int):
    """Copy one message to every user who has started the bot."""
    if admin_id in broadcast_running:
        await safe_send(admin_id, "A broadcast is already running. Wait for it to finish or send /cancel.")
        return

    users = get_broadcast_users()
    total = len(users)
    if total == 0:
        await safe_send(admin_id, "There are no registered users yet.")
        return

    broadcast_running.add(admin_id)
    broadcast_cancel.discard(admin_id)
    sent = blocked = failed = 0
    try:
        progress_msg = await bot.send_message(
            admin_id,
            f"Broadcast started...\n\n"
            f"Total users: <b>{total}</b>\n"
            f"Sent: <b>0</b> | Failed: <b>0</b>\n\n"
            f"Cancel: /cancel",
            parse_mode="HTML",
        )

        for index, user_id in enumerate(users, start=1):
            if admin_id in broadcast_cancel:
                await send_broadcast_report(admin_id, total, sent, blocked, failed, cancelled=True)
                return

            status = await safe_copy(user_id, from_chat_id, message_id)
            mark_user_broadcast(user_id, status)
            if status == "sent":
                sent += 1
            elif status == "blocked":
                blocked += 1
            else:
                failed += 1

            # Stay comfortably below Telegram's global send rate (~30 msg/s).
            await asyncio.sleep(0.05)

            if index == 1 or index % 25 == 0 or index == total:
                try:
                    await progress_msg.edit_text(
                        f"Broadcast in progress...\n\n"
                        f"Total: <b>{total}</b>\n"
                        f"Processed: <b>{index}/{total}</b>\n"
                        f"Sent: <b>{sent}</b>\n"
                        f"Blocked: <b>{blocked}</b>\n"
                        f"Failed: <b>{failed}</b>\n\n"
                        f"Cancel: /cancel",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

        await send_broadcast_report(admin_id, total, sent, blocked, failed)
    finally:
        broadcast_running.discard(admin_id)
        broadcast_cancel.discard(admin_id)


# ------------------------------------------------------- user-facing commands

DEFAULT_WELCOME = "🔥 Welcome! ✅\n\n👇 Post dekhne ke liye <b>Open Channel</b> par click karo 😉♦️"

USER_COMMANDS = [
    ("start", "Open the channel"),
    ("mute", "Stop new-post notifications"),
    ("unmute", "Turn notifications back on"),
    ("help", "Show help"),
]

ADMIN_COMMANDS = [
    ("start", "Open the channel"),
    ("help", "All admin commands"),
    ("stats", "Users, blocked, growth"),
    ("users", "Browse all users"),
    ("blocked", "Users who blocked the bot"),
    ("newusers", "Users joined in last 24h"),
    ("user", "Find one user by ID / @username"),
    ("export", "Download users CSV"),
    ("broadcast", "Send a message to all users"),
    ("cancel", "Cancel a broadcast"),
    ("ban", "Ban a user"),
    ("unban", "Unban a user"),
    ("banned", "List banned users"),
    ("posts", "Last 10 posts in the Mini App"),
    ("delpost", "Remove a post from the Mini App"),
    ("notifications", "New-post notifications on/off"),
    ("setwelcome", "Change the welcome message"),
    ("resetwelcome", "Restore default welcome"),
    ("admins", "List admins"),
    ("addadmin", "Add an admin (owners only)"),
    ("removeadmin", "Remove an admin (owners only)"),
    ("status", "Bot health and uptime"),
    ("backup", "Download database backup"),
    ("mute", "Stop new-post notifications"),
    ("unmute", "Turn notifications back on"),
]

ADMIN_HELP = f"""🛠 <b>Admin Panel</b>  ({TZ_LABEL} time)

<b>Users</b>
/stats - totals, blocked, growth
/users [all|active|blocked|muted|new|banned] - browse users
/blocked - users who blocked the bot
/newusers - joined in the last 24h
/user ID or @username - one user's details
/export [filter] - users as CSV file

<b>Broadcast</b>
/broadcast - send any message to all users
   (or reply to a message with /broadcast, or /broadcast your text)
/cancel - cancel a running broadcast

<b>Moderation</b>
/ban ID - ban a user (no bot, no broadcasts, no Mini App)
/unban ID - unban | /banned - banned list

<b>Mini App posts</b>
/posts - last 10 stored posts (with message IDs)
/delpost MESSAGE_ID - remove a post (albums included) from the Mini App

<b>Settings</b>
/notifications on|off - new-post notifications
/setwelcome TEXT - change the welcome message (bold/italic/links work)
/resetwelcome - restore the default welcome

<b>Access</b>
/admins - list admins
/addadmin ID | /removeadmin ID (primary owners only)

<b>Info</b>
/status - bot health | /backup - database file

<b>For everyone</b>
/mute | /unmute - notifications for yourself"""


def _commands(items):
    return [BotCommand(command=c, description=d) for c, d in items]


async def set_admin_menu(admin_id: int) -> bool:
    """Give an admin the full command menu (the 'Menu' button next to the message box)."""
    try:
        await bot.set_my_commands(_commands(ADMIN_COMMANDS), scope=BotCommandScopeChat(chat_id=admin_id))
        return True
    except Exception:
        return False  # the admin has not started the bot yet


@dp.message(CommandStart())
async def start(message: Message):
    u = message.from_user
    is_new = upsert_user(u.id, u.username, u.first_name)
    if is_new:
        await alert_admins(
            f"🆕 <b>New user</b>\n{_user_link(dict(user_id=u.id, first_name=u.first_name, username=u.username))}"
            f" · <code>{u.id}</code>\nTotal users: <b>{member_stats()['total']}</b>"
        )
    text = get_setting("welcome_text") or DEFAULT_WELCOME
    try:
        await message.answer(text, reply_markup=main_keyboard(), parse_mode="HTML")
    except TelegramBadRequest:
        await message.answer(DEFAULT_WELCOME, reply_markup=main_keyboard(), parse_mode="HTML")
    if is_admin_id(u.id):
        await set_admin_menu(u.id)
        await message.answer("🛠 You are an admin. Send /help to see all admin commands.")


@dp.message(Command("help"))
async def help_command(message: Message):
    if is_admin(message):
        await message.answer(ADMIN_HELP, parse_mode="HTML")
        return
    await message.answer(
        "<b>Commands</b>\n"
        "/start - open the channel\n"
        "/mute - stop new-post notifications\n"
        "/unmute - turn notifications back on",
        parse_mode="HTML",
    )


@dp.message(Command("mute"))
async def mute(message: Message):
    u = message.from_user
    upsert_user(u.id, u.username, u.first_name)
    set_notify(u.id, False)
    await message.answer("🔕 Notifications are off. Send /unmute to turn them back on.")


@dp.message(Command("unmute"))
async def unmute(message: Message):
    u = message.from_user
    upsert_user(u.id, u.username, u.first_name)
    set_notify(u.id, True)
    await message.answer("🔔 Notifications are on again.")


@dp.my_chat_member()
async def my_chat_member(event: ChatMemberUpdated):
    """Telegram tells us in real time when a user blocks / unblocks the bot."""
    if event.chat.type != "private":
        return
    status = str(getattr(event.new_chat_member.status, "value", event.new_chat_member.status))
    before = get_user(event.chat.id)
    if before is None:
        return  # never pressed /start, nothing to track
    if status in {"kicked", "left"}:
        set_user_blocked(event.chat.id, True)
        if not before["is_blocked"]:
            await alert_admins(f"🚫 <b>Blocked the bot</b>\n{_user_link(before)} · <code>{before['user_id']}</code>\n"
                               f"Active users now: <b>{member_stats()['active']}</b>")
    elif status == "member":
        set_user_blocked(event.chat.id, False)
        if before["is_blocked"]:
            await alert_admins(f"✅ <b>Unblocked the bot</b>\n{_user_link(before)} · <code>{before['user_id']}</code>")


# ------------------------------------------------------------ admin commands

def stats_text() -> str:
    s = member_stats(local_day_start_utc_iso())
    return (
        "📊 <b>Member Statistics</b>\n\n"
        f"👥 Total started the bot: <b>{s['total']}</b>\n"
        f"✅ Active: <b>{s['active']}</b>\n"
        f"🚫 Blocked the bot: <b>{s['blocked']}</b>\n"
        f"🔕 Notifications muted: <b>{s['muted']}</b>\n\n"
        f"⛔ Banned: <b>{s['banned']}</b>\n"
        f"📅 Joined today ({TZ_LABEL}): <b>{s['new_today']}</b>\n"
        f"🆕 New users: 24h <b>{s['new_24h']}</b> | 7d <b>{s['new_7d']}</b> | 30d <b>{s['new_30d']}</b>\n"
        f"📉 Blocked in last 7 days: <b>{s['blocked_7d']}</b>\n"
        f"🟢 Active: 24h <b>{s['active_24h']}</b> | 7d <b>{s['active_7d']}</b>\n"
        f"📰 Posts stored: <b>{s['posts']}</b>\n\n"
        "Lists: /users /blocked /newusers"
    )


@dp.message(Command("stats"))
async def stats(message: Message):
    if not is_admin(message):
        return
    await message.answer(stats_text(), parse_mode="HTML")


# ---- user lists ----------------------------------------------------------

USERS_PER_PAGE = 10
FILTER_LABELS = {"all": "All", "active": "Active", "blocked": "Blocked", "muted": "Muted", "new": "New 24h",
                 "banned": "Banned"}


def _user_link(u: dict) -> str:
    name = html.escape(u.get("first_name") or "No name")
    uname = f" @{html.escape(u['username'])}" if u.get("username") else ""
    return f'<a href="tg://user?id={u["user_id"]}">{name}</a>{uname}'


def _user_icon(u: dict) -> str:
    if u.get("banned"):
        return "⛔"
    return "🚫" if u.get("is_blocked") else ("🔕" if not u.get("notify", 1) else "✅")


def render_users(flt: str, page: int):
    flt = flt if flt in FILTER_LABELS else "all"
    rows, total, page = get_users_page(flt, page, USERS_PER_PAGE)
    pages = max(1, -(-total // USERS_PER_PAGE))
    lines = [f"👥 <b>Users - {FILTER_LABELS[flt]}</b> ({total})", f"Page {page}/{pages}",
             "✅ active  🔕 muted  🚫 blocked bot  ⛔ banned", ""]
    if not rows:
        lines.append("No users here.")
    for i, u in enumerate(rows, start=(page - 1) * USERS_PER_PAGE + 1):
        if u.get("banned"):
            extra = f"banned {fmt_local(u.get('banned_at'), False)}"
        elif u["is_blocked"]:
            extra = f"blocked {fmt_local(u.get('blocked_at'), False)}"
        else:
            extra = f"joined {fmt_local(u['created_at'], False)}"
        lines.append(f"{i}. {_user_icon(u)} {_user_link(u)} · <code>{u['user_id']}</code> · {extra}")

    def btn(text, data):
        return InlineKeyboardButton(text=text, callback_data=data)

    filters = [btn(("• " if k == flt else "") + label, f"u:{k}:1") for k, label in FILTER_LABELS.items()]
    nav = []
    if page > 1:
        nav.append(btn("◀ Prev", f"u:{flt}:{page - 1}"))
    nav.append(btn(f"{page}/{pages}", "u:noop"))
    if page < pages:
        nav.append(btn("Next ▶", f"u:{flt}:{page + 1}"))
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=[filters[:3], filters[3:], nav])


async def _send_users(message: Message, flt: str):
    text, kb = render_users(flt, 1)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@dp.message(Command("users"))
async def users_command(message: Message, command: CommandObject):
    if not is_admin(message):
        return
    arg = (command.args or "all").strip().lower()
    await _send_users(message, arg if arg in FILTER_LABELS else "all")


@dp.message(Command("blocked"))
async def blocked_command(message: Message):
    if is_admin(message):
        await _send_users(message, "blocked")


@dp.message(Command("newusers"))
async def newusers_command(message: Message):
    if is_admin(message):
        await _send_users(message, "new")


@dp.callback_query(F.data.startswith("u:"))
async def users_callback(cb: CallbackQuery):
    if not is_admin_id(cb.from_user.id):
        await cb.answer("Admins only", show_alert=True)
        return
    parts = (cb.data or "").split(":")
    if len(parts) < 3 or parts[1] == "noop":
        await cb.answer()
        return
    try:
        text, kb = render_users(parts[1], int(parts[2]))
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except (ValueError, TelegramBadRequest):
        pass  # bad data, or "message is not modified"
    await cb.answer()


@dp.message(Command("user"))
async def user_lookup(message: Message, command: CommandObject):
    """/user 123456789  or  /user @username  or  /user Name"""
    if not is_admin(message):
        return
    if not command.args:
        await message.answer("Usage: <code>/user 123456789</code> or <code>/user @username</code>", parse_mode="HTML")
        return
    found = find_users(command.args)
    if not found:
        await message.answer("No user found.")
        return
    if len(found) > 1:
        await message.answer(
            "Several matches:\n\n" + "\n".join(
                f"{_user_icon(u)} {_user_link(u)} · <code>{u['user_id']}</code>" for u in found),
            parse_mode="HTML")
        return
    u = found[0]
    status = "⛔ Banned" if u.get("banned") else ("🚫 Blocked the bot" if u["is_blocked"] else "✅ Active")
    await message.answer(
        f"👤 {_user_link(u)}\n"
        f"ID: <code>{u['user_id']}</code>\n"
        f"Status: {status}\n"
        f"Notifications: {'on' if u['notify'] else 'muted'}\n"
        f"Joined: {fmt_local(u['created_at'])}\n"
        f"Last seen: {fmt_local(u['last_seen'])}\n"
        + (f"Blocked the bot: {fmt_local(u['blocked_at'])}\n" if u["is_blocked"] else "")
        + (f"Banned: {fmt_local(u.get('banned_at'))}\n" if u.get("banned") else "")
        + f"Last broadcast: {u['last_broadcast_status'] or '-'}",
        parse_mode="HTML",
    )


@dp.message(Command("export"))
async def export_users(message: Message, command: CommandObject):
    """/export [all|active|blocked|muted|new]"""
    if not is_admin(message):
        return
    flt = (command.args or "all").strip().lower()
    flt = flt if flt in FILTER_LABELS else "all"
    rows, _, _ = get_users_page(flt, 1, 10_000_000)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["user_id", "username", "first_name", "created_at", "last_seen",
                     "is_blocked", "blocked_at", "banned", "notify", "last_broadcast_at", "last_broadcast_status"])
    for m in rows:
        writer.writerow([m["user_id"], m["username"], m["first_name"], m["created_at"], m["last_seen"],
                         m["is_blocked"], m["blocked_at"] or "", m["banned"], m["notify"],
                         m["last_broadcast_at"] or "", m["last_broadcast_status"] or ""])
    data = ("\ufeff" + buf.getvalue()).encode("utf-8")  # BOM so Excel shows names correctly
    name = f"users-{flt}-{datetime.now(timezone.utc):%Y%m%d-%H%M}.csv"
    await message.answer_document(BufferedInputFile(data, filename=name),
                                  caption=f"Users export ({flt}): {len(rows)} rows")


@dp.message(Command("backup"))
async def backup(message: Message):
    if not is_admin(message):
        return
    tmp = Path(tempfile.gettempdir()) / f"atrangii-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.db"
    try:
        await asyncio.to_thread(backup_db, str(tmp))
        await message.answer_document(FSInputFile(tmp), caption="SQLite database backup")
    except Exception:
        log.exception("backup failed")
        await message.answer("Backup failed. Check the server logs.")
    finally:
        tmp.unlink(missing_ok=True)


@dp.message(Command("broadcast"))
async def broadcast_command(message: Message):
    if not is_admin(message):
        return

    admin_id = message.from_user.id

    # 1) /broadcast as a reply to any message -> broadcast that message right away.
    if message.reply_to_message:
        broadcast_waiting.discard(admin_id)
        await message.answer("Preparing broadcast...")
        src = message.reply_to_message
        await broadcast_message(src.chat.id, src.message_id, admin_id)
        return

    # 2) /broadcast Your text -> broadcast the text.
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip():
        broadcast_waiting.discard(admin_id)
        await message.answer("Preparing text broadcast...")
        source = await bot.send_message(admin_id, parts[1].strip())
        try:
            await broadcast_message(source.chat.id, source.message_id, admin_id)
        finally:
            try:
                await bot.delete_message(admin_id, source.message_id)
            except Exception:
                pass
        return

    # 3) plain /broadcast -> the next message you send becomes the broadcast.
    broadcast_waiting.add(admin_id)
    await message.answer(
        "Advanced Broadcast\n\n"
        "Send the message you want to broadcast now (text, photo, video, document...).\n\n"
        "Other ways:\n"
        "• Reply to any message with <code>/broadcast</code>\n"
        "• <code>/broadcast Hello everyone!</code>\n\n"
        "Send /cancel to abort.",
        parse_mode="HTML",
    )


@dp.message(Command("cancel"))
async def cancel_broadcast(message: Message):
    if not is_admin(message):
        return

    admin_id = message.from_user.id
    if admin_id in broadcast_waiting:
        broadcast_waiting.discard(admin_id)
        await message.answer("Broadcast cancelled.")
        return
    if admin_id in broadcast_running:
        broadcast_cancel.add(admin_id)
        await message.answer("Cancelling the running broadcast...")
        return
    await message.answer("There is no broadcast to cancel.")


# ---- admins ----------------------------------------------------------------

def _parse_target(arg):
    """Numeric Telegram ID, or @username / name of a user who has started the bot."""
    arg = (arg or "").strip()
    if not arg:
        return None
    if arg.lstrip("-").isdigit():
        return int(arg)
    found = find_users(arg)
    return found[0]["user_id"] if len(found) == 1 else None


def _who(user_id: int) -> str:
    u = get_user(user_id)
    return (f"{_user_link(u)} · " if u and (u.get("first_name") or u.get("username")) else "") + f"<code>{user_id}</code>"


@dp.message(Command("admins"))
async def admins_list(message: Message):
    if not is_admin(message):
        return
    owners = "\n".join(f"👑 {_who(i)}" for i in sorted(ADMIN_IDS)) or "none (set ADMIN_IDS)"
    extra = get_admin_rows()
    added = "\n".join(f"🛡 {_who(r['user_id'])} · added {fmt_local(r['added_at'], False)}" for r in extra) or "none"
    await message.answer(f"<b>Primary owners</b>\n{owners}\n\n<b>Admins</b>\n{added}", parse_mode="HTML")


@dp.message(Command("addadmin"))
async def add_admin_command(message: Message, command: CommandObject):
    if not is_admin(message):
        return
    if not is_owner(message):
        await message.answer("Only primary owners can add admins.")
        return
    uid = _parse_target(command.args)
    if uid is None:
        await message.answer("Usage: <code>/addadmin 123456789</code> (or @username of a user who started the bot)",
                             parse_mode="HTML")
        return
    if is_admin_id(uid):
        await message.answer("That user is already an admin.")
        return
    add_admin(uid, message.from_user.id)
    menu_ok = await set_admin_menu(uid)
    told = await safe_send(uid, "🛠 You are now an admin of this bot.\nSend /help to see the admin commands.")
    note = "" if (menu_ok and told == "sent") else "\nThey must press /start in the bot once to see the admin menu."
    await message.answer(f"✅ Added admin {_who(uid)}{note}", parse_mode="HTML")


@dp.message(Command("removeadmin"))
async def remove_admin_command(message: Message, command: CommandObject):
    if not is_admin(message):
        return
    if not is_owner(message):
        await message.answer("Only primary owners can remove admins.")
        return
    uid = _parse_target(command.args)
    if uid is None:
        await message.answer("Usage: <code>/removeadmin 123456789</code>", parse_mode="HTML")
        return
    if is_owner_id(uid):
        await message.answer("Primary owners (ADMIN_IDS) cannot be removed from inside the bot.")
        return
    if not remove_admin(uid):
        await message.answer("That user is not an admin.")
        return
    try:
        await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=uid))
    except Exception:
        pass
    await message.answer(f"✅ Removed admin {_who(uid)}", parse_mode="HTML")


# ---- moderation ------------------------------------------------------------

@dp.message(Command("ban"))
async def ban_command(message: Message, command: CommandObject):
    if not is_admin(message):
        return
    uid = _parse_target(command.args)
    if uid is None:
        await message.answer("Usage: <code>/ban 123456789</code>", parse_mode="HTML")
        return
    if is_admin_id(uid):
        await message.answer("Admins cannot be banned.")
        return
    ban_user(uid)
    await message.answer(
        f"⛔ Banned {_who(uid)}\nThey are ignored by the bot and skipped in broadcasts and notifications."
        + ("" if REQUIRE_TG_AUTH else "\nTip: set REQUIRE_TG_AUTH=true to also block them from the Mini App."),
        parse_mode="HTML")


@dp.message(Command("unban"))
async def unban_command(message: Message, command: CommandObject):
    if not is_admin(message):
        return
    uid = _parse_target(command.args)
    if uid is None:
        await message.answer("Usage: <code>/unban 123456789</code>", parse_mode="HTML")
        return
    await message.answer(f"✅ Unbanned {_who(uid)}" if unban_user(uid) else "That user is not banned.",
                         parse_mode="HTML")


@dp.message(Command("banned"))
async def banned_command(message: Message):
    if is_admin(message):
        await _send_users(message, "banned")


# ---- Mini App posts --------------------------------------------------------

@dp.message(Command("posts"))
async def posts_command(message: Message):
    if not is_admin(message):
        return
    rows = recent_posts(10)
    if not rows:
        await message.answer("No posts stored yet. Post something in the channel (bot must be admin there).")
        return
    lines = ["📰 <b>Last 10 posts in the Mini App</b>", ""]
    for r in rows:
        snippet = html.escape((r["caption"] or r["text"] or "")[:40].replace("\n", " "))
        lines.append(f"<code>{r['message_id']}</code> · {r['media_type'] or 'text'} · "
                     f"{fmt_local(r['posted_at'] or r['created_at'])}" + (f"\n    {snippet}" if snippet else ""))
    lines += ["", "Remove one from the Mini App: /delpost MESSAGE_ID"]
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("delpost"))
async def delpost_command(message: Message, command: CommandObject):
    if not is_admin(message):
        return
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Usage: <code>/delpost MESSAGE_ID</code> (IDs are listed by /posts)", parse_mode="HTML")
        return
    n = delete_post(int(arg))
    await message.answer(
        f"🗑 Removed {n} item(s) from the Mini App. The post in your channel is untouched." if n
        else "No stored post with that message ID.")


# ---- settings --------------------------------------------------------------

@dp.message(Command("notifications"))
async def notifications_command(message: Message, command: CommandObject):
    if not is_admin(message):
        return
    arg = (command.args or "").strip().lower()
    if arg in {"on", "off"}:
        set_setting("notifications", arg)
    state = get_setting("notifications", "on")
    extra = "" if NOTIFY_ENABLED else "\n⚠ NOTIFY_ENABLED=false in Railway variables, so nothing is sent anyway."
    await message.answer(
        f"New-post notifications: <b>{state.upper()}</b>{extra}\nChange: /notifications on | /notifications off",
        parse_mode="HTML")


@dp.message(Command("setwelcome"))
async def setwelcome_command(message: Message):
    if not is_admin(message):
        return
    parts = re.split(r"\s+", message.html_text or "", maxsplit=1)
    text = parts[1].strip() if len(parts) > 1 else ""
    if not text:
        await message.answer("Usage: <code>/setwelcome Your welcome text</code>\n"
                             "Bold, italic and links are kept. Preview is shown right away.", parse_mode="HTML")
        return
    try:
        await message.answer(text, parse_mode="HTML", reply_markup=main_keyboard())
    except TelegramBadRequest:
        await message.answer("That text has invalid formatting, so it was not saved.")
        return
    set_setting("welcome_text", text)
    await message.answer("✅ Welcome message saved (preview above).")


@dp.message(Command("resetwelcome"))
async def resetwelcome_command(message: Message):
    if is_admin(message):
        delete_setting("welcome_text")
        await message.answer("✅ Default welcome message restored.")


@dp.message(Command("status"))
async def status_command(message: Message):
    if not is_admin(message):
        return
    me = await bot.me()
    up = int(time.time() - START_TIME)
    s = member_stats(local_day_start_utc_iso())
    try:
        db_mb = os.path.getsize(DATABASE_PATH) / 1048576
    except OSError:
        db_mb = 0.0
    await message.answer(
        f"🟢 <b>Bot is running</b>\n\n"
        f"Bot: @{me.username}\n"
        f"Uptime: {up // 86400}d {up % 86400 // 3600}h {up % 3600 // 60}m\n"
        f"Time now: {fmt_local(datetime.now(timezone.utc).isoformat())}\n"
        f"Version: <code>{APP_VERSION}</code>\n\n"
        f"Users: <b>{s['total']}</b> (blocked {s['blocked']}, banned {s['banned']})\n"
        f"Posts stored: <b>{s['posts']}</b>\n"
        f"Database: {db_mb:.2f} MB\n"
        f"Notifications: <b>{get_setting('notifications', 'on').upper()}</b>\n"
        f"Mini App login required: <b>{'yes' if REQUIRE_TG_AUTH else 'no'}</b>\n"
        f"Admins: {len(admin_ids())}",
        parse_mode="HTML",
    )


@dp.message()
async def broadcast_waiting_message(message: Message):
    """After /broadcast, the next message from that admin becomes the broadcast."""
    admin_id = message.from_user.id if message.from_user else None
    if not admin_id or admin_id not in broadcast_waiting or not is_admin(message):
        return
    if message.text and message.text.startswith("/"):
        return  # another command: let it be (use /cancel to abort)

    broadcast_waiting.discard(admin_id)
    await message.answer("Preparing broadcast...")
    await broadcast_message(message.chat.id, message.message_id, admin_id)


# ---------------------------------------------------------------- channel

def extract_post(message: Message) -> dict:
    """Pull everything the Mini App needs out of a channel post."""
    text = message.text or ""
    caption = message.caption or ""
    media_type = file_id = mime = name = thumb = ""
    size = 0

    def thumb_of(obj):
        t = getattr(obj, "thumbnail", None)
        return t.file_id if t else ""

    if message.photo:
        p = message.photo[-1]
        media_type, file_id, size = "photo", p.file_id, p.file_size or 0
    elif message.video:
        v = message.video
        media_type, file_id, size = "video", v.file_id, v.file_size or 0
        mime, name, thumb = v.mime_type or "", v.file_name or "", thumb_of(v)
    elif message.animation:
        a = message.animation
        media_type, file_id, size = "animation", a.file_id, a.file_size or 0
        mime, name, thumb = a.mime_type or "", a.file_name or "", thumb_of(a)
    elif message.document:
        d = message.document
        media_type, file_id, size = "document", d.file_id, d.file_size or 0
        mime, name, thumb = d.mime_type or "", d.file_name or "", thumb_of(d)
    elif message.audio:
        a = message.audio
        media_type, file_id, size = "audio", a.file_id, a.file_size or 0
        mime, name, thumb = a.mime_type or "", a.file_name or a.title or "", thumb_of(a)
    elif message.voice:
        v = message.voice
        media_type, file_id, size = "voice", v.file_id, v.file_size or 0
        mime = v.mime_type or ""
    elif message.video_note:
        v = message.video_note
        media_type, file_id, size = "video_note", v.file_id, v.file_size or 0
        thumb = thumb_of(v)

    html_text = message.html_text if message.text else ""
    html_caption = html_decoration.unparse(caption, message.caption_entities or []) if caption else ""

    posted_at = ""
    if message.date:
        d = message.date if message.date.tzinfo else message.date.replace(tzinfo=timezone.utc)
        posted_at = d.astimezone(timezone.utc).isoformat()

    return dict(
        text=text, media_type=media_type, file_id=file_id, caption=caption,
        html_text=html_text, html_caption=html_caption,
        media_group_id=message.media_group_id or "", mime_type=mime, file_name=name,
        file_size=size, thumb_id=thumb, posted_at=posted_at,
        channel_username=message.chat.username or "",
    )


def _from_main_channel(message: Message) -> bool:
    return not MAIN_CHANNEL_ID or str(message.chat.id) == str(MAIN_CHANNEL_ID)


# Notification batching: an album (or several quick posts) becomes ONE notification.
_pending_posts: set = set()
_notify_task: asyncio.Task | None = None


def queue_notification(key):
    global _notify_task
    if not NOTIFY_ENABLED or get_setting("notifications", "on") == "off":
        return
    _pending_posts.add(key)
    if _notify_task is None or _notify_task.done():
        _notify_task = asyncio.create_task(_notify_worker(), name="notify-users")


async def _notify_worker():
    try:
        while True:
            await asyncio.sleep(NOTIFY_DELAY)
            count = len(_pending_posts)
            _pending_posts.clear()
            if not count:
                return

            users = get_notify_users()
            if not users:
                continue
            title = "1 New Post Uploaded!" if count == 1 else f"{count} New Posts Uploaded!"
            text = f"🔥 {title} ✅\n\n👇 Tap below to view"
            sent = blocked = 0
            for uid in users:
                status = await safe_send(uid, text, reply_markup=main_keyboard(), parse_mode="HTML")
                if status == "sent":
                    sent += 1
                elif status == "blocked":
                    blocked += 1
                    set_user_blocked(uid, True)
                await asyncio.sleep(0.05)
            log.info("Notified %s users (%s blocked) about %s new post(s)", sent, blocked, count)
    except Exception:
        log.exception("notification worker crashed")


@dp.channel_post()
async def channel_post(message: Message):
    # The bot must be an administrator of the main channel to receive these updates.
    if not _from_main_channel(message):
        return
    fields = extract_post(message)
    is_new = add_post(message.chat.id, message.message_id, **fields)
    if is_new:
        queue_notification(message.media_group_id or f"m{message.message_id}")


@dp.edited_channel_post()
async def edited_channel_post(message: Message):
    """Keep the Mini App in sync when a post is edited in the channel."""
    if not _from_main_channel(message):
        return
    update_post(message.chat.id, message.message_id, **extract_post(message))


# ------------------------------------------------------------------ startup

async def _setup_commands():
    try:
        await bot.set_my_commands(_commands(USER_COMMANDS))
    except Exception:
        log.warning("Could not set bot commands", exc_info=True)
    for admin_id in admin_ids():
        await set_admin_menu(admin_id)


async def _daily_report_loop():
    hour, minute = DAILY_REPORT_TIME
    while True:
        now = local_now()
        nxt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        try:
            title = f"🗓 <b>Daily report - {local_now():%d %b %Y} ({TZ_LABEL})</b>\n\n"
            for admin_id in admin_ids():
                await safe_send(admin_id, title + stats_text(), parse_mode="HTML")
        except Exception:
            log.exception("daily report failed")


async def main():
    init_db()
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS is empty: nobody is a primary owner, so /addadmin cannot be used.")
    if not MAIN_CHANNEL_ID:
        log.warning("MAIN_CHANNEL_ID is empty: posts from ANY channel the bot is admin of will be stored.")
    report_task = None
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await _setup_commands()
        if DAILY_REPORT_TIME and admin_ids():
            report_task = asyncio.create_task(_daily_report_loop(), name="daily-report")
        await dp.start_polling(
            bot,
            allowed_updates=["message", "callback_query", "channel_post", "edited_channel_post", "my_chat_member"],
            handle_signals=False,  # uvicorn (app/main.py) owns SIGTERM/SIGINT
        )
    finally:
        if report_task:
            report_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
