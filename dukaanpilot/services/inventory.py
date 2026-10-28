"""Stock-risk assessment: how long will each item last, and what should we order?"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from ..data.models import Product
from ..data.repository import Repository
from . import forecast
from .festivals import FestivalCalendar

RISK_ORDER = {"stockout": 0, "critical": 1, "low": 2, "ok": 3}


@dataclass
class StockStatus:
    product: Product
    daily_demand: float  # expected units/day over the next lead time
    days_cover: float
    risk: str  # stockout | critical | low | ok
    suggested_qty: int
    festival: str | None = None
    model: forecast.DemandModel | None = None


def stock_statuses(
    repo: Repository,
    shop_id: str,
    today: date,
    calendar: FestivalCalendar,
    products: list[Product] | None = None,
) -> list[StockStatus]:
    products = products if products is not None else repo.list_products(shop_id)
    start = today - timedelta(days=forecast.LOOKBACK_DAYS)
    end = today - timedelta(days=1)
    qty_by: dict[str, dict[date, float]] = defaultdict(lambda: defaultdict(float))
    for r in repo.sales_between(shop_id, start, end):
        qty_by[r.sku][r.sale_date] += r.qty
    snapshots = repo.snapshots_between(shop_id, start, end)

    out: list[StockStatus] = []
    for p in products:
        history: dict[date, float] = {}
        for i in range(forecast.LOOKBACK_DAYS):
            d = start + timedelta(days=i)
            if d > end:
                break
            # Drop stock-out days: zero sales then is lost demand, not low demand.
            if snapshots.get(d, {}).get(p.sku, 1.0) <= 0:
                continue
            history[d] = qty_by[p.sku].get(d, 0.0)

        model = forecast.fit(history, today)
        horizon = p.lead_time_days + p.review_days
        daily = forecast.forecast(model, today, horizon, calendar.uplift)
        cover = forecast.days_of_cover(p.stock, daily)
        lead_demand = daily[: max(p.lead_time_days, 1)]
        mean_demand = sum(lead_demand) / len(lead_demand) if lead_demand else 0.0

        if p.stock <= 0:
            risk = "stockout"
        elif model.usable_days >= 7 and cover < p.lead_time_days:
            risk = "critical"
        elif p.stock <= p.reorder_point or (model.usable_days >= 7 and cover < p.lead_time_days + 2):
            risk = "low"
        else:
            risk = "ok"

        qty = 0
        if risk != "ok":
            qty = forecast.reorder_quantity(
                model, p.stock, p.lead_time_days, p.review_days, today, calendar.uplift, p.pack_size
            )
        fest = calendar.upcoming(today, horizon)
        out.append(
            StockStatus(
                product=p, daily_demand=round(mean_demand, 2), days_cover=round(cover, 1),
                risk=risk, suggested_qty=qty, festival=fest[0][0].name if fest else None, model=model,
            )
        )
    out.sort(key=lambda s: (RISK_ORDER[s.risk], s.days_cover))
    return out
