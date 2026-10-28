"""Deterministic demo data: a small kirana shop with 90 days of history.

The story baked in (so a demo always has something real to discover):
yesterday the dairy supplier missed a delivery, so milk, curd, bread and eggs sold
out and revenue dropped sharply; rice and oil are running low just before Dussehra.
Everything is synthetic.
"""

from __future__ import annotations

import math
import random
from datetime import date, timedelta

from .models import Product, SaleRow, Shop, Supplier
from .repository import Repository

DEMO_SHOP_ID = "demo-kirana"
DEMO_JOIN_CODE = "DEMO-KIRANA"
HISTORY_DAYS = 90
WEEKDAY_FACTOR = [0.95, 0.90, 0.95, 1.00, 1.10, 1.25, 1.20]  # Mon..Sun

DAIRY = "dairy"
WHOLESALE = "wholesale"

# sku, name, unit, price, cost, base/day, reorder_point, lead_days, supplier, pack, aliases
_CATALOG = [
    ("MILK", "Amul Milk 500ml", "packet", 28, 25, 60, 40, 1, DAIRY, 12, ["milk", "palu", "పాలు", "doodh", "दूध", "milk packet"]),
    ("CURD", "Curd 400g", "cup", 40, 33, 18, 12, 1, DAIRY, 12, ["curd", "perugu", "పెరుగు", "dahi", "दही"]),
    ("BREAD", "Bread", "loaf", 40, 34, 14, 10, 1, DAIRY, 10, ["bread", "బ్రెడ్", "double roti"]),
    ("EGGS", "Eggs", "piece", 7, 6, 70, 120, 2, DAIRY, 30, ["egg", "eggs", "guddu", "గుడ్డు", "anda", "अंडा"]),
    ("RICE", "Sona Masoori Rice 1kg", "kg", 62, 55, 35, 60, 3, WHOLESALE, 25, ["rice", "biyyam", "బియ్యం", "chawal", "चावल"]),
    ("DAL", "Toor Dal 1kg", "kg", 160, 145, 9, 20, 3, WHOLESALE, 10, ["dal", "toor dal", "kandi pappu", "కందిపప్పు", "दाल"]),
    ("SUGAR", "Sugar 1kg", "kg", 48, 43, 12, 25, 3, WHOLESALE, 10, ["sugar", "panchadara", "చక్కెర", "chini", "चीनी"]),
    ("OIL", "Sunflower Oil 1L", "litre", 135, 122, 10, 20, 3, WHOLESALE, 12, ["oil", "sunflower oil", "nune", "నూనె", "tel", "तेल"]),
    ("ATTA", "Wheat Atta 5kg", "bag", 255, 232, 6, 12, 3, WHOLESALE, 5, ["atta", "wheat flour", "ఆటా", "gehu", "आटा"]),
    ("TEA", "Tea Powder 250g", "pack", 120, 104, 7, 14, 3, WHOLESALE, 10, ["tea", "chai", "టీ", "चाय"]),
    ("BISC", "Parle-G Biscuits", "pack", 10, 8.5, 40, 80, 2, WHOLESALE, 24, ["biscuit", "parle", "parle g", "బిస్కెట్"]),
    ("NOODLE", "Maggi Noodles 70g", "pack", 14, 12, 22, 40, 2, WHOLESALE, 24, ["maggi", "noodles", "నూడుల్స్"]),
    ("SOAP", "Bath Soap", "piece", 38, 31, 8, 16, 3, WHOLESALE, 12, ["soap", "సబ్బు", "sabun", "साबुन"]),
    ("DETERG", "Detergent Powder 1kg", "pack", 110, 92, 5, 10, 3, WHOLESALE, 6, ["detergent", "washing powder", "surf"]),
]

# Perishables are ordered for a couple of days at a time, staples weekly.
_REVIEW_DAYS = {"MILK": 2, "CURD": 2, "BREAD": 2, "EGGS": 4}

# Where the shop stands this morning (overrides the simulation's ending stock).
_STOCK_NOW = {"MILK": 0, "CURD": 0, "BREAD": 0, "EGGS": 0, "RICE": 70, "OIL": 22, "SUGAR": 80}


def seed_demo(repo: Repository, today: date, *, shop_id: str = DEMO_SHOP_ID, rng_seed: int = 42) -> Shop:
    rng = random.Random(rng_seed)
    repo.reset_shop_data(shop_id)

    shop = Shop(
        id=shop_id, name="Lakshmi General Stores", owner_name="Ramesh", language="te",
        timezone="Asia/Kolkata", join_code=DEMO_JOIN_CODE,
        settings={},
    )
    existing = repo.get_shop(shop_id)
    if existing:
        shop.telegram_chat_id = existing.telegram_chat_id
        shop.settings = existing.settings
    repo.upsert_shop(shop)

    repo.upsert_supplier(Supplier(DAIRY, shop_id, "Sri Lakshmi Dairy", "919000000001", "te"))
    repo.upsert_supplier(Supplier(WHOLESALE, shop_id, "Venkateswara Traders", "919000000002", "en"))

    products = [
        # A sane shop reorders when stock covers the delivery wait plus a little, so that is the floor here.
        Product(shop_id, sku, name, unit, price, cost, stock=max(rp, math.ceil(b * lead * 1.15)) * 3,
                reorder_point=max(rp, math.ceil(b * lead * 1.15)), lead_time_days=lead,
                supplier_id=sup, pack_size=pack, aliases=aliases, review_days=_REVIEW_DAYS.get(sku, 7))
        for sku, name, unit, price, cost, b, rp, lead, sup, pack, aliases in _CATALOG
    ]
    base = {c[0]: c[5] for c in _CATALOG}
    stock = {p.sku: float(p.stock) for p in products}
    arrivals: dict[tuple[str, date], float] = {}
    on_order: dict[str, bool] = {}
    sales: list[SaleRow] = []
    yesterday = today - timedelta(days=1)

    for i in range(HISTORY_DAYS, 0, -1):
        day = today - timedelta(days=i)
        snap: dict[str, float] = {}
        for p in products:
            if (p.sku, day) in arrivals:
                stock[p.sku] += arrivals.pop((p.sku, day))
                on_order[p.sku] = False
            if day == yesterday and p.supplier_id == DAIRY:
                stock[p.sku] = min(stock[p.sku], math.floor(0.2 * base[p.sku]))  # missed delivery
            mu = base[p.sku] * WEEKDAY_FACTOR[day.weekday()]
            demand = max(0, round(rng.gauss(mu, math.sqrt(mu) * 0.8)))
            sold = min(stock[p.sku], demand)
            stock[p.sku] -= sold
            if sold > 0:
                sales.append(SaleRow(shop_id, day, p.sku, float(sold), round(sold * p.price, 2)))
            snap[p.sku] = stock[p.sku]
            if stock[p.sku] <= p.reorder_point and not on_order.get(p.sku):
                missed = p.supplier_id == DAIRY and day >= yesterday - timedelta(days=1)
                if not missed:
                    qty = math.ceil(base[p.sku] * 7 / p.pack_size) * p.pack_size
                    arrivals[(p.sku, day + timedelta(days=p.lead_time_days))] = qty
                    on_order[p.sku] = True
        repo.save_snapshot(shop_id, day, snap)

    repo.add_sales(sales)
    for p in products:
        p.stock = float(_STOCK_NOW.get(p.sku, base[p.sku] * 8))
        repo.upsert_product(p)
    return shop
