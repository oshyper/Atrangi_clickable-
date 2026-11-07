import asyncio
import logging
import os

import uvicorn

from app.bot import main as bot_main
from app.db import init_db


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx/httpcore log full request URLs at INFO level, and the Telegram URL contains
    # the bot token. Keep them quiet so the token can never leak into Railway logs.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def main():
    setup_logging()
    init_db()

    port = int(os.getenv("PORT", "8000"))
    server = uvicorn.Server(uvicorn.Config(
        "app.web:app", host="0.0.0.0", port=port, log_level="info",
    ))

    bot_task = asyncio.create_task(bot_main(), name="bot")
    web_task = asyncio.create_task(server.serve(), name="web")

    # If either half stops (crash or SIGTERM), stop the other one too and exit,
    # so Railway restarts the whole service instead of leaving half of it dead.
    done, pending = await asyncio.wait({bot_task, web_task}, return_when=asyncio.FIRST_COMPLETED)
    server.should_exit = True
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    for task in done:
        task.result()  # re-raise the original error, if any


if __name__ == "__main__":
    asyncio.run(main())
