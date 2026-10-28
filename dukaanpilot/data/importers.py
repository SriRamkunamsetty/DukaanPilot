"""Bring a real shop's data in from spreadsheets (CSV exports from Tally, Vyapar, Excel, a POS...).

Strict on purpose: a bad row is reported with its line number and nothing is written,
so a typo never leaves the shop half-imported.
"""

from __future__ import annotations

import csv
import io
from datetime import date

from .models import Product, SaleRow, Supplier
from .repository import Repository

PRODUCT_COLUMNS = ["sku", "name", "unit", "price", "cost", "stock", "reorder_point", "lead_time_days",
                   "supplier_id", "pack_size", "review_days", "aliases"]
SALES_COLUMNS = ["date", "sku", "qty", "revenue"]


class ImportErrorReport(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems[:10]) + (f" (+{len(problems) - 10} more)" if len(problems) > 10 else ""))
        self.problems = problems


def _rows(text: str, required: list[str]) -> list[tuple[int, dict[str, str]]]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    missing = [c for c in required if c not in (reader.fieldnames or [])]
    if missing:
        raise ImportErrorReport([f"missing column(s): {', '.join(missing)}"])
    return [(i, {k: (v or "").strip() for k, v in row.items() if k}) for i, row in enumerate(reader, start=2)]


def parse_products(shop_id: str, text: str, known_suppliers: set[str]) -> list[Product]:
    out, problems = [], []
    seen: set[str] = set()
    for line, r in _rows(text, ["sku", "name", "unit", "price", "stock", "supplier_id"]):
        try:
            sku = r["sku"].upper()
            if not sku or sku in seen:
                raise ValueError("empty or duplicate sku")
            seen.add(sku)
            if r["supplier_id"] not in known_suppliers:
                raise ValueError(f"unknown supplier_id {r['supplier_id']!r}")
            price = float(r["price"])
            lead = int(r.get("lead_time_days") or 2)
            p = Product(
                shop_id=shop_id, sku=sku, name=r["name"], unit=r["unit"], price=price,
                cost=float(r.get("cost") or price * 0.85), stock=float(r["stock"]),
                reorder_point=float(r.get("reorder_point") or 0), lead_time_days=lead,
                supplier_id=r["supplier_id"], pack_size=int(r.get("pack_size") or 1),
                aliases=[a.strip() for a in (r.get("aliases") or "").split("|") if a.strip()],
                review_days=int(r.get("review_days") or 7),
            )
            if min(p.price, p.cost, p.stock, p.reorder_point) < 0 or p.pack_size < 1 or p.lead_time_days < 0:
                raise ValueError("negative number or pack_size < 1")
            out.append(p)
        except (ValueError, KeyError) as e:
            problems.append(f"line {line}: {e}")
    if problems:
        raise ImportErrorReport(problems)
    return out


def parse_sales(shop_id: str, text: str, known_skus: set[str]) -> list[SaleRow]:
    out, problems = [], []
    for line, r in _rows(text, SALES_COLUMNS):
        try:
            sku = r["sku"].upper()
            if sku not in known_skus:
                raise ValueError(f"unknown sku {sku!r}")
            qty, revenue = float(r["qty"]), float(r["revenue"])
            if qty <= 0 or revenue < 0:
                raise ValueError("qty must be > 0 and revenue >= 0")
            out.append(SaleRow(shop_id, date.fromisoformat(r["date"]), sku, qty, revenue))
        except (ValueError, KeyError) as e:
            problems.append(f"line {line}: {e}")
    if problems:
        raise ImportErrorReport(problems)
    return out


def import_products(repo: Repository, shop_id: str, text: str) -> int:
    suppliers = {s.id for s in repo.list_suppliers(shop_id)}
    products = parse_products(shop_id, text, suppliers)
    for p in products:
        repo.upsert_product(p)
    return len(products)


def import_sales(repo: Repository, shop_id: str, text: str) -> int:
    skus = {p.sku for p in repo.list_products(shop_id)}
    rows = parse_sales(shop_id, text, skus)
    repo.add_sales(rows)
    return len(rows)


def add_supplier(repo: Repository, shop_id: str, spec: str) -> Supplier:
    """``id:Name:919876543210[:te]``"""
    parts = spec.split(":")
    if len(parts) not in (3, 4) or not parts[2].isdigit():
        raise ValueError(f"supplier must look like id:Name:919876543210[:en|hi|te], got {spec!r}")
    s = Supplier(parts[0], shop_id, parts[1], parts[2], parts[3] if len(parts) == 4 else "en")
    repo.upsert_supplier(s)
    return s
