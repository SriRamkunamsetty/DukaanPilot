"""Read-only tools: safe to call any time, never change data."""

from __future__ import annotations

from datetime import timedelta

from google.adk.tools import ToolContext

from ..services import analytics, forecast, inventory, reorders
from ..services.timeutil import format_inr, resolve_period
from .common import ToolFailure, ok, resolve_product, safe_tool, tool_env


@safe_tool
def get_sales_summary(period: str = "yesterday", tool_context: ToolContext = None) -> dict:
    """Total sales for a period, with a comparison to the same weekday in recent weeks.

    Args:
        period: One of today, yesterday, this_week, last_7_days, last_30_days,
            this_month, a date like 2026-10-14, or a range like 2026-10-01..2026-10-14.

    Returns:
        dict: revenue in rupees, units sold, daily average, best day and, for a single
        day, how it compares with a usual same weekday.
    """
    env = tool_env(tool_context)
    start, end, label = resolve_period(period, env.today)
    summary = analytics.period_summary(env.c.repo, env.shop.id, start, end)
    out = {"period": label, **summary, "revenue_formatted": format_inr(summary["revenue"])}
    if start == end:
        out["vs_usual_day"] = analytics.compare_day(env.c.repo, env.shop.id, start)
    return ok(**out)


@safe_tool
def get_top_products(period: str = "last_7_days", limit: int = 5, tool_context: ToolContext = None) -> dict:
    """Best-selling products by revenue for a period.

    Args:
        period: Same options as get_sales_summary.
        limit: How many products to return (1 to 20).

    Returns:
        dict: ranked list with units and revenue per product.
    """
    env = tool_env(tool_context)
    start, end, label = resolve_period(period, env.today)
    return ok(period=label, products=analytics.top_products(env.c.repo, env.shop.id, start, end, limit))


@safe_tool
def check_inventory(product: str = "", tool_context: ToolContext = None) -> dict:
    """Current stock, how many days it will last, and risk level for one product or all.

    Args:
        product: Product name or local word (milk, palu, doodh). Leave empty for every product.

    Returns:
        dict: per-product stock, days_cover (999 means no recent demand), risk
        (stockout, critical, low, ok) and suggested_reorder_qty.
    """
    env = tool_env(tool_context)
    products = [resolve_product(env, product)] if product.strip() else None
    statuses = inventory.stock_statuses(env.c.repo, env.shop.id, env.today, env.c.calendar, products)
    return ok(items=[
        {
            "name": s.product.name, "unit": s.product.unit, "stock": s.product.stock,
            "days_cover": s.days_cover, "risk": s.risk, "expected_units_per_day": s.daily_demand,
            "supplier_lead_time_days": s.product.lead_time_days, "suggested_reorder_qty": s.suggested_qty,
        }
        for s in statuses
    ])


@safe_tool
def get_stock_alerts(tool_context: ToolContext = None) -> dict:
    """Only the products that are out of stock, about to run out, or low.

    Returns:
        dict: at-risk products sorted by urgency, with suggested reorder quantities.
    """
    env = tool_env(tool_context)
    statuses = inventory.stock_statuses(env.c.repo, env.shop.id, env.today, env.c.calendar)
    risky = [s for s in statuses if s.risk != "ok"]
    return ok(count=len(risky), alerts=[
        {"name": s.product.name, "unit": s.product.unit, "stock": s.product.stock, "risk": s.risk,
         "days_cover": s.days_cover, "supplier_lead_time_days": s.product.lead_time_days,
         "suggested_reorder_qty": s.suggested_qty, "festival_ahead": s.festival}
        for s in risky
    ])


