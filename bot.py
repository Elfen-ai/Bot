# bot.py
import asyncio
import os
import sys

from telegram.ext import ApplicationBuilder
from features.link_cmd import register_handlers
from utils.shutdown import reset_shutdown_timer

BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")


async def main():
    if not BOT_TOKEN:
        print("❌ TELEGRAM_TOKEN tidak ditemukan di Secret!")
        sys.exit(1)

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # registrasi fitur (link generator)
    register_handlers(app)

    print("🤖 Bot aktif! Siap menerima perintah di Telegram.")

    # start initial shutdown timer (async)
    await reset_shutdown_timer()

    # run polling (await)
    await app.run_polling()


if __name__ == "__main__":
    # Compatibility with hosted runners that may already run an event loop
    try:
        import nest_asyncio
        nest_asyncio.apply()
        asyncio.get_event_loop().run_until_complete(main())
    except ModuleNotFoundError:
        asyncio.run(main())