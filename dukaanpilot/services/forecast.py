"""Small, explainable demand forecasting. Pure functions, no I/O.

Why not a big ML model? A kirana shop has a few weeks of noisy data per item. A
weekday-aware moving average is accurate enough, cheap, and the owner can be told
exactly why it says what it says. Two details matter more than model choice:

* Days when the item was out of stock are dropped from history. Zero sales on a
  stock-out day is *lost demand*, not low demand, and would otherwise make the
  agent under-order the very items that keep running out.
* Festival uplift is applied on top, so the agent orders ahead of Dussehra/Diwali.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

Uplift = Callable[[date], float]
NO_UPLIFT: Uplift = lambda _d: 1.0  # noqa: E731

LOOKBACK_DAYS = 56
RECENT_DAYS = 14
MAX_COVER_DAYS = 999.0


@dataclass
class DemandModel:
    level: float = 0.0  # recent average daily demand
    weekday: dict[int, float] = field(default_factory=dict)  # Monday=0
    sigma: float = 0.0  # typical day-to-day noise
    usable_days: int = 0

    def predict(self, d: date) -> float:
        base = self.level
        if self.weekday:
            base = 0.6 * self.weekday.get(d.weekday(), self.level) + 0.4 * self.level
        return max(base, 0.0)


def fit(history: dict[date, float], today: date) -> DemandModel:
    """Fit on days strictly before ``today``. Missing days are simply not used."""
    start = today - timedelta(days=LOOKBACK_DAYS)
    days = sorted(d for d in history if start <= d < today)
    if not days:
        return DemandModel()
    values = [history[d] for d in days]
    recent = [history[d] for d in days if d >= today - timedelta(days=RECENT_DAYS)] or values
    level = statistics.fmean(recent)

    weekday: dict[int, float] = {}
    if len(days) >= 14:
        for w in range(7):
            ws = [history[d] for d in days if d.weekday() == w]
            if len(ws) >= 2:
                weekday[w] = statistics.fmean(ws)

    model = DemandModel(level=level, weekday=weekday, usable_days=len(days))
    residuals = [history[d] - model.predict(d) for d in days]
    model.sigma = statistics.pstdev(residuals) if len(residuals) > 1 else math.sqrt(max(level, 0.0))
    return model


def forecast(model: DemandModel, start: date, days: int, uplift: Uplift = NO_UPLIFT) -> list[float]:
    return [model.predict(start + timedelta(days=i)) * uplift(start + timedelta(days=i)) for i in range(days)]


def days_of_cover(stock: float, daily: list[float], fallback_daily: float = 0.0) -> float:
    """How many days the stock lasts against the forecast; capped at 999."""
    if stock <= 0:
        return 0.0
    remaining = stock
    for i, need in enumerate(daily):
        if need > 0 and remaining < need:
            return i + remaining / need
        remaining -= need
    tail = fallback_daily if fallback_daily > 0 else (statistics.fmean(daily) if daily else 0.0)
    if tail <= 0:
        return MAX_COVER_DAYS
    return min(len(daily) + remaining / tail, MAX_COVER_DAYS)


def reorder_quantity(
    model: DemandModel,
    stock: float,
    lead_time_days: int,
    review_days: int,
    start: date,
    uplift: Uplift = NO_UPLIFT,
    pack_size: int = 1,
    service_z: float = 1.65,
) -> int:
    """Units to order so we do not run out before the *next* delivery.

    need = forecast demand over (lead time + review period) + safety stock - stock on hand
    """
    horizon = max(lead_time_days + review_days, 1)
    demand = sum(forecast(model, start, horizon, uplift))
    safety = service_z * model.sigma * math.sqrt(horizon)
    need = demand + safety - max(stock, 0.0)
    if need <= 0:
        return 0
    pack = max(int(pack_size), 1)
    return int(math.ceil(need / pack) * pack)
