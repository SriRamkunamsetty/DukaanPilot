"""Onboard a real shop into whichever store is configured (DUKAANPILOT_STORE).

    python scripts/create_shop.py --id my-shop --name "Sri Sai Kirana" --owner Ramesh --language te \\
        --supplier dairy:"Sri Dairy":919876543210:te --supplier wholesale:"Traders":919812345678 \\
        --products products.csv --sales sales.csv

It prints a one-time join code. The owner opens your Telegram bot and sends  /start CODE.
For Firestore, run `gcloud auth application-default login` first and set GOOGLE_CLOUD_PROJECT.
"""

from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dukaanpilot.config import Settings  # noqa: E402
from dukaanpilot.data import importers  # noqa: E402
from dukaanpilot.data.models import Shop  # noqa: E402
from dukaanpilot.data.repository import open_repository  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--id", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--owner", required=True)
    ap.add_argument("--language", default="en", choices=["en", "hi", "te"])
    ap.add_argument("--timezone", default="Asia/Kolkata")
    ap.add_argument("--supplier", action="append", default=[], help="id:Name:919876543210[:lang] (repeatable)")
    ap.add_argument("--products", type=Path, help="CSV: " + ",".join(importers.PRODUCT_COLUMNS))
    ap.add_argument("--sales", type=Path, help="CSV: " + ",".join(importers.SALES_COLUMNS))
    args = ap.parse_args()

    repo = open_repository(Settings.from_env())
    existing = repo.get_shop(args.id)
    code = f"{args.id[:6].upper().strip('-')}-{secrets.token_hex(3).upper()}"
    shop = Shop(id=args.id, name=args.name, owner_name=args.owner, language=args.language, timezone=args.timezone,
                join_code=code, telegram_chat_id=existing.telegram_chat_id if existing else None,
                settings=existing.settings if existing else {})
    try:
        repo.upsert_shop(shop)
        for spec in args.supplier:
            importers.add_supplier(repo, args.id, spec)
        if args.products:
            print(f"products imported: {importers.import_products(repo, args.id, args.products.read_text(encoding='utf-8'))}")
        if args.sales:
            print(f"sales rows imported: {importers.import_sales(repo, args.id, args.sales.read_text(encoding='utf-8'))}")
    except (ValueError, OSError) as e:
        print(f"Import failed, nothing further written: {e}", file=sys.stderr)
        return 1
    print(f"\nShop '{args.name}' is ready.\nAsk the owner to open your bot and send:   /start {code}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
