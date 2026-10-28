from __future__ import annotations

import pytest

from dukaanpilot.data import importers
from dukaanpilot.data.models import Shop
from dukaanpilot.data.sqlite_repo import SqliteRepository

PRODUCTS = """sku,name,unit,price,cost,stock,reorder_point,lead_time_days,supplier_id,pack_size,review_days,aliases
rice,Sona Masoori 5kg,bag,320,270,40,15,3,wh,5,7,rice|biyyam
OIL,Oil 1L,bottle,150,,22,10,3,wh,,,
"""


@pytest.fixture
def repo():
    r = SqliteRepository(":memory:")
    r.upsert_shop(Shop("s", "S", "O"))
    importers.add_supplier(r, "s", "wh:Traders:919812345678:te")
    return r


def test_products_import_with_defaults_and_aliases(repo):
    assert importers.import_products(repo, "s", PRODUCTS) == 2
    rice, oil = repo.get_product("s", "RICE"), repo.get_product("s", "OIL")
    assert rice.aliases == ["rice", "biyyam"] and rice.pack_size == 5
    assert oil.cost == pytest.approx(127.5) and oil.pack_size == 1 and oil.review_days == 7


def test_one_bad_row_writes_nothing_and_names_the_line(repo):
    bad = PRODUCTS + "X,Thing,pc,10,5,3,1,2,ghost,1,7,\n"
    with pytest.raises(importers.ImportErrorReport, match="line 4: unknown supplier_id 'ghost'"):
        importers.import_products(repo, "s", bad)
    assert repo.list_products("s") == []


def test_sales_import_checks_skus_and_numbers(repo):
    importers.import_products(repo, "s", PRODUCTS)
    assert importers.import_sales(repo, "s", "date,sku,qty,revenue\n2026-10-01,rice,3,960\n") == 1
    with pytest.raises(importers.ImportErrorReport) as e:
        importers.import_sales(repo, "s", "date,sku,qty,revenue\n2026-10-01,nope,3,9\n2026-13-01,rice,1,1\n2026-10-02,rice,-1,1\n")
    assert len(e.value.problems) == 3


def test_missing_columns_and_bad_supplier_spec(repo):
    with pytest.raises(importers.ImportErrorReport, match="missing column"):
        importers.import_sales(repo, "s", "date,sku\n")
    with pytest.raises(ValueError):
        importers.add_supplier(repo, "s", "bad-spec")
