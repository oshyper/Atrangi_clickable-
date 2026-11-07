import asyncio
import csv
import io
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand, BotCommandScopeChat, BufferedInputFile, ChatMemberUpdated, FSInputFile,
    InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo,
)
from aiogram.utils.text_decorations import html_decoration

from app.config import (
    ADMIN_IDS, APP_URL, BACKUP_CHANNEL_URL, BOT_TOKEN, MAIN_CHANNEL_ID,
    NOTIFY_DELAY, NOTIFY_ENABLED,
)
from app.db import (
    add_post, backup_db, get_broadcast_users, get_members, get_notify_users, init_db,
    mark_user_broadcast, member_stats, set_notify, set_user_blocked, update_post, upsert_user,
)

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

def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Open Channel", web_app=WebAppInfo(url=APP_URL))],
        [InlineKeyboardButton(text="Backup Channel", url=BACKUP_CHANNEL_URL)],
    ])


def is_admin(message: Message) -> bool:
    return bool(ADMIN_IDS) and bool(message.from_user) and message.from_user.id in ADMIN_IDS


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

@dp.message(CommandStart())
async def start(message: Message):
    u = message.from_user
    upsert_user(u.id, u.username, u.first_name)
    await message.answer(
        "🔥 Welcome! ✅\n\n"
        "👇 Post dekhne ke liye <b>Open Channel</b> par click karo 😉♦️",
        reply_markup=main_keyboard(),
        parse_mode="HTML",
    )


@dp.message(Command("help"))
async def help_command(message: Message):
    lines = [
        "<b>Commands</b>",
        "/start - open the channel",
        "/mute - stop new-post notifications",
        "/unmute - turn notifications back on",
    ]
    if is_admin(message):
        lines += [
            "",
            "<b>Admin</b>",
            "/stats - member statistics",
            "/broadcast - send a message to all users",
            "/cancel - cancel a broadcast",
            "/export - download users as CSV",
            "/backup - download a copy of the database",
        ]
    await message.answer("\n".join(lines), parse_mode="HTML")


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
    if status in {"kicked", "left"}:
        set_user_blocked(event.chat.id, True)
    elif status == "member":
        set_user_blocked(event.chat.id, False)


# ------------------------------------------------------------ admin commands

@dp.message(Command("stats"))
async def stats(message: Message):
    if not is_admin(message):
        return
    s = member_stats()
    await message.answer(
        "Member Statistics\n\n"
        f"Total registered: <b>{s['total']}</b>\n"
        f"Active: <b>{s['active']}</b>\n"
        f"Blocked/inaccessible: <b>{s['blocked']}</b>\n"
        f"Muted notifications: <b>{s['muted']}</b>\n"
        f"Active in last 7 days: <b>{s['active_7d']}</b>\n"
        f"Posts stored: <b>{s['posts']}</b>",
        parse_mode="HTML",
    )


@dp.message(Command("export"))
async def export_users(message: Message):
    if not is_admin(message):
        return
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["user_id", "username", "first_name", "created_at", "last_seen",
                     "is_blocked", "notify", "last_broadcast_at", "last_broadcast_status"])
    for m in get_members(limit=1_000_000):
        writer.writerow([m["user_id"], m["username"], m["first_name"], m["created_at"],
                         m["last_seen"], m["is_blocked"], m["notify"],
                         m["last_broadcast_at"] or "", m["last_broadcast_status"] or ""])
    data = ("\ufeff" + buf.getvalue()).encode("utf-8")  # BOM so Excel shows names correctly
    name = f"users-{datetime.now(timezone.utc):%Y%m%d-%H%M}.csv"
    await message.answer_document(BufferedInputFile(data, filename=name), caption="Users export")


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
    if not NOTIFY_ENABLED:
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
        await bot.set_my_commands([
            BotCommand(command="start", description="Open the channel"),
            BotCommand(command="mute", description="Stop new-post notifications"),
            BotCommand(command="unmute", description="Turn notifications back on"),
            BotCommand(command="help", description="Show help"),
        ])
        for admin_id in ADMIN_IDS:
            try:
                await bot.set_my_commands(
                    [
                        BotCommand(command="start", description="Open the channel"),
                        BotCommand(command="stats", description="Member statistics"),
                        BotCommand(command="broadcast", description="Message all users"),
                        BotCommand(command="cancel", description="Cancel a broadcast"),
                        BotCommand(command="export", description="Download users CSV"),
                        BotCommand(command="backup", description="Download database backup"),
                        BotCommand(command="mute", description="Stop notifications"),
                        BotCommand(command="unmute", description="Turn notifications on"),
                        BotCommand(command="help", description="Show help"),
                    ],
                    scope=BotCommandScopeChat(chat_id=admin_id),
                )
            except Exception:
                pass  # the admin has not started the bot yet
    except Exception:
        log.warning("Could not set bot commands", exc_info=True)


async def main():
    init_db()
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS is empty: /stats, /broadcast, /export and /backup are disabled.")
    if not MAIN_CHANNEL_ID:
        log.warning("MAIN_CHANNEL_ID is empty: posts from ANY channel the bot is admin of will be stored.")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await _setup_commands()
        await dp.start_polling(
            bot,
            allowed_updates=["message", "channel_post", "edited_channel_post", "my_chat_member"],
            handle_signals=False,  # uvicorn (app/main.py) owns SIGTERM/SIGINT
        )
    finally:
        await bot.session.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
