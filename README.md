# Atrangii Telegram Bot + Mini App (Railway)

One Railway service runs both the Telegram bot (long polling) and the Mini App web server,
with one persistent SQLite volume.

## Railway deployment

1. Push this project to a GitHub repo (or use the Railway CLI).
2. Railway → **New Project → Deploy from GitHub Repo**. The `Dockerfile` / `railway.json` are picked up automatically.
3. **Settings → Networking → Generate Domain**. `APP_URL` is optional on Railway: if you leave it empty, `RAILWAY_PUBLIC_DOMAIN` is used automatically.
4. **Volumes → Add Volume**, mount path `/app/data` (otherwise your users/posts are lost on every redeploy).
5. **Variables**:
   | Variable | Value |
   |---|---|
   | `BOT_TOKEN` | token from @BotFather (**required**) |
   | `MAIN_CHANNEL_ID` | numeric channel ID, e.g. `-1001234567890` |
   | `ADMIN_IDS` | your numeric Telegram ID (comma-separated for several admins) |
   | `DATABASE_PATH` | `/app/data/bot.db` |
   | `BACKUP_CHANNEL_URL` | `https://t.me/Atrangii_re_new` |
   | `REQUIRE_TG_AUTH` | `true` recommended (see Security) |
6. Add the bot as an **administrator** of the main channel (needed to receive channel posts).
7. @BotFather → your bot → Main Mini App / Web App URL = your Railway HTTPS domain.

## Features

**Mini App**
- Infinite scroll (loads older posts as you scroll), auto-check for new posts every 30 s with a "↑ New posts" button, manual refresh button
- Albums are shown as one post with a photo grid; tap a photo to view it full-screen
- Photos, videos (seeking works), GIFs, audio, voice, video notes and files
- Files over 20 MB (Telegram Bot API limit) show an "Open in Telegram" button instead of a broken player
- Follows the user's Telegram theme (light/dark), keeps links/bold/italic/quotes from the channel post
- Real post time (the time it was posted in the channel), "5m ago" style

**Bot**
- Edited channel posts are updated in the Mini App
- New-post notifications are batched: an album or several quick posts = ONE notification
- Users can `/mute` and `/unmute` notifications; blocked users are skipped automatically
- Real-time block/unblock detection (`my_chat_member`)
- Admin panel with a full command menu, user management, ban/unban, add admins inside the bot, editable welcome message, notification switch, IST dates (see below)
- Optional instant admin alerts (`ADMIN_ALERTS=true`) and daily report (`DAILY_REPORT_TIME=21:00`)
- Rate-limit (flood) handling on broadcasts and notifications

## Admin commands (all of them are also in the bot's **Menu** button)

Send `/help` to the bot as an admin to see the full panel. All dates are shown in **IST** (change with `TIMEZONE_OFFSET`).

| Group | Command | What it does |
|---|---|---|
| Users | `/stats` | total started, active, blocked, muted, banned, joined today (IST) / 24h / 7d / 30d, blocked in 7d, active 24h/7d, posts |
| | `/users [all\|active\|blocked\|muted\|new\|banned]` | browse users with buttons (filters + Prev/Next) |
| | `/blocked`, `/newusers` | users who blocked the bot / joined in the last 24h |
| | `/user ID\|@username` | full details of one user |
| | `/export [filter]` | users as CSV (`/export blocked`, ...) |
| Broadcast | `/broadcast` | then send any message; or reply to a message with `/broadcast`; or `/broadcast text` |
| | `/cancel` | cancel a running broadcast |
| Moderation | `/ban ID`, `/unban ID`, `/banned` | banned users are ignored by the bot, skipped in broadcasts/notifications, and (with `REQUIRE_TG_AUTH=true`) cannot open the Mini App |
| Mini App | `/posts` | last 10 stored posts with their message IDs |
| | `/delpost MESSAGE_ID` | remove a post (whole album) from the Mini App. Telegram bots cannot see channel deletions, so use this for posts you deleted in the channel |
| Settings | `/notifications on\|off` | switch new-post notifications without redeploying |
| | `/setwelcome TEXT`, `/resetwelcome` | change the welcome message (bold/italic/links are kept) |
| Access | `/admins` | list admins |
| | `/addadmin ID`, `/removeadmin ID` | add/remove admins from inside the bot (**primary owners only**) |
| Info | `/status` | bot username, uptime, version, users, DB size, settings |
| | `/backup` | consistent copy of the SQLite database - **use it regularly** |

**Primary owners** are the IDs in the `ADMIN_IDS` variable. They can add/remove other admins with `/addadmin` and `/removeadmin`; added admins have every command except those two. Admins added in the bot are stored in the database (keep the Railway volume!).

Optional extras: `ADMIN_ALERTS=true` (instant message when someone starts / blocks / unblocks the bot) and `DAILY_REPORT_TIME=21:00` (daily stats report to every admin, IST).

## About "blocked" numbers

Telegram tells the bot about a block in real time (`my_chat_member`), and also when a broadcast/notification fails with "Forbidden". Users who blocked the bot *before* this version was installed are detected the first time a broadcast or notification is sent to them, so run one broadcast to refresh the count.

## Security

- Never publish your BotFather token. If it ever leaked, revoke it in @BotFather (`/revoke`).
- `REQUIRE_TG_AUTH=true` makes the server verify Telegram's signed `initData`, so the posts API and media can only be opened from inside Telegram (not by someone who just guesses your URL). With it on, opening the URL in a normal browser shows "Please open this app from the Telegram bot" — that is expected.
- The bot token is never logged (HTTP client logging is silenced on purpose).

## Important

- Keep exactly ONE running copy of the bot with this token (no local copy while Railway runs), otherwise Telegram returns `Conflict: terminated by other getUpdates request`.
- Posts are captured only after the bot is installed; old channel history is not imported.
- For very high traffic consider Postgres + object storage/CDN.

## Local run

```bash
pip install -r requirements.txt
cp .env.example .env      # fill BOT_TOKEN and an HTTPS APP_URL (e.g. an ngrok URL)
python -m app.main
```

## Tests

```bash
python -m unittest discover -s tests -v
```
