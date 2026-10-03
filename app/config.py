import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
APP_URL = os.getenv("APP_URL", "").rstrip("/")
MAIN_CHANNEL_ID = os.getenv("MAIN_CHANNEL_ID", "").strip()
BACKUP_CHANNEL_URL = os.getenv("BACKUP_CHANNEL_URL", "https://t.me/Atrangii_re_new").strip()
DATABASE_PATH = os.getenv("DATABASE_PATH", "./data/bot.db")
ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Copy .env.example to .env and set it.")
if not APP_URL.startswith("https://"):
    raise RuntimeError("APP_URL must be an HTTPS URL. Telegram Mini Apps require HTTPS.")
