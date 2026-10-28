"""The proactive morning briefing: compute facts in code, let the model phrase them.

Order of trust: numbers come from ``compute_briefing`` (code). The model only
writes the message, and ``is_grounded`` rejects any text containing a figure that
is not in the data, falling back to the deterministic template below.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from ..data.models import Shop
from ..data.repository import Repository
from ..i18n import t
from . import analytics, inventory
from .festivals import FestivalCalendar
from .messaging import Button
from .timeutil import format_inr, local_today

MAX_ALERTS = 6


def compute_briefing(repo: Repository, shop: Shop, now_utc: datetime, calendar: FestivalCalendar) -> dict[str, Any]:
    today = local_today(shop, now_utc)
    yday = today - timedelta(days=1)

    summary = analytics.compare_day(repo, shop.id, yday)
    top = analytics.top_products(repo, shop.id, yday, yday, 3)
    anomaly = next((a for a in analytics.detect_anomalies(repo, shop.id, today, days=3) if a["date"] == yday.isoformat()), None)
    explanation = None
    if anomaly:
        festival = calendar.active_on(yday)
        explanation = analytics.explain_change(repo, shop.id, yday, festival=festival.name if festival else None)

    statuses = inventory.stock_statuses(repo, shop.id, today, calendar)
    alerts = [
        {
            "name": s.product.name, "unit": s.product.unit, "stock": s.product.stock, "risk": s.risk,
            "days_cover": s.days_cover, "suggested_qty": s.suggested_qty,
        }
        for s in statuses if s.risk != "ok"
    ]
    pending = [
        {"id": d.id, "supplier": d.supplier_name, "items": [{"name": i.name, "qty": i.qty, "unit": i.unit} for i in d.items]}
        for d in repo.list_drafts(shop.id, "pending")
    ]
    upcoming = [{"name": f.name, "in_days": n} for f, n in calendar.upcoming(today, 10)]

    if pending:
        action = {"kind": "approve_orders", "count": len(pending)}
    elif alerts:
        action = {"kind": "review_stock", "count": len(alerts)}
    else:
        action = {"kind": "none", "count": 0}

    return {
        "shop": shop.name, "owner": shop.owner_name, "language": shop.settings.get("reply_language") or shop.language,
        "date": today.isoformat(), "weekday": today.strftime("%A"),
        "yesterday": {**summary, "top_items": top},
        "anomaly": anomaly,
        "explanation": (
            {"likely_cause": explanation["likely_cause"], "drivers": [
                {"name": d["name"], "revenue_change": d["revenue_change"], "out_of_stock": d["out_of_stock_that_day"]}
                for d in explanation["top_drivers"][:4]]}
            if explanation else None
        ),
        "stock_alerts": alerts[:MAX_ALERTS], "stock_alert_total": len(alerts),
        "pending_orders": pending, "upcoming_festivals": upcoming, "suggested_action": action,
    }


# -- grounding guard ---------------------------------------------------------------

_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers_in(obj: Any, out: set[float]) -> None:
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        out.add(float(obj))
    elif isinstance(obj, str):
        for m in _NUM.findall(obj):
            try:
                out.add(float(m.replace(",", "")))
            except ValueError:
                pass
    elif isinstance(obj, dict):
        for v in obj.values():
            _numbers_in(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _numbers_in(v, out)


def is_grounded(text: str, data: dict[str, Any]) -> bool:
    """True if every figure >= 100 in ``text`` appears (within rounding) in ``data``."""
    allowed: set[float] = set()
    _numbers_in(data, allowed)
    # Revenue shown as rupees can be rounded; percentages get rounded too.
    allowed |= {round(x) for x in allowed} | {round(abs(x)) for x in allowed}
    for m in _NUM.findall(text):
        try:
            value = float(m.replace(",", ""))
        except ValueError:
            continue
        if value >= 100 and not any(abs(value - a) <= 1.0 for a in allowed):
            return False
    return True


# -- deterministic fallback --------------------------------------------------------

_L = {
    "en": {
        "hi": "Good morning {owner}!", "rev": "Yesterday: {rev}", "vs_up": "{pct}% above a usual {wd}",
        "vs_down": "{pct}% below a usual {wd}", "stock": "Stock needing attention:", "left": "{n} {unit} left",
        "out": "out of stock", "orders": "{n} reorder draft(s) are waiting for your OK. Tap a button below.",
        "fest": "{name} is in {n} day(s).", "calm": "Stock looks fine today.",
    },
    "hi": {
        "hi": "सुप्रभात {owner}!", "rev": "कल की बिक्री: {rev}", "vs_up": "सामान्य {wd} से {pct}% ज़्यादा",
        "vs_down": "सामान्य {wd} से {pct}% कम", "stock": "ध्यान देने लायक स्टॉक:", "left": "{n} {unit} बचा है",
        "out": "स्टॉक खत्म", "orders": "{n} ऑर्डर आपकी मंज़ूरी का इंतज़ार कर रहे हैं। नीचे बटन दबाइए।",
        "fest": "{name} {n} दिन में है।", "calm": "आज स्टॉक ठीक है।",
    },
    "te": {
        "hi": "శుభోదయం {owner}!", "rev": "నిన్నటి అమ్మకాలు: {rev}", "vs_up": "సాధారణ {wd} కంటే {pct}% ఎక్కువ",
        "vs_down": "సాధారణ {wd} కంటే {pct}% తక్కువ", "stock": "శ్రద్ధ అవసరమైన స్టాక్:", "left": "{n} {unit} మిగిలింది",
        "out": "స్టాక్ అయిపోయింది", "orders": "{n} ఆర్డర్లు మీ ఆమోదం కోసం ఎదురు చూస్తున్నాయి. కింద బటన్ నొక్కండి.",
        "fest": "{name} {n} రోజుల్లో ఉంది.", "calm": "ఈ రోజు స్టాక్ బాగుంది.",
    },
}


def fallback_text(data: dict[str, Any]) -> str:
    s = _L.get(data["language"], _L["en"])
    y = data["yesterday"]
    lines = [s["hi"].format(owner=data["owner"]), s["rev"].format(rev=format_inr(y["revenue"]))]
    pct = y.get("pct_vs_baseline")
    if pct is not None and abs(pct) >= 5:
        key = "vs_up" if pct > 0 else "vs_down"
        lines[-1] += f" ({s[key].format(pct=abs(round(pct)), wd=y['weekday'])})"
    if data["stock_alerts"]:
        lines.append(s["stock"])
        for a in data["stock_alerts"]:
            what = s["out"] if a["risk"] == "stockout" else s["left"].format(n=f"{a['stock']:g}", unit=a["unit"])
            lines.append(f"- {a['name']}: {what}")
    else:
        lines.append(s["calm"])
    for f in data["upcoming_festivals"][:1]:
        lines.append(s["fest"].format(name=f["name"], n=f["in_days"]))
    if data["pending_orders"]:
        lines.append(s["orders"].format(n=len(data["pending_orders"])))
    return "\n".join(lines)


def order_buttons(data_or_drafts: list[dict[str, Any]], lang: str) -> list[list[Button]]:
    """One row per pending draft: Approve / Cancel."""
    rows = []
    for d in data_or_drafts:
        rows.append([
            Button(f"{t('approve_btn', lang)} {d['id']}", callback_data=f"approve:{d['id']}"),
            Button(f"{t('reject_btn', lang)} {d['id']}", callback_data=f"reject:{d['id']}"),
        ])
    return rows
