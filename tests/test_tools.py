from __future__ import annotations

import inspect
from datetime import date

import pytest

from dukaanpilot import tools as tool_pkg
from dukaanpilot.tools import (
    cancel_reorder_draft,
    check_inventory,
    create_reorder_drafts,
    detect_sales_anomalies,
    explain_sales_change,
    forecast_demand,
    get_sales_summary,
    get_shop_profile,
    get_stock_alerts,
    get_upcoming_festivals,
    list_reorder_drafts,
    log_restock,
    log_sale,
    request_reorder_approval,
    undo_sale,
    update_preference,
)

from .conftest import make_world, tool_ctx


def test_no_tool_can_approve_an_order():
    """The model must have no way to approve or send a supplier order."""
    names = {t.__name__ for t in tool_pkg.ALL_TOOLS}
    assert not {n for n in names if "approve_" in n or n.startswith("send_") or "send_order" in n}
    assert "request_reorder_approval" in names  # it only asks the human


def test_no_tool_takes_a_shop_id_from_the_model():
    for t in tool_pkg.ALL_TOOLS:
        assert "shop_id" not in inspect.signature(t).parameters, t.__name__


def test_read_tools_return_real_numbers(world):
    ctx = tool_ctx()
    s = get_sales_summary("yesterday", tool_context=ctx)
    assert s["status"] == "success" and s["revenue"] > 0 and s["revenue_formatted"].startswith("₹")
    assert s["vs_usual_day"]["pct_vs_baseline"] < -15
    week = get_sales_summary("last_7_days", tool_context=ctx)
    assert week["days"] == 7 and "vs_usual_day" not in week

    inv = check_inventory("palu", tool_context=ctx)
    assert inv["items"][0]["name"] == "Amul Milk 500ml" and inv["items"][0]["risk"] == "stockout"
    assert check_inventory("", tool_context=ctx)["items"][0]["risk"] == "stockout"

    alerts = get_stock_alerts(tool_context=ctx)
    assert alerts["count"] >= 6 and alerts["alerts"][0]["risk"] == "stockout"

    f = forecast_demand("rice", 5, tool_context=ctx)
    assert len(f["forecast"]) == 5 and f["total_units"] > 100 and f["risk"] == "critical"

    why = explain_sales_change("yesterday", tool_context=ctx)
    assert why["likely_cause"] == "stock_outs"
    assert detect_sales_anomalies(7, tool_context=ctx)["count"] >= 1
    assert get_upcoming_festivals(10, tool_context=ctx)["festivals"][0]["name"] == "Dussehra"
    profile = get_shop_profile(tool_context=ctx)
    assert profile["owner"] == "Ramesh" and profile["language"] == "te"


def test_bad_input_becomes_a_clear_error_not_a_crash(world):
    ctx = tool_ctx()
    assert get_sales_summary("someday", tool_context=ctx)["status"] == "error"
    err = check_inventory("laptop", tool_context=ctx)
    assert err["status"] == "error" and "No product matches" in err["error_message"]
    assert list_reorder_drafts("bogus", tool_context=ctx)["status"] == "error"


def test_ambiguous_product_asks_instead_of_guessing(world):
    from dukaanpilot.data.models import Product

    world.repo.upsert_product(Product(world.shop_id, "RICE2", "Basmati Rice 1kg", "kg", 120, 100, 50, 10, 3, "wholesale", aliases=["basmati"]))
    res = log_sale("rice", 2, tool_context=tool_ctx())
    assert res["status"] == "error" and "several products" in res["error_message"]
    assert world.repo.get_product(world.shop_id, "RICE").stock == 70  # nothing changed


def test_log_sale_updates_stock_and_audit_then_undo_restores(world):
    ctx = tool_ctx()
    before = world.repo.get_product(world.shop_id, "ATTA").stock
    res = log_sale("atta", 2, tool_context=ctx)
    assert res["status"] == "success" and res["revenue"] == 510.0 and res["stock_now"] == before - 2
    assert "undo" in res["note"]
    assert any(a["action"] == "sale_logged" for a in world.repo.list_audit(world.shop_id))

    undone = undo_sale(res["sale_id"], tool_context=ctx)
    assert undone["status"] == "success"
    assert world.repo.get_product(world.shop_id, "ATTA").stock == before
    assert undo_sale(res["sale_id"], tool_context=ctx)["status"] == "error"  # not twice


