"""Tools that change data.

Guardrails, in order of strength:
1. The model can *propose* supplier orders but cannot approve them. Approval is a
   button the owner taps (handled by deterministic code, not by the model).
2. Every change is written to an audit log.
3. Sales can be undone, but only sales the agent itself recorded.
4. Quantities and dates are range-checked.
"""

from __future__ import annotations

from datetime import date, timedelta

from google.adk.tools import ToolContext

from ..i18n import t
from ..services import inventory, reorders
from ..services.briefing import order_buttons
from ..services.timeutil import format_inr, resolve_period
from .common import ToolFailure, ok, resolve_product, safe_tool, tool_env

MAX_QTY = 100_000
ALLOWED_PREFS = {
    "briefing_hour": "Hour of day (0-23, shop local time) for the morning briefing.",
    "reply_language": "en, hi, te or auto.",
    "stock_alerts": "true or false: proactive stock-out alerts.",
}


def _check_qty(qty: float) -> float:
    qty = float(qty)
    if not (0 < qty <= MAX_QTY):
        raise ToolFailure(f"Quantity must be more than 0 and at most {MAX_QTY}.")
    return qty


@safe_tool
def log_sale(
    product: str, quantity: float, price_per_unit: float = 0.0, sale_date: str = "today",
    tool_context: ToolContext = None,
) -> dict:
    """Record a sale and reduce stock. Read the result back to the owner so they can catch mistakes.

    Args:
        product: Product name or local word (milk, palu, doodh).
        quantity: Units sold. Must be above 0.
        price_per_unit: Selling price per unit in rupees; 0 uses the shop's listed price.
        sale_date: today, yesterday or a date within the last 7 days (YYYY-MM-DD).

    Returns:
        dict: sale_id (needed to undo), what was recorded and the new stock level.
    """
    env = tool_env(tool_context)
    p = resolve_product(env, product)
    qty = _check_qty(quantity)
    day, _, _ = resolve_period(sale_date, env.today)
    if day > env.today or (env.today - day).days > 7:
        raise ToolFailure("I can only record sales from the last 7 days, not the future.")
    price = float(price_per_unit) if price_per_unit and price_per_unit > 0 else p.price
    if price > 1_000_000:
        raise ToolFailure("That price looks wrong.")
    warnings = []
    if qty > p.stock:
        warnings.append(f"Only {p.stock:g} {p.unit} were in stock; stock is now 0. Check the count.")
    sale_id = env.c.repo.record_sale(env.shop.id, day, p.sku, qty, round(qty * price, 2))
    new_stock = env.c.repo.adjust_stock(env.shop.id, p.sku, -qty)
    env.c.repo.add_audit(env.shop.id, env.actor, "sale_logged",
                         {"sale_id": sale_id, "sku": p.sku, "qty": qty, "revenue": round(qty * price, 2),
                          "date": day.isoformat()}, env.now.isoformat())
    return ok(
        sale_id=sale_id, product=p.name, quantity=qty, unit=p.unit, date=day.isoformat(),
        revenue=round(qty * price, 2), revenue_formatted=format_inr(qty * price),
        stock_now=new_stock, warnings=warnings,
        note="Tell the owner what was recorded and that they can say 'undo' to reverse it.",
    )


@safe_tool
def undo_sale(sale_id: int, tool_context: ToolContext = None) -> dict:
    """Reverse a sale that this assistant recorded earlier and put the stock back.

    Args:
        sale_id: The sale_id returned by log_sale.

    Returns:
        dict: what was reversed.
    """
    env = tool_env(tool_context)
    logged = [a for a in env.c.repo.list_audit(env.shop.id, 300) if a["action"] == "sale_logged" and a["detail"].get("sale_id") == sale_id]
    if not logged:
        raise ToolFailure("I can only undo sales that I recorded myself.")
    if any(a["action"] == "sale_undone" and a["detail"].get("sale_id") == sale_id for a in env.c.repo.list_audit(env.shop.id, 300)):
        raise ToolFailure("That sale was already undone.")
    row = env.c.repo.delete_sale(env.shop.id, sale_id)
    if row is None:
        raise ToolFailure("I can't find that sale.")
    env.c.repo.adjust_stock(env.shop.id, row.sku, row.qty)
    env.c.repo.add_audit(env.shop.id, env.actor, "sale_undone", {"sale_id": sale_id, "sku": row.sku, "qty": row.qty}, env.now.isoformat())
    return ok(undone={"sale_id": sale_id, "sku": row.sku, "quantity": row.qty, "revenue": row.revenue})


@safe_tool
def log_restock(product: str, quantity: float, tool_context: ToolContext = None) -> dict:
    """Record that new stock arrived from a supplier and add it to inventory.

    Args:
        product: Product name or local word.
        quantity: Units received. Must be above 0.

    Returns:
        dict: the new stock level.
    """
    env = tool_env(tool_context)
    p = resolve_product(env, product)
    qty = _check_qty(quantity)
    new_stock = env.c.repo.adjust_stock(env.shop.id, p.sku, qty)
    env.c.repo.add_audit(env.shop.id, env.actor, "restock_logged", {"sku": p.sku, "qty": qty}, env.now.isoformat())
    return ok(product=p.name, received=qty, unit=p.unit, stock_now=new_stock)