@safe_tool
def forecast_demand(product: str, days: int = 7, tool_context: ToolContext = None) -> dict:
    """Expected sales of a product for the next few days, including festival effects.

    Args:
        product: Product name or local word.
        days: Number of days to forecast (1 to 14).

    Returns:
        dict: units per day, the total, current stock and whether it will last.
    """
    env = tool_env(tool_context)
    p = resolve_product(env, product)
    days = max(1, min(int(days), 14))
    s = inventory.stock_statuses(env.c.repo, env.shop.id, env.today, env.c.calendar, [p])[0]
    daily = forecast.forecast(s.model, env.today, days, env.c.calendar.uplift)
    return ok(
        product=p.name, unit=p.unit, stock=p.stock, days_cover=s.days_cover, risk=s.risk,
        forecast=[{"date": (env.today + timedelta(days=i)).isoformat(), "units": round(v, 1)} for i, v in enumerate(daily)],
        total_units=round(sum(daily), 1), suggested_reorder_qty=s.suggested_qty,
        history_days_used=s.model.usable_days,
        note="Days when the item was out of stock are ignored when learning demand.",
    )


@safe_tool
def explain_sales_change(date: str = "yesterday", tool_context: ToolContext = None) -> dict:
    """Explain why a day's sales were higher or lower than usual, product by product.

    Args:
        date: yesterday, today or a date like 2026-10-14.

    Returns:
        dict: percent change versus the usual same weekday, the likely cause
        (stock_outs, broad_decline, festival_or_demand_spike, normal_variation) and the
        products that moved revenue most, flagging any that were out of stock.
    """
    env = tool_env(tool_context)
    start, _end, _ = resolve_period(date, env.today)
    festival = env.c.calendar.active_on(start)
    return ok(**analytics.explain_change(env.c.repo, env.shop.id, start, festival=festival.name if festival else None))


@safe_tool
def detect_sales_anomalies(days: int = 14, tool_context: ToolContext = None) -> dict:
    """Find recent days whose sales were unusually high or low for that weekday.

    Args:
        days: How many recent days to scan (1 to 60).

    Returns:
        dict: list of unusual days with expected and actual revenue.
    """
    env = tool_env(tool_context)
    found = analytics.detect_anomalies(env.c.repo, env.shop.id, env.today, days)
    return ok(count=len(found), anomalies=found)


@safe_tool
def get_upcoming_festivals(within_days: int = 30, tool_context: ToolContext = None) -> dict:
    """Festivals coming up that will change demand.

    Args:
        within_days: How far ahead to look (1 to 120).

    Returns:
        dict: festivals with days remaining and the demand uplift assumed.
    """
    env = tool_env(tool_context)
    ahead = max(1, min(int(within_days), 120))
    return ok(festivals=[
        {"name": f.name, "date": f.day.isoformat(), "in_days": n, "assumed_demand_uplift": f.uplift}
        for f, n in env.c.calendar.upcoming(env.today, ahead)
    ])


@safe_tool
def list_reorder_drafts(status: str = "pending", tool_context: ToolContext = None) -> dict:
    """Supplier order drafts and their status.

    Args:
        status: pending, approved, rejected, expired or all.

    Returns:
        dict: drafts with supplier, items and the reason each item was included.
    """
    env = tool_env(tool_context)
    if status not in {"pending", "approved", "rejected", "expired", "all"}:
        raise ToolFailure("status must be pending, approved, rejected, expired or all.")
    drafts = env.c.repo.list_drafts(env.shop.id, None if status == "all" else status)[:20]
    return ok(drafts=[
        {"id": d.id, "supplier": d.supplier_name, "status": d.status, "created_at": d.created_at,
         "items": [{"name": i.name, "qty": i.qty, "unit": i.unit} for i in d.items], "why": d.rationale}
        for d in drafts
    ])


@safe_tool
def get_shop_profile(tool_context: ToolContext = None) -> dict:
    """The shop's name, owner, language and saved preferences.

    Returns:
        dict: profile fields and suppliers.
    """
    env = tool_env(tool_context)
    suppliers = env.c.repo.list_suppliers(env.shop.id)
    return ok(
        shop=env.shop.name, owner=env.shop.owner_name, language=env.lang, timezone=env.shop.timezone,
        today=env.today.isoformat(), weekday=env.today.strftime("%A"), preferences=env.shop.settings,
        suppliers=[{"name": s.name, "language": s.language} for s in suppliers],
    )


__all__ = [
    "get_sales_summary", "get_top_products", "check_inventory", "get_stock_alerts", "forecast_demand",
    "explain_sales_change", "detect_sales_anomalies", "get_upcoming_festivals", "list_reorder_drafts",
    "get_shop_profile", "reorders",
]
