"""
One-time interactive auth script — uses telethon.sync for robust DC migration handling.

Run once with TTY to generate the session file:

  docker run --rm -it \\
    -v ~/homelab/apps/telegram-watch/data:/data \\
    --env TELEGRAM_API_ID=<id> \\
    --env TELEGRAM_API_HASH=<hash> \\
    --env TELEGRAM_SESSION_FILE=/data/telegram.session \\
    telegram-sentinel:local python auth.py
"""
import os
import sys

# telethon.sync handles DC migration transparently — more reliable than async for initial auth
from telethon.sync import TelegramClient

API_ID       = int(os.environ["TELEGRAM_API_ID"])
API_HASH     = os.environ["TELEGRAM_API_HASH"]
SESSION_FILE = os.environ.get("TELEGRAM_SESSION_FILE", "/data/telegram.session")

print(f"Session path : {SESSION_FILE}")
print(f"API ID       : {API_ID}")
print()

try:
    with TelegramClient(SESSION_FILE, API_ID, API_HASH) as client:
        # start() prompts for phone, OTP, and cloud password (2FA) automatically
        client.start()
        me = client.get_me()
        if me is None:
            print("❌ Auth failed — get_me() returned None")
            sys.exit(1)
        print(f"\n✅ Authenticated as: {me.first_name} ({me.username})")
        print(f"   Session saved to : {SESSION_FILE}")
        print(f"   DC               : {client.session.dc_id}")
        # Verify the key is accepted by the server
        from telethon.tl.functions.updates import GetStateRequest
        state = client(GetStateRequest())
        print(f"   Server confirmed : unread={state.unread_count} mentions={state.unread_mentions_count}")
        print("\n✅ Session is valid and registered with Telegram servers.")
except Exception as exc:
    print(f"\n❌ Auth error: {type(exc).__name__}: {exc}")
    sys.exit(1)
