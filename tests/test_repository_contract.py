"""One behaviour suite, run against every storage backend.

SQLite always runs. Firestore runs only when an emulator is available:

    gcloud emulators firestore start --host-port=localhost:8686
    FIRESTORE_EMULATOR_HOST=localhost:8686 pytest tests/test_repository_contract.py
"""

from __future__ import annotations

import os
import uuid
from datetime import date

import pytest

from dukaanpilot.data.models import Product, ReorderDraft, ReorderItem, SaleRow, Shop, Supplier
from dukaanpilot.data.sqlite_repo import SqliteRepository

D = date(2026, 10, 15)


def _backends():
    yield pytest.param("sqlite", id="sqlite")
    yield pytest.param(
        "firestore",
        id="firestore",
        marks=pytest.mark.skipif(not os.getenv("FIRESTORE_EMULATOR_HOST"), reason="needs the Firestore emulator"),
    )


@pytest.fixture(params=list(_backends()))
def repo(request):
    if request.param == "sqlite":
        return SqliteRepository(":memory:")
    from google.cloud import firestore

    from dukaanpilot.data.firestore_repo import FirestoreRepository

    return FirestoreRepository(firestore.Client(project="dukaanpilot-test"))


@pytest.fixture
def shop(repo):
    sid = f"s-{uuid.uuid4().hex[:8]}"  # unique per test so a shared emulator never leaks state between tests
    repo.upsert_shop(Shop(sid, "Test Store", "Owner", language="te", join_code="JOIN-1", settings={"a": 1}))
    repo.upsert_supplier(Supplier("sup", sid, "Supplier", "919000000001"))
    repo.upsert_product(Product(sid, "MILK", "Milk", "pkt", 30, 25, 10, 5, 1, "sup", 12, ["paal"], 2))
    return sid


def test_shop_roundtrip_and_lookups(repo, shop):
    got = repo.get_shop(shop)
    assert got.language == "te" and got.settings == {"a": 1}
    assert repo.get_shop_by_join_code(" join-1 ").id == shop
    assert repo.get_shop("nope") is None
    repo.link_chat(shop, "42")
    assert repo.get_shop_by_chat("42").id == shop
    repo.set_setting(shop, "briefing_hour", 7)
    assert repo.get_shop(shop).settings == {"a": 1, "briefing_hour": 7}
    with pytest.raises(KeyError):
        repo.set_setting("nope", "x", 1)


def test_a_chat_belongs_to_one_shop(repo, shop):
    other = f"o-{uuid.uuid4().hex[:8]}"
    repo.upsert_shop(Shop(other, "Other", "O"))
    repo.link_chat(shop, "77")
    repo.link_chat(other, "77")
    assert repo.get_shop_by_chat("77").id == other
    assert repo.get_shop(shop).telegram_chat_id is None


def test_products_keep_every_field(repo, shop):
    p = repo.get_product(shop, "MILK")
    assert (p.pack_size, p.aliases, p.review_days, p.lead_time_days) == (12, ["paal"], 2, 1)
    assert [x.sku for x in repo.list_products(shop)] == ["MILK"]
    assert repo.get_supplier(shop, "sup").phone == "919000000001"
    assert repo.get_product(shop, "NOPE") is None


def test_stock_never_goes_negative(repo, shop):
    assert repo.adjust_stock(shop, "MILK", -4) == 6
    assert repo.adjust_stock(shop, "MILK", -100) == 0
    repo.set_stock(shop, "MILK", 9)
    assert repo.get_product(shop, "MILK").stock == 9
    with pytest.raises(KeyError):
        repo.adjust_stock(shop, "NOPE", 1)


