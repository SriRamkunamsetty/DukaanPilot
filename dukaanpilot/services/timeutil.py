"""Date helpers: shop-local time, period parsing, Indian currency formatting."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from ..data.models import Shop


def local_now(shop: Shop, now_utc: datetime) -> datetime:
    return now_utc.astimezone(ZoneInfo(shop.timezone))


def local_today(shop: Shop, now_utc: datetime) -> date:
    return local_now(shop, now_utc).date()


def resolve_period(period: str, today: date) -> tuple[date, date, str]:
    """Turn a human period name into ``(start, end, label)``, both inclusive.

    Accepted: today, yesterday, this_week, last_7_days, last_30_days, this_month,
    ``YYYY-MM-DD`` or ``YYYY-MM-DD..YYYY-MM-DD``. Raises ``ValueError`` otherwise.
    """
    key = (period or "yesterday").strip().lower().replace(" ", "_").replace("-", "_")
    if key == "today":
        return today, today, "today"
    if key == "yesterday":
        y = today - timedelta(days=1)
        return y, y, "yesterday"
    if key in {"this_week", "week"}:
        return today - timedelta(days=today.weekday()), today, "this week"
    if key in {"last_7_days", "7_days", "past_week"}:
        return today - timedelta(days=6), today, "last 7 days"
    if key in {"last_30_days", "30_days", "past_month"}:
        return today - timedelta(days=29), today, "last 30 days"
    if key in {"this_month", "month"}:
        return today.replace(day=1), today, "this month"
    raw = (period or "").strip()
    try:
        if ".." in raw:
            a, b = raw.split("..", 1)
            start, end = date.fromisoformat(a.strip()), date.fromisoformat(b.strip())
        else:
            start = end = date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(
            f"Unknown period {period!r}. Use today, yesterday, this_week, last_7_days, "
            "last_30_days, this_month, YYYY-MM-DD or YYYY-MM-DD..YYYY-MM-DD."
        ) from exc
    if end < start:
        raise ValueError("The end date is before the start date.")
    if (end - start).days > 400:
        raise ValueError("That period is too long; ask for at most about a year.")
    return start, end, f"{start.isoformat()}..{end.isoformat()}" if start != end else start.isoformat()


def format_inr(amount: float) -> str:
    """12345678 -> ₹1,23,45,678 (Indian digit grouping)."""
    n = int(round(amount))
    sign = "-" if n < 0 else ""
    s = str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups: list[str] = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups + [tail])
    return f"{sign}₹{s}"
