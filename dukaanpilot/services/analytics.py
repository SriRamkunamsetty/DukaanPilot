"""Sales analytics: summaries, baselines, anomaly detection and "why" explanations.

All numbers the agent speaks come from here, computed in code. The language model
only narrates them, which keeps the agent from inventing figures.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date, timedelta

from ..data.models import Product, SaleRow
from ..data.repository import Repository

BASELINE_WEEKS = 4


def daily_revenue(rows: list[SaleRow]) -> dict[date, float]:
    out: dict[date, float] = defaultdict(float)
    for r in rows:
        out[r.sale_date] += r.revenue
    return dict(out)


def _same_weekday_samples(by_day: dict[date, float], day: date, weeks: int = BASELINE_WEEKS) -> list[float]:
    samples = []
    for k in range(1, weeks + 1):
        v = by_day.get(day - timedelta(days=7 * k))
        if v is not None and v > 0:
            samples.append(v)
    return samples


def _robust_center_scale(samples: list[float]) -> tuple[float, float]:
    med = statistics.median(samples)
    mad = statistics.median(abs(x - med) for x in samples)
    scale = 1.4826 * mad
    floor = 0.05 * med
    if scale < floor:
        scale = max(statistics.pstdev(samples), floor, 1e-9)
    return med, scale


def period_summary(repo: Repository, shop_id: str, start: date, end: date) -> dict:
    rows = repo.sales_between(shop_id, start, end)
    by_day = daily_revenue(rows)
    days = (end - start).days + 1
    revenue = sum(by_day.values())
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "days": days,
        "revenue": round(revenue, 2),
        "units_sold": round(sum(r.qty for r in rows), 2),
        "daily_average": round(revenue / days, 2) if days else 0.0,
        "days_with_sales": len(by_day),
        "best_day": (
            {"date": max(by_day, key=by_day.get).isoformat(), "revenue": round(max(by_day.values()), 2)}
            if by_day
            else None
        ),
    }


def top_products(repo: Repository, shop_id: str, start: date, end: date, limit: int = 5) -> list[dict]:
    names = {p.sku: p for p in repo.list_products(shop_id)}
    agg: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for r in repo.sales_between(shop_id, start, end):
        agg[r.sku][0] += r.qty
        agg[r.sku][1] += r.revenue
    ranked = sorted(agg.items(), key=lambda kv: -kv[1][1])[: max(1, min(limit, 20))]
    return [
        {
            "sku": sku,
            "name": names[sku].name if sku in names else sku,
            "units": round(q, 2),
            "unit": names[sku].unit if sku in names else "",
            "revenue": round(rev, 2),
        }
        for sku, (q, rev) in ranked
    ]


def compare_day(repo: Repository, shop_id: str, day: date) -> dict:
    """A day's revenue against the same weekday in the previous four weeks."""
    rows = repo.sales_between(shop_id, day - timedelta(days=7 * BASELINE_WEEKS), day)
    by_day = daily_revenue(rows)
    actual = by_day.get(day, 0.0)
    samples = _same_weekday_samples(by_day, day)
    result = {"date": day.isoformat(), "weekday": day.strftime("%A"), "revenue": round(actual, 2),
              "baseline": None, "pct_vs_baseline": None, "baseline_samples": len(samples)}
    if len(samples) >= 2:
        med, _ = _robust_center_scale(samples)
        result["baseline"] = round(med, 2)
        result["pct_vs_baseline"] = round((actual - med) / med * 100, 1) if med else None
    return result


