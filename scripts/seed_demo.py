"""Create the demo shop ("Lakshmi General Stores") in the configured store, for judges and demos.

    DUKAANPILOT_STORE=firestore GOOGLE_CLOUD_PROJECT=my-project python scripts/seed_demo.py

Against anything but local SQLite it replaces the public demo join code with a random one,
so strangers who find the bot cannot claim the demo shop.
"""

from __future__ import annotations

import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dukaanpilot.config import Settings  # noqa: E402
from dukaanpilot.data.repository import open_repository  # noqa: E402
from dukaanpilot.data.seed import seed_demo  # noqa: E402


def main() -> int:
    s = Settings.from_env()
    repo = open_repository(s)
    today = datetime.now(UTC).astimezone(ZoneInfo(s.default_timezone)).date()
    shop = seed_demo(repo, today, shop_id=s.default_shop_id)
    code = shop.join_code
    if s.store != "sqlite" or s.is_prod:
        code = f"DEMO-{secrets.token_hex(3).upper()}"
        shop.join_code = code
        repo.upsert_shop(shop)
    print(f"Demo shop '{shop.name}' seeded for {today}.\nIn Telegram, send your bot:   /start {code}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
