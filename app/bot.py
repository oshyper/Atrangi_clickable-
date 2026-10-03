import asyncio
import time

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError, TelegramBadRequest

from app.config import BOT_TOKEN, APP_URL, BACKUP_CHANNEL_URL, MAIN_CHANNEL_ID, ADMIN_IDS
from app.db import (
    init_db, upsert_user, add_post, latest_message_id, user_count, conn,
    get_broadcast_users, mark_user_broadcast, member_stats
)

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# Admins who have sent /broadcast and are waiting to send/reply with the message.
broadcast_waiting = set()
broadcast_cancel = set()


def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 Open Channel", web_app=WebAppInfo(url=APP_URL))],
        [InlineKeyboardButton(text="📢 Backup Channel", url=BACKUP_CHANNEL_URL)]
    ])


def is_admin(message: Message) -> bool:
    return bool(ADMIN_IDS) and message.from_user and message.from_user.id in ADMIN_IDS


async def send_broadcast_report(admin_id: int, total: int, sent: int, failed: int, cancelled: bool = False):
    status = "🛑 Cancelled" if cancelled else "✅ Completed"
    await bot.send_message(
        admin_id,
        f"📢 <b>Broadcast {status}</b>\n\n"
        f"👥 Total: <b>{total}</b>\n"
        f"✅ Sent: <b>{sent}</b>\n"
        f"❌ Failed: <b>{failed}</b>",
        parse_mode="HTML"
    )


async def broadcast_message(source: Message, admin_id: int):
    """Copy the source message to every user who has started the bot."""
    with conn() as c:
        users = get_broadcast_users()

    total = len(users)
    sent = 0
    failed = 0
    broadcast_cancel.discard(admin_id)

    if total == 0:
        await bot.send_message(admin_id, "👥 Abhi koi registered user nahi hai.")
        return

    progress_msg = await bot.send_message(
        admin_id,
        f"📢 <b>Broadcast started...</b>\n\n"
        f"👥 Total users: <b>{total}</b>\n"
        f"⏳ Sent: <b>0</b> | ❌ Failed: <b>0</b>\n\n"
        f"🛑 Cancel: /cancel",
        parse_mode="HTML"
    )

    for index, user_id in enumerate(users, start=1):
        if admin_id in broadcast_cancel:
            await send_broadcast_report(admin_id, total, sent, failed, cancelled=True)
            broadcast_cancel.discard(admin_id)
            return

        try:
            # copy_message preserves the original text/caption and media.
            await bot.copy_message(
                chat_id=user_id,
                from_chat_id=source.chat.id,
                message_id=source.message_id,
            )
            sent += 1
            mark_user_broadcast(user_id, "sent")

        except TelegramRetryAfter as e:
            # Respect Telegram's rate limit and retry this user.
            await asyncio.sleep(float(e.retry_after) + 0.5)
            try:
                await bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=source.chat.id,
                    message_id=source.message_id,
                )
                sent += 1
                mark_user_broadcast(user_id, "sent")
            except TelegramForbiddenError:
                mark_user_broadcast(user_id, "blocked")
                failed += 1
            except Exception:
                mark_user_broadcast(user_id, "failed")
                failed += 1

        except TelegramForbiddenError:
            # Telegram reports the bot as blocked / inaccessible.
            mark_user_broadcast(user_id, "blocked")
            failed += 1

        except TelegramBadRequest:
            # Invalid/deleted chat or another Telegram-side failure.
            mark_user_broadcast(user_id, "failed")
            failed += 1

        except Exception:
            mark_user_broadcast(user_id, "failed")
            failed += 1

        # Keep comfortably below Telegram's global send rate.
        await asyncio.sleep(0.05)

        if index == 1 or index % 25 == 0 or index == total:
            try:
                await progress_msg.edit_text(
                    f"📢 <b>Broadcast in progress...</b>\n\n"
                    f"👥 Total: <b>{total}</b>\n"
                    f"📨 Processed: <b>{index}/{total}</b>\n"
                    f"✅ Sent: <b>{sent}</b>\n"
                    f"❌ Failed: <b>{failed}</b>\n\n"
                    f"🛑 Cancel: /cancel",
                    parse_mode="HTML"
                )
            except Exception:
                pass

    await send_broadcast_report(admin_id, total, sent, failed)


@dp.message(CommandStart())
async def start(message: Message):
    u = message.from_user
    upsert_user(u.id, u.username, u.first_name)
    await message.answer(
        "🔥 <b>Welcome!</b> ✅\n\n"
        "👇 Post dekhne ke liye <b>Open Channel</b> par click karo 😉♦️",
        reply_markup=main_keyboard(),
        parse_mode="HTML"
    )