def test_undo_refuses_history_the_agent_did_not_write(world):
    first_seeded_sale = world.repo.sales_between(world.shop_id, date(2026, 7, 1), date(2026, 10, 16))[0]
    res = undo_sale(first_seeded_sale.id, tool_context=tool_ctx())
    assert res["status"] == "error" and "recorded myself" in res["error_message"]
    assert world.repo.sales_between(world.shop_id, first_seeded_sale.sale_date, first_seeded_sale.sale_date)  # still there


@pytest.mark.parametrize("qty", [0, -3, 10**7])
def test_quantities_are_range_checked(world, qty):
    assert log_sale("milk", qty, tool_context=tool_ctx())["status"] == "error"
    assert log_restock("milk", qty, tool_context=tool_ctx())["status"] == "error"


def test_sale_dates_are_limited(world):
    ctx = tool_ctx()
    assert log_sale("milk", 1, sale_date="2026-10-17", tool_context=ctx)["status"] == "error"  # future
    assert log_sale("milk", 1, sale_date="2026-09-01", tool_context=ctx)["status"] == "error"  # too old
    assert log_sale("milk", 1, sale_date="yesterday", tool_context=ctx)["status"] == "success"


def test_overselling_warns_and_floors_stock_at_zero(world):
    res = log_sale("rice", 500, tool_context=tool_ctx())
    assert res["stock_now"] == 0 and res["warnings"]


def test_restock_adds_stock(world):
    res = log_restock("milk", 120, tool_context=tool_ctx())
    assert res["stock_now"] == 120
    assert check_inventory("milk", tool_context=tool_ctx())["items"][0]["risk"] != "stockout"


def test_preferences_are_whitelisted_and_validated(world):
    ctx = tool_ctx()
    assert update_preference("briefing_hour", "7", tool_context=ctx)["saved"] == {"briefing_hour": 7}
    assert update_preference("briefing_hour", "25", tool_context=ctx)["status"] == "error"
    assert update_preference("reply_language", "fr", tool_context=ctx)["status"] == "error"
    assert update_preference("is_admin", "true", tool_context=ctx)["status"] == "error"
    assert update_preference("reply_language", "hi", tool_context=ctx)["status"] == "success"
    assert world.repo.get_shop(world.shop_id).settings == {"briefing_hour": 7, "reply_language": "hi"}


def test_draft_flow_through_tools(world):
    ctx = tool_ctx(chat_id="1001")
    created = create_reorder_drafts(tool_context=ctx)
    assert len(created["new_drafts"]) == 2
    again = create_reorder_drafts(tool_context=ctx)
    assert again["new_drafts"] == [] and len(again["already_waiting"]) == 2
    draft_id = created["new_drafts"][0]["id"]
    assert list_reorder_drafts("pending", tool_context=ctx)["drafts"]
    cancelled = cancel_reorder_draft(draft_id, tool_context=ctx)
    assert cancelled["status"] == "success"
    assert cancel_reorder_draft(draft_id, tool_context=ctx)["status"] == "error"


async def test_request_approval_only_sends_buttons(world):
    ctx = tool_ctx(chat_id="1001")
    draft_id = create_reorder_drafts(tool_context=ctx)["new_drafts"][0]["id"]
    res = await request_reorder_approval(draft_id, tool_context=ctx)
    assert res["sent"] is True
    chat, text, buttons = world.messenger.sent[-1]
    assert chat == "1001" and draft_id in text
    assert [b.callback_data for b in buttons[0]] == [f"approve:{draft_id}", f"reject:{draft_id}"]
    assert world.repo.get_draft(world.shop_id, draft_id).status == "pending"  # nothing approved
    assert (await request_reorder_approval("RDNOPE", tool_context=ctx))["status"] == "error"


def test_conversation_without_a_shop_is_refused_in_production():
    w = make_world(environment="prod")
    res = get_sales_summary("yesterday", tool_context=tool_ctx(shop_id=None))
    assert res["status"] == "error" and "not linked" in res["error_message"]
    assert get_sales_summary("yesterday", tool_context=tool_ctx(shop_id="demo-kirana"))["status"] == "success"
    assert w  # keep the container alive for the assertion above


def test_unknown_shop_id_in_state_is_refused(world):
    res = get_sales_summary("yesterday", tool_context=tool_ctx(shop_id="someone-elses-shop"))
    assert res["status"] == "error"
