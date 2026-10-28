"""Proactive jobs, triggered by Cloud Scheduler: the agent speaks first.

* morning briefing: once per shop per local day, at the shop's chosen hour
* stock watch: warns about items about to run out, once per item per day

Both are idempotent: Scheduler retries or overlapping runs never double-message.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Protocol

from .container import Container
from .data.models import Shop
from .i18n import t
from .services import briefing, inventory, reorders
from .services.timeutil import local_now, local_today

log = logging.getLogger(__name__)
WAKING_HOURS = range(8, 21)


class Writer(Protocol):
    async def write(self, data: dict[str, Any]) -> str: ...


def _lang(shop: Shop) -> str:
    return shop.settings.get("reply_language") or shop.language


async def send_briefing(c: Container, writer: Writer, shop: Shop, now_utc: datetime) -> None:
    """Build and send one briefing right now (also used by the /briefing command)."""
    today = local_today(shop, now_utc)
    yday = today - timedelta(days=1)
    products = c.repo.list_products(shop.id)
    if not c.repo.snapshots_between(shop.id, yday, yday):
        c.repo.save_snapshot(shop.id, yday, {p.sku: p.stock for p in products})
    reorders.expire_stale(c.repo, shop, now_utc, c.settings.draft_expiry_hours)
    statuses = inventory.stock_statuses(c.repo, shop.id, today, c.calendar, products)
    reorders.create_drafts(c.repo, shop, statuses, now_utc)
    data = briefing.compute_briefing(c.repo, shop, now_utc, c.calendar)
    text = await writer.write(data)
    buttons = briefing.order_buttons(data["pending_orders"], _lang(shop)) or None
    await c.messenger.send_text(shop.telegram_chat_id, text, buttons)  # type: ignore[arg-type]
    # The briefing already told the owner about these, so the stock watch must not repeat them today.
    for s in statuses:
        if s.risk in ("stockout", "critical"):
            c.repo.mark_notified(shop.id, "stock_alert", f"{today.isoformat()}:{s.product.sku}", now_utc.isoformat())
    c.repo.add_audit(shop.id, "agent", "briefing_sent", {"date": today.isoformat()}, now_utc.isoformat())


async def run_morning_briefings(
    c: Container, writer: Writer, now_utc: datetime | None = None, *, force: bool = False, shop_id: str | None = None
) -> dict[str, str]:
    now_utc = now_utc or c.now()
    results: dict[str, str] = {}
    for shop in c.repo.list_shops():
        if shop_id and shop.id != shop_id:
            continue
        if not shop.telegram_chat_id:
            results[shop.id] = "no_chat"
            continue
        local = local_now(shop, now_utc)
        hour = int(shop.settings.get("briefing_hour", c.settings.briefing_hour_local))
        if not force and local.hour < hour:
            results[shop.id] = "not_due"
            continue
        key = local.date().isoformat()
        claimed = c.repo.mark_notified(shop.id, "briefing", key, now_utc.isoformat())
        if not claimed and not force:
            results[shop.id] = "already_sent"
            continue
        try:
            await send_briefing(c, writer, shop, now_utc)
            results[shop.id] = "sent"
        except Exception:  # noqa: BLE001 - one shop failing must not stop the others
            log.exception("briefing_failed", extra={"shop_id": shop.id})
            if claimed:
                c.repo.unmark_notified(shop.id, "briefing", key)  # let the next run retry
            results[shop.id] = "failed"
    return results


async def run_stock_watch(
    c: Container, now_utc: datetime | None = None, *, shop_id: str | None = None
) -> dict[str, str]:
    now_utc = now_utc or c.now()
    results: dict[str, str] = {}
    for shop in c.repo.list_shops():
        if shop_id and shop.id != shop_id:
            continue
        if not shop.telegram_chat_id:
            results[shop.id] = "no_chat"
            continue
        if not shop.settings.get("stock_alerts", True):
            results[shop.id] = "disabled"
            continue
        local = local_now(shop, now_utc)
        if local.hour not in WAKING_HOURS:
            results[shop.id] = "quiet_hours"
            continue
        try:
            today = local.date()
            statuses = inventory.stock_statuses(c.repo, shop.id, today, c.calendar)
            fresh = [
                s for s in statuses
                if s.risk in ("stockout", "critical")
                and not c.repo.was_notified(shop.id, "stock_alert", f"{today.isoformat()}:{s.product.sku}")
            ]
            if not fresh:
                results[shop.id] = "nothing_new"
                continue
            drafts = reorders.create_drafts(c.repo, shop, fresh, now_utc)
            lang = _lang(shop)
            words = briefing._L.get(lang, briefing._L["en"])
            lines = [words["stock"]]
            for s in fresh:
                what = words["out"] if s.risk == "stockout" else words["left"].format(n=f"{s.product.stock:g}", unit=s.product.unit)
                lines.append(f"- {s.product.name}: {what}")
            if drafts:
                lines.append(words["orders"].format(n=len(drafts)))
            buttons = briefing.order_buttons(
                [{"id": d.id} for d in drafts], lang
            ) or None
            await c.messenger.send_text(shop.telegram_chat_id, "\n".join(lines), buttons)
            for s in fresh:
                c.repo.mark_notified(shop.id, "stock_alert", f"{today.isoformat()}:{s.product.sku}", now_utc.isoformat())
            results[shop.id] = f"alerted:{len(fresh)}"
        except Exception:  # noqa: BLE001
            log.exception("stock_watch_failed", extra={"shop_id": shop.id})
            results[shop.id] = "failed"
    return results


__all__ = ["send_briefing", "run_morning_briefings", "run_stock_watch", "t"]
