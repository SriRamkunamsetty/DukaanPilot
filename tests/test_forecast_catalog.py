from __future__ import annotations

from datetime import date, timedelta

from dukaanpilot.data.models import Product
from dukaanpilot.services import catalog, forecast
from dukaanpilot.services.timeutil import format_inr, resolve_period

TODAY = date(2026, 10, 16)


def _history(fn, days=56):
    return {TODAY - timedelta(days=i): fn(TODAY - timedelta(days=i)) for i in range(1, days + 1)}


def test_model_learns_weekend_peak():
    hist = _history(lambda d: 20.0 if d.weekday() >= 5 else 10.0)
    m = forecast.fit(hist, TODAY)
    saturday = date(2026, 10, 17)
    monday = date(2026, 10, 19)
    assert m.predict(saturday) > m.predict(monday) * 1.3


def test_festival_uplift_scales_forecast():
    hist = _history(lambda d: 10.0)
    m = forecast.fit(hist, TODAY)
    base = sum(forecast.forecast(m, TODAY, 5))
    boosted = sum(forecast.forecast(m, TODAY, 5, lambda d: 1.5))
    assert boosted == base * 1.5


def test_empty_history_is_safe():
    m = forecast.fit({}, TODAY)
    assert m.predict(TODAY) == 0
    assert forecast.days_of_cover(10, forecast.forecast(m, TODAY, 7)) == forecast.MAX_COVER_DAYS
    assert forecast.reorder_quantity(m, 0, 2, 7, TODAY) == 0


def test_days_of_cover_walks_the_forecast():
    assert forecast.days_of_cover(0, [5, 5]) == 0
    assert forecast.days_of_cover(10, [5, 5, 5]) >= 2
    assert round(forecast.days_of_cover(7.5, [5, 5, 5]), 1) == 1.5


def test_reorder_quantity_rounds_up_to_pack_and_covers_lead_time():
    m = forecast.fit(_history(lambda d: 10.0), TODAY)
    qty = forecast.reorder_quantity(m, stock=5, lead_time_days=2, review_days=3, start=TODAY, pack_size=12)
    assert qty % 12 == 0
    assert qty >= 10 * 5 - 5  # five days of demand minus what is on the shelf
    assert forecast.reorder_quantity(m, stock=1000, lead_time_days=2, review_days=3, start=TODAY) == 0


def test_dropping_stockout_days_raises_estimated_demand():
    """Zero sales on a stock-out day is lost demand, so it must not drag the average down."""
    with_zeros = _history(lambda d: 0.0 if d.day % 4 == 0 else 20.0)
    cleaned = {d: v for d, v in with_zeros.items() if v > 0}
    assert forecast.fit(cleaned, TODAY).level > forecast.fit(with_zeros, TODAY).level


def _p(sku, name, aliases=()):
    return Product("s", sku, name, "u", 1, 1, 1, 1, 1, "x", aliases=list(aliases))


CATALOG = [
    _p("MILK", "Amul Milk 500ml", ["palu", "పాలు", "doodh", "दूध"]),
    _p("RICE", "Sona Masoori Rice 1kg", ["biyyam", "chawal"]),
    _p("RICE2", "Basmati Rice 1kg", ["basmati"]),
    _p("OIL", "Sunflower Oil 1L", ["nune", "tel"]),
]


def test_catalog_matches_local_words_and_scripts():
    for q in ("milk", "palu", "పాలు", "दूध", "doodh", "MILK!"):
        product, cands = catalog.resolve(CATALOG, q)
        assert product and product.sku == "MILK", q


def test_catalog_asks_when_ambiguous_instead_of_guessing():
    product, candidates = catalog.resolve(CATALOG, "rice")
    assert product is None
    assert {p.sku for p in candidates} == {"RICE", "RICE2"}


def test_catalog_unknown_product():
    assert catalog.resolve(CATALOG, "laptop") == (None, [])
    assert catalog.resolve(CATALOG, "   ") == (None, [])


def test_resolve_period_variants():
    assert resolve_period("yesterday", TODAY)[:2] == (date(2026, 10, 15),) * 2
    assert resolve_period("last 7 days", TODAY)[:2] == (date(2026, 10, 10), TODAY)
    assert resolve_period("2026-10-01..2026-10-05", TODAY)[:2] == (date(2026, 10, 1), date(2026, 10, 5))
    for bad in ("someday", "2026-10-05..2026-10-01", "2020-01-01..2026-10-01"):
        try:
            resolve_period(bad, TODAY)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_format_inr_uses_indian_grouping():
    assert format_inr(0) == "₹0"
    assert format_inr(999) == "₹999"
    assert format_inr(12345) == "₹12,345"
    assert format_inr(1234567) == "₹12,34,567"
    assert format_inr(-4500.4) == "-₹4,500"
