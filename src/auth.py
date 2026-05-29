"""
One-time interactive auth script.
Run once with TTY to generate the session file:

  docker run --rm -it \\
    -v /opt/appdata/telegram-watch/data:/data \\
    --env TELEGRAM_API_ID=<id> \\
    --env TELEGRAM_API_HASH=<hash> \\
    --env TELEGRAM_SESSION_FILE=/data/telegram.session \\
    telegram-sentinel:local python auth.py
"""
import asyncio
import os
from telethon.sync import TelegramClient

API_ID       = int(os.environ["TELEGRAM_API_ID"])
API_HASH     = os.environ["TELEGRAM_API_HASH"]
SESSION_FILE = os.environ.get("TELEGRAM_SESSION_FILE", "/data/telegram.session")


async def main():
    client = TelegramClient(SESSION_FILE, API_ID, API_HASH)
    await client.start()
    me = await client.get_me()
    print(f"\n✅ Authenticated as: {me.first_name} ({me.username})")
    print(f"   Session saved to: {SESSION_FILE}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
