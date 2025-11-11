# balance_checker_helper.py
# Service name on Render: twxnhelp
# Exposes: POST /check_balance
# Body: {"number":"4034461234567890", "exp":"12/27", "code":"123"}
# Returns: {"status":"ok","balance":25.00} or {"status":"invalid","balance":0.0}

import os
import re
import asyncio
from decimal import Decimal
from typing import Optional

from flask import Flask, request, jsonify

# Telethon
from telethon import TelegramClient, events
from telethon.errors.rpcerrorlist import PhoneCodeInvalidError, SessionPasswordNeededError

# ========= Config =========
API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))          # required
API_HASH = os.getenv("TELEGRAM_API_HASH", "")            # required
PHONE_NUMBER = os.getenv("TELEGRAM_PHONE", "+12149312105")
SESSION_DIR = os.getenv("SESSION_DIR", "/data/telethon")
SESSION_NAME = os.getenv("SESSION_NAME", "checker")

CHECK_BOT = os.getenv("CHECK_BOT", "AllGccCheckersBot")  # no leading @ needed

# Optional DB update hook (same DB as main bot, if you want centralized updates here too)
DATABASE_URL = os.getenv("DATABASE_URL")  # e.g. postgresql://...

# ========= Setup =========
os.makedirs(SESSION_DIR, exist_ok=True)
SESSION_PATH = os.path.join(SESSION_DIR, SESSION_NAME)

app = Flask(__name__)

# Telethon client runs in an asyncio loop
loop = asyncio.get_event_loop()
client = TelegramClient(SESSION_PATH, API_ID, API_HASH, loop=loop)


def parse_balance_from_text(text: str) -> Optional[Decimal]:
    """
    Very tolerant parser. Looks for a money amount like $12.34 or 12.34
    """
    if not text:
        return None
    # common patterns
    m = re.search(r'(\$?\s*\d+(?:[\.,]\d{2})?)', text)
    if not m:
        return None
    raw = m.group(1).replace("$", "").strip()
    raw = raw.replace(",", "")
    raw = raw.replace(" ", "")
    raw = raw.replace("USD", "")
    # normalize comma/period usage
    if raw.count(".") > 1 and raw.count(",") == 0:
        # weird case, fallback
        raw = raw.replace(".", "")
    elif raw.count(",") == 1 and raw.count(".") == 0:
        raw = raw.replace(",", ".")
    try:
        return Decimal(raw)
    except Exception:
        return None


async def ensure_connected():
    if not client.is_connected():
        await client.connect()
    # If not authorized, you must authorize once (locally or via Render Shell).
    if not await client.is_user_authorized():
        # Prefer running this LOCALLY once to generate the session file, then deploy it to Render.
        # On Render shell you can also complete login interactively.
        raise RuntimeError("Telethon session not authorized. Run this helper locally once to sign in and upload the session file to /data/telethon.")


async def check_with_bot(number: str, exp: str, code: str) -> dict:
    """
    Messages @AllGccCheckersBot: "number:exp:code", waits for reply, parses balance.
    """
    await ensure_connected()

    entity = await client.get_entity(CHECK_BOT if CHECK_BOT.startswith("@") else f"@{CHECK_BOT}")

    # Send request
    payload = f"{number}:{exp}:{code}"
    await client.send_message(entity, payload)

    # Wait for a reply from that bot
    # We'll watch for the next message from the bot within 30s
    resp_text = None

    @client.on(events.NewMessage(from_users=entity))
    async def handler(event):
        nonlocal resp_text
        if resp_text is None:
            resp_text = event.raw_text

    # run the event wait for up to timeout
    try:
        # Wait up to 30 seconds for response
        for _ in range(60):
            await asyncio.sleep(0.5)
            if resp_text:
                break
    finally:
        client.remove_event_handler(handler, events.NewMessage)

    if not resp_text:
        return {"status": "timeout", "balance": 0.0, "raw": None}

    bal = parse_balance_from_text(resp_text)
    if bal is None:
        # Treat obvious invalid phrases
        if any(k in resp_text.lower() for k in ["invalid", "not found", "blocked", "error"]):
            return {"status": "invalid", "balance": 0.0, "raw": resp_text}
        return {"status": "unknown", "balance": 0.0, "raw": resp_text}

    if bal <= 0:
        return {"status": "empty", "balance": float(bal), "raw": resp_text}

    return {"status": "ok", "balance": float(bal), "raw": resp_text}


@app.route("/check_balance", methods=["POST"])
def http_check_balance():
    """
    JSON body:
      { "number": "4034461234567890", "exp": "12/27", "code": "123" }
    """
    data = request.get_json(force=True, silent=True) or {}
    number = str(data.get("number", "")).strip()
    exp = str(data.get("exp", "")).strip()
    code = str(data.get("code", "")).strip()

    if not number or not exp or not code:
        return jsonify({"error": "number, exp, and code are required"}), 400

    try:
        result = loop.run_until_complete(check_with_bot(number, exp, code))
        return jsonify(result)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": f"unexpected: {e}"}), 500


if __name__ == "__main__":
    # You can run this locally once to generate the session file:
    #   export TELEGRAM_API_ID=...
    #   export TELEGRAM_API_HASH=...
    #   python3 balance_checker_helper.py
    #
    # Then scan/login in the terminal, Ctrl+C, and deploy to Render with the session file in /data/telethon
    #
    app.run(host="0.0.0.0", port=8000)
