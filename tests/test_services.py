from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from dukaanpilot.services import analytics, briefing, inventory, reorders
from dukaanpilot.services.festivals import FestivalCalendar

from .conftest import NOW, TODAY

YESTERDAY = TODAY - timedelta(days=1)


def test_yesterday_is_flagged_as_unusually_low(world):
    found = analytics.detect_anomalies(world.repo, world.shop_id, TODAY, days=3)
    assert any(a["date"] == YESTERDAY.isoformat() and a["direction"] == "below" for a in found)
    cmp = analytics.compare_day(world.repo, world.shop_id, YESTERDAY)
    assert cmp["pct_vs_baseline"] < -15


def test_explanation_blames_the_dairy_stockouts(world):
    ex = analytics.explain_change(world.repo, world.shop_id, YESTERDAY)
    assert ex["likely_cause"] == "stock_outs"
    out_of_stock = {d["name"] for d in ex["top_drivers"] if d["out_of_stock_that_day"]}
    assert {"Amul Milk 500ml", "Curd 400g"} <= out_of_stock


def test_explain_a_normal_day_is_not_dramatic(world):
    ex = analytics.explain_change(world.repo, world.shop_id, TODAY - timedelta(days=9))
    assert ex["likely_cause"] in {"normal_variation", "above_usual_demand", "broad_decline"}
    assert abs(ex["pct_vs_expected"]) < 25


def test_period_summary_and_top_products(world):
    s = analytics.period_summary(world.repo, world.shop_id, TODAY - timedelta(days=7), YESTERDAY)
    assert s["days"] == 7 and s["revenue"] > 50_000
    top = analytics.top_products(world.repo, world.shop_id, TODAY - timedelta(days=7), YESTERDAY, 3)
    assert len(top) == 3 and top[0]["revenue"] >= top[1]["revenue"] >= top[2]["revenue"]


def _by_name(world, calendar=None):
    cal = calendar or world.c.calendar
    return {s.product.sku: s for s in inventory.stock_statuses(world.repo, world.shop_id, TODAY, cal)}


def test_risk_levels_match_the_story(world):
    st = _by_name(world)
    assert st["MILK"].risk == "stockout" and st["MILK"].days_cover == 0
    assert st["RICE"].risk == "critical"
    assert st["OIL"].risk == "critical"
    assert st["ATTA"].risk == "ok" and st["ATTA"].suggested_qty == 0


def test_festival_makes_the_same_stock_run_out_sooner(world):
    with_fest = _by_name(world)["RICE"].days_cover
    without = _by_name(world, FestivalCalendar([]))["RICE"].days_cover
    assert with_fest < without


def test_perishables_are_not_overordered(world):
    milk = _by_name(world)["MILK"]
    # ~60/day over lead 1 + review 2 days is ~180, plus safety stock. A weekly plan would be 600+.
    assert 150 <= milk.suggested_qty <= 320
    rice = _by_name(world)["RICE"]
    assert rice.suggested_qty > milk.suggested_qty  # staples are bought in bigger lots
    assert rice.suggested_qty % rice.product.pack_size == 0


def test_stockout_days_do_not_drag_demand_down(world):
    milk = _by_name(world)["MILK"]
    # Yesterday milk sold ~12 packets because it ran out; the model must still expect about 60.
    assert 45 <= milk.daily_demand <= 80


def test_many_stockout_days_are_excluded_when_learning_demand():
    """Five stock-out days in the last two weeks must not pull a steady 20/day item down to ~14."""
    from dukaanpilot.data.models import Product, SaleRow, Shop, Supplier
    from dukaanpilot.data.sqlite_repo import SqliteRepository

    repo = SqliteRepository(":memory:")
    repo.upsert_shop(Shop("s", "S", "O"))
    repo.upsert_supplier(Supplier("sup", "s", "Sup", "919"))
    repo.upsert_product(Product("s", "X", "Item", "pc", 10, 8, stock=100, reorder_point=10, lead_time_days=2, supplier_id="sup"))
    sales, snaps = [], {}
    for i in range(1, 57):
        d = TODAY - timedelta(days=i)
        out = i <= 14 and i % 3 == 0  # stock-out days: no sales because nothing was on the shelf
        if not out:
            sales.append(SaleRow("s", d, "X", 20.0, 200.0))
        snaps[d] = {"X": 0.0 if out else 50.0}
    repo.add_sales(sales)
    for d, snap in snaps.items():
        repo.save_snapshot("s", d, snap)
    status = inventory.stock_statuses(repo, "s", TODAY, FestivalCalendar([]))[0]
    assert status.daily_demand == pytest.approx(20.0, abs=0.5)
    assert status.model.usable_days == 56 - 4  # the four stock-out days (i = 3, 6, 9, 12) were dropped


