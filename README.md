# Atrangii Telegram Bot + Mini App — Railway

This version is designed for ONE Railway service running both the Telegram bot and Mini App web server, with one persistent SQLite volume.

## Railway deployment

1. Put this project in a GitHub repository, or deploy with Railway CLI.
2. Railway -> New Project -> Deploy from GitHub Repo.
3. Select the repository. Railway detects the Dockerfile and builds it.
4. In the service Variables tab, add:
   - BOT_TOKEN = your @BotFather token
   - APP_URL = the HTTPS Railway domain you will generate
   - MAIN_CHANNEL_ID = your main channel numeric ID
   - BACKUP_CHANNEL_URL = https://t.me/Atrangii_re_new
   - ADMIN_IDS = your Telegram numeric ID (optional)
   - DATABASE_PATH = /app/data/bot.db
5. In Settings -> Networking, generate a public domain.
6. Put that generated https://... URL into APP_URL and redeploy.
7. Add @Atrangii_re_bot as an administrator in the main channel.
8. In @BotFather, configure the bot's Main Mini App / Web App URL as the same Railway HTTPS URL.
9. In Railway Volumes, add a volume mounted at /app/data.
10. Open https://YOUR-RAILWAY-DOMAIN/ to check the Mini App.

## Important

- Never publish your BotFather token.
- The bot must be an administrator in the main channel to receive channel-post updates.
- The private invite link is not enough for the bot to read channel posts.
- This project captures posts arriving after the bot is installed; it does not import arbitrary old channel history.
- For high traffic, consider Postgres + object storage/CDN and server-side Telegram Mini App initData validation.

## Local run

```bash
pip install -r requirements.txt
cp .env.example .env
python -m app.main
```


## Member management

The bot keeps a SQLite member record for users who start/interact with it. It records:
- first registration time and last seen time
- username and first name
- whether a broadcast detected the user as blocked/inaccessible
- when the block was detected
- last broadcast time and result

Admin command:
- `/stats` — total, active, blocked/inaccessible, and users active in the last 7 days.

A successful interaction or `/start` clears a previously detected blocked flag. The bot does not receive a real-time "user blocked bot" event; blocked status is detected when Telegram rejects a message sent to that user.