@dp.message(Command("stats"))
async def stats(message: Message):
    if not is_admin(message):
        return
    s = member_stats()
    await message.answer(
        "📊 <b>Member Statistics</b>\n\n"
        f"👥 Total registered: <b>{s['total']}</b>\n"
        f"🟢 Active: <b>{s['active']}</b>\n"
        f"🔴 Blocked/inaccessible: <b>{s['blocked']}</b>\n"
        f"🔥 Active in last 7 days: <b>{s['active_7d']}</b>",
        parse_mode="HTML"
    )


@dp.message(Command("broadcast"))
async def broadcast_command(message: Message):
    if not is_admin(message):
        return

    admin_id = message.from_user.id
    broadcast_waiting.add(admin_id)

    # If /broadcast is a reply to a message, broadcast it immediately.
    if message.reply_to_message:
        broadcast_waiting.discard(admin_id)
        await bot.send_message(
            admin_id,
            "⏳ Broadcast prepare ho raha hai..."
        )
        await broadcast_message(message.reply_to_message, admin_id)
        return

    # Support /broadcast Your text directly.
    text = (message.text or "").split(maxsplit=1)
    if len(text) > 1 and text[1].strip():
        broadcast_waiting.discard(admin_id)
        await bot.send_message(admin_id, "⏳ Text broadcast prepare ho raha hai...")
        source = await bot.send_message(admin_id, text[1].strip())
        await broadcast_message(source, admin_id)
        try:
            await bot.delete_message(admin_id, source.message_id)
        except Exception:
            pass
        return

    await message.answer(
        "📢 <b>Advanced Broadcast</b>\n\n"
        "Broadcast karne ke 2 tarike:\n\n"
        "1️⃣ Kisi <b>photo/video/document/text</b> ko reply karke "
        "<code>/broadcast</code> bhejo.\n\n"
        "2️⃣ Direct text ke liye:\n"
        "<code>/broadcast Hello everyone!</code>\n\n"
        "🛑 Broadcast cancel karne ke liye <code>/cancel</code> bhejo.",
        parse_mode="HTML"
    )


@dp.message(Command("cancel"))
async def cancel_broadcast(message: Message):
    if not is_admin(message):
        return

    admin_id = message.from_user.id
    if admin_id in broadcast_waiting:
        broadcast_waiting.discard(admin_id)
        await message.answer("❌ Broadcast message selection cancel kar di gayi.")
        return

    broadcast_cancel.add(admin_id)
    await message.answer("🛑 Running broadcast ko cancel karne ka request bhej diya gaya hai.")


@dp.message()
async def broadcast_waiting_message(message: Message):
    """After /broadcast, the next message from that admin becomes the broadcast."""
    admin_id = message.from_user.id if message.from_user else None
    if not admin_id or admin_id not in broadcast_waiting or not is_admin(message):
        return

    # Ignore another command while waiting, except cancel.
    if message.text and message.text.startswith("/"):
        return

    broadcast_waiting.discard(admin_id)
    await bot.send_message(admin_id, "⏳ Broadcast prepare ho raha hai...")
    await broadcast_message(message, admin_id)


@dp.channel_post()
async def channel_post(message: Message):
    # Add the bot as an administrator to your main channel first.
    if MAIN_CHANNEL_ID and str(message.chat.id) != str(MAIN_CHANNEL_ID):
        return

    text = message.text or ""
    caption = message.caption or ""
    media_type, file_id = "", ""

    if message.photo:
        media_type, file_id = "photo", message.photo[-1].file_id
    elif message.video:
        media_type, file_id = "video", message.video.file_id
    elif message.animation:
        media_type, file_id = "animation", message.animation.file_id
    elif message.document:
        media_type, file_id = "document", message.document.file_id

    html_text = message.html_text if message.text else ""
    html_caption = message.html_caption if message.caption else ""
    add_post(message.chat.id, message.message_id, text, media_type, file_id, caption, html_text, html_caption)

    # Notify users about the new post.
    with conn() as c:
        users = [r["user_id"] for r in c.execute(
            "SELECT user_id FROM users"
        ).fetchall()]

    if users:
        msg = (
            "🔥 <b>1 New Post Uploaded!</b> ✅\n\n"
            "👇 <b>Tap below to view</b>"
        )
        for uid in users:
            try:
                await bot.send_message(
                    uid, msg, reply_markup=main_keyboard(), parse_mode="HTML"
                )
            except Exception:
                pass


async def main():
    init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=["message", "channel_post"])


if __name__ == "__main__":
    asyncio.run(main())