def test_one_draft_per_supplier_and_no_duplicates(world):
    shop = world.repo.get_shop(world.shop_id)
    statuses = inventory.stock_statuses(world.repo, world.shop_id, TODAY, world.c.calendar)
    drafts = reorders.create_drafts(world.repo, shop, statuses, NOW)
    assert {d.supplier_id for d in drafts} == {"dairy", "wholesale"}
    again = reorders.create_drafts(world.repo, shop, statuses, NOW)
    assert again == []
    dairy = next(d for d in drafts if d.supplier_id == "dairy")
    assert "నమస్కారం" in dairy.message and "Amul Milk 500ml" in dairy.message  # Telugu header, English product names


def test_approval_gives_a_whatsapp_link_and_cannot_happen_twice(world):
    shop = world.repo.get_shop(world.shop_id)
    draft = reorders.create_drafts(world.repo, shop, inventory.stock_statuses(world.repo, world.shop_id, TODAY, world.c.calendar), NOW)[0]
    decision = reorders.approve(world.repo, shop, draft.id.lower(), "owner:1001", NOW)
    assert decision.draft.status == "approved"
    assert decision.whatsapp_link.startswith("https://wa.me/9190000000")
    assert "%0A" in decision.whatsapp_link  # message is URL-encoded
    with pytest.raises(reorders.InvalidTransition):
        reorders.approve(world.repo, shop, draft.id, "owner:1001", NOW)
    with pytest.raises(reorders.InvalidTransition):
        reorders.reject(world.repo, shop, draft.id, "owner:1001", NOW)
    with pytest.raises(reorders.DraftNotFound):
        reorders.approve(world.repo, shop, "RDNOPE", "owner:1001", NOW)
    actions = [a["action"] for a in world.repo.list_audit(world.shop_id)]
    assert "reorder_draft_approved" in actions


def test_stale_drafts_expire(world):
    shop = world.repo.get_shop(world.shop_id)
    reorders.create_drafts(world.repo, shop, inventory.stock_statuses(world.repo, world.shop_id, TODAY, world.c.calendar), NOW)
    later = NOW + timedelta(hours=49)
    assert reorders.expire_stale(world.repo, shop, later, 48) == 2
    assert world.repo.list_drafts(world.shop_id, "pending") == []


def test_briefing_numbers_and_grounding_guard(world):
    shop = world.repo.get_shop(world.shop_id)
    data = briefing.compute_briefing(world.repo, shop, NOW, world.c.calendar)
    assert data["anomaly"] and data["explanation"]["likely_cause"] == "stock_outs"
    assert data["upcoming_festivals"][0] == {"name": "Dussehra", "in_days": 4}
    honest = briefing.fallback_text(data)
    assert briefing.is_grounded(honest, data)
    revenue = f"{data['yesterday']['revenue']:,.0f}"
    assert revenue in honest.replace("₹", "")
    assert not briefing.is_grounded("Sales were ₹54,321 yesterday", data)
    assert briefing.is_grounded("Good morning Ramesh, sales were low, 22% below usual", data)


@pytest.mark.parametrize("lang,marker", [("en", "Good morning"), ("hi", "सुप्रभात"), ("te", "శుభోదయం")])
def test_fallback_briefing_speaks_the_owners_language(world, lang, marker):
    shop = world.repo.get_shop(world.shop_id)
    shop.settings["reply_language"] = lang
    data = briefing.compute_briefing(world.repo, shop, NOW, world.c.calendar)
    assert marker in briefing.fallback_text(data)


def test_midnight_utc_is_still_yesterday_in_india(world):
    """06:00 UTC on the 15th is 11:30 IST on the 15th: 'today' must follow the shop, not the server."""
    shop = world.repo.get_shop(world.shop_id)
    data = briefing.compute_briefing(world.repo, shop, datetime(2026, 10, 15, 20, 0, tzinfo=UTC), world.c.calendar)
    assert data["date"] == "2026-10-16"