@safe_tool
def create_reorder_drafts(tool_context: ToolContext = None) -> dict:
    """Prepare supplier order drafts for everything at risk of running out.

    Drafts are only proposals. Nothing is sent. After creating drafts, call
    request_reorder_approval for each so the owner can approve with a button.

    Returns:
        dict: new drafts (one per supplier) and any that were already waiting.
    """
    env = tool_env(tool_context)
    statuses = inventory.stock_statuses(env.c.repo, env.shop.id, env.today, env.c.calendar)
    created = reorders.create_drafts(env.c.repo, env.shop, statuses, env.now)
    waiting = [d for d in env.c.repo.list_drafts(env.shop.id, "pending") if d.id not in {c.id for c in created}]
    fmt = lambda d: {  # noqa: E731
        "id": d.id, "supplier": d.supplier_name, "why": d.rationale,
        "items": [{"name": i.name, "qty": i.qty, "unit": i.unit} for i in d.items],
    }
    return ok(new_drafts=[fmt(d) for d in created], already_waiting=[fmt(d) for d in waiting])


@safe_tool
async def request_reorder_approval(draft_id: str, tool_context: ToolContext = None) -> dict:
    """Ask the owner to approve or cancel a draft using on-screen buttons.

    You cannot approve orders yourself; only the owner can, by tapping the button.

    Args:
        draft_id: The draft id such as RD1A2B.

    Returns:
        dict: whether the approval buttons were sent to the owner's chat.
    """
    env = tool_env(tool_context)
    draft = env.c.repo.get_draft(env.shop.id, draft_id)
    if draft is None:
        raise ToolFailure(f"No draft {draft_id}.")
    if draft.status != "pending":
        raise ToolFailure(f"Draft {draft.id} is already {draft.status}.")
    if not env.chat_id:
        return ok(sent=False, draft_id=draft.id,
                  note="No chat is linked here. Tell the owner to open the draft in their messaging app to approve it.")
    items = "\n".join(f"- {i.name}: {i.qty} {i.unit}" for i in draft.items)
    text = f"{draft.id} → {draft.supplier_name}\n{items}"
    await env.c.messenger.send_text(env.chat_id, text, order_buttons([{"id": draft.id}], env.lang))
    return ok(sent=True, draft_id=draft.id, note="Approval buttons were sent. Tell the owner to tap Approve or Cancel.")


@safe_tool
def cancel_reorder_draft(draft_id: str, tool_context: ToolContext = None) -> dict:
    """Cancel a pending draft when the owner says they do not want it.

    Args:
        draft_id: The draft id such as RD1A2B.

    Returns:
        dict: the cancelled draft.
    """
    env = tool_env(tool_context)
    try:
        d = reorders.reject(env.c.repo, env.shop, draft_id, env.actor, env.now).draft
    except (reorders.DraftNotFound, reorders.InvalidTransition) as exc:
        raise ToolFailure(str(exc)) from exc
    return ok(cancelled=d.id, supplier=d.supplier_name, message=t("rejected", env.lang, id=d.id))


@safe_tool
def update_preference(key: str, value: str, tool_context: ToolContext = None) -> dict:
    """Save a preference for this shop.

    Args:
        key: One of briefing_hour, reply_language, stock_alerts.
        value: The new value as text.

    Returns:
        dict: the saved preference.
    """
    env = tool_env(tool_context)
    if key not in ALLOWED_PREFS:
        raise ToolFailure(f"Unknown preference. Allowed: {', '.join(ALLOWED_PREFS)}.")
    parsed: object
    if key == "briefing_hour":
        if not value.strip().isdigit() or not 0 <= int(value) <= 23:
            raise ToolFailure("briefing_hour must be a number from 0 to 23.")
        parsed = int(value)
    elif key == "reply_language":
        if value not in {"en", "hi", "te", "auto"}:
            raise ToolFailure("reply_language must be en, hi, te or auto.")
        parsed = None if value == "auto" else value
    else:
        if value.strip().lower() not in {"true", "false"}:
            raise ToolFailure("stock_alerts must be true or false.")
        parsed = value.strip().lower() == "true"
    env.c.repo.set_setting(env.shop.id, key, parsed)
    env.c.repo.add_audit(env.shop.id, env.actor, "preference_changed", {"key": key, "value": parsed}, env.now.isoformat())
    return ok(saved={key: parsed})


__all__ = [
    "log_sale", "undo_sale", "log_restock", "create_reorder_drafts", "request_reorder_approval",
    "cancel_reorder_draft", "update_preference", "date", "timedelta",
]
