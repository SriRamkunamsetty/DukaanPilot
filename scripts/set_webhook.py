"""Point Telegram at a deployed service (deploy.sh does this too; use this to repair it).

    TELEGRAM_BOT_TOKEN=... TELEGRAM_WEBHOOK_SECRET=... python scripts/set_webhook.py https://SERVICE_URL
"""

from __future__ import annotations

import json
import os
import sys

import httpx


def main() -> int:
    if len(sys.argv) != 2 or not sys.argv[1].startswith("https://"):
        print(__doc__)
        return 2
    token, secret = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    if not token or not secret:
        print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_WEBHOOK_SECRET.", file=sys.stderr)
        return 2
    r = httpx.post(
        f"https://api.telegram.org/bot{token}/setWebhook",
        data={"url": sys.argv[1].rstrip("/") + "/webhooks/telegram", "secret_token": secret,
              "allowed_updates": json.dumps(["message", "callback_query"])},
        timeout=20,
    )
    print(r.json().get("description", r.text))
    return 0 if r.json().get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