def test_sales_can_be_recorded_queried_and_undone(repo, shop):
    sale_id = repo.record_sale(shop, D, "MILK", 2, 60)
    repo.add_sales([SaleRow(shop, date(2026, 10, 1), "MILK", 5, 150), SaleRow(shop, date(2026, 11, 1), "MILK", 1, 30)])
    rows = repo.sales_between(shop, date(2026, 10, 1), date(2026, 10, 31))
    assert [(r.sale_date, r.qty) for r in rows] == [(date(2026, 10, 1), 5), (D, 2)]
    gone = repo.delete_sale(shop, sale_id)
    assert gone.qty == 2 and repo.delete_sale(shop, sale_id) is None
    assert len(repo.sales_between(shop, date(2026, 10, 1), date(2026, 10, 31))) == 1


def test_snapshots_are_returned_per_day(repo, shop):
    repo.save_snapshot(shop, D, {"MILK": 3.0})
    repo.save_snapshot(shop, D, {"MILK": 4.0})
    repo.save_snapshot(shop, date(2026, 9, 1), {"MILK": 1.0})
    assert repo.snapshots_between(shop, date(2026, 10, 1), date(2026, 10, 31)) == {D: {"MILK": 4.0}}


def _draft(shop, draft_id="RD1"):
    return ReorderDraft(draft_id, shop, "sup", "Supplier", [ReorderItem("MILK", "Milk", 24, "pkt")],
                        "pending", "msg", "why", "2026-10-15T01:00:00+00:00")


def test_draft_approval_is_compare_and_set(repo, shop):
    repo.save_draft(_draft(shop))
    assert repo.transition_draft(shop, "rd1", "pending", "approved", "owner:1", "2026-10-15T02:00:00+00:00")
    assert not repo.transition_draft(shop, "RD1", "pending", "rejected", "owner:1", "2026-10-15T02:01:00+00:00")
    d = repo.get_draft(shop, "rd1")
    assert (d.status, d.decided_by, d.items[0].qty) == ("approved", "owner:1", 24)
    assert not repo.transition_draft(shop, "RDNOPE", "pending", "approved", "x", "t")


def test_draft_listing_filters_and_orders_newest_first(repo, shop):
    older, newer = _draft(shop, "RD1"), _draft(shop, "RD2")
    newer.created_at = "2026-10-16T01:00:00+00:00"
    repo.save_draft(older)
    repo.save_draft(newer)
    repo.transition_draft(shop, "RD1", "pending", "rejected", "o", "t")
    assert [d.id for d in repo.list_drafts(shop)] == ["RD2", "RD1"]
    assert [d.id for d in repo.list_drafts(shop, "pending")] == ["RD2"]


def test_notifications_are_claimed_exactly_once(repo, shop):
    assert not repo.was_notified(shop, "briefing", "2026-10-15")
    assert repo.mark_notified(shop, "briefing", "2026-10-15", "t")
    assert not repo.mark_notified(shop, "briefing", "2026-10-15", "t")
    assert repo.was_notified(shop, "briefing", "2026-10-15")
    repo.unmark_notified(shop, "briefing", "2026-10-15")
    assert repo.mark_notified(shop, "briefing", "2026-10-15", "t")


def test_telegram_updates_are_claimed_exactly_once(repo):
    update = uuid.uuid4().hex
    assert repo.claim_update("telegram", update)
    assert not repo.claim_update("telegram", update)


def test_audit_log_is_newest_first(repo, shop):
    repo.add_audit(shop, "agent", "first", {"d": date(2026, 10, 1)}, "2026-10-15T01:00:00+00:00")
    repo.add_audit(shop, "owner", "second", {}, "2026-10-15T02:00:00+00:00")
    log = repo.list_audit(shop)
    assert [e["action"] for e in log] == ["second", "first"]
    assert log[1]["detail"] == {"d": "2026-10-01"}


def test_reset_removes_a_shops_data_but_not_the_shop(repo, shop):
    repo.record_sale(shop, D, "MILK", 1, 30)
    repo.save_draft(_draft(shop))
    repo.add_audit(shop, "a", "x", {}, "t")
    repo.reset_shop_data(shop)
    assert repo.sales_between(shop, D, D) == [] and repo.list_drafts(shop) == []
    assert repo.list_products(shop) == [] and repo.list_audit(shop) == []
    assert repo.get_shop(shop) is not None