def detect_anomalies(
    repo: Repository, shop_id: str, today: date, days: int = 14, min_pct: float = 15.0, z_threshold: float = 2.0
) -> list[dict]:
    """Days in the last ``days`` whose revenue is unusual for that weekday."""
    days = max(1, min(days, 60))
    first = today - timedelta(days=days)
    rows = repo.sales_between(shop_id, first - timedelta(days=7 * BASELINE_WEEKS), today - timedelta(days=1))
    by_day = daily_revenue(rows)
    found = []
    for i in range(days):
        d = first + timedelta(days=i)
        if d >= today:
            break
        samples = _same_weekday_samples(by_day, d)
        if len(samples) < 3:
            continue
        med, scale = _robust_center_scale(samples)
        actual = by_day.get(d, 0.0)
        pct = (actual - med) / med * 100 if med else 0.0
        z = (actual - med) / scale
        if abs(z) >= z_threshold and abs(pct) >= min_pct:
            found.append({
                "date": d.isoformat(), "weekday": d.strftime("%A"), "revenue": round(actual, 2),
                "expected": round(med, 2), "pct_vs_expected": round(pct, 1),
                "direction": "above" if pct > 0 else "below",
            })
    return sorted(found, key=lambda a: a["date"], reverse=True)


def explain_change(
    repo: Repository, shop_id: str, day: date, products: list[Product] | None = None, festival: str | None = None
) -> dict:
    """Which products moved revenue on ``day`` versus the usual, and were they out of stock?"""
    products = products if products is not None else repo.list_products(shop_id)
    by_sku = {p.sku: p for p in products}
    rows = repo.sales_between(shop_id, day - timedelta(days=7 * BASELINE_WEEKS), day)
    total_by_day = daily_revenue(rows)
    weeks = [k for k in range(1, BASELINE_WEEKS + 1) if total_by_day.get(day - timedelta(days=7 * k), 0) > 0]

    actual: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    base: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for r in rows:
        if r.sale_date == day:
            actual[r.sku][0] += r.qty
            actual[r.sku][1] += r.revenue
        else:
            k = (day - r.sale_date).days
            if k % 7 == 0 and (k // 7) in weeks:
                base[r.sku][0] += r.qty
                base[r.sku][1] += r.revenue

    snap = repo.snapshots_between(shop_id, day, day).get(day, {})
    n = len(weeks)
    drivers = []
    for sku in set(actual) | set(base):
        exp_q, exp_rev = (base[sku][0] / n, base[sku][1] / n) if n else (0.0, 0.0)
        act_q, act_rev = actual[sku]
        drivers.append({
            "sku": sku,
            "name": by_sku[sku].name if sku in by_sku else sku,
            "unit": by_sku[sku].unit if sku in by_sku else "",
            "expected_units": round(exp_q, 1), "actual_units": round(act_q, 1),
            "revenue_change": round(act_rev - exp_rev, 2),
            "out_of_stock_that_day": snap.get(sku, 1) <= 0,
        })
    actual_total = total_by_day.get(day, 0.0)
    expected_total = statistics.median([total_by_day[day - timedelta(days=7 * k)] for k in weeks]) if n >= 2 else None
    pct = round((actual_total - expected_total) / expected_total * 100, 1) if expected_total else None

    down = pct is not None and pct < 0
    drivers.sort(key=lambda d: d["revenue_change"] if down else -d["revenue_change"])
    top = drivers[:5]

    cause = "not_enough_history" if n < 2 else "normal_variation"
    if n >= 2 and pct is not None and abs(pct) >= 10:
        lost = sum(d["revenue_change"] for d in drivers if d["revenue_change"] < 0)
        lost_stockout = sum(d["revenue_change"] for d in drivers if d["revenue_change"] < 0 and d["out_of_stock_that_day"])
        if down and lost < 0 and lost_stockout / lost >= 0.5:
            cause = "stock_outs"
        elif down:
            cause = "broad_decline"
        else:
            cause = "festival_or_demand_spike" if festival else "above_usual_demand"

    return {
        "date": day.isoformat(), "weekday": day.strftime("%A"),
        "revenue": round(actual_total, 2),
        "expected_revenue": round(expected_total, 2) if expected_total else None,
        "pct_vs_expected": pct, "likely_cause": cause, "festival_in_window": festival,
        "top_drivers": top, "weeks_compared": n,
    }
