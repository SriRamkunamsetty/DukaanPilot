"""Plain data classes shared by the services, tools and storage layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass
class Shop:
    id: str
    name: str
    owner_name: str
    language: str = "en"  # "en" | "hi" | "te"
    timezone: str = "Asia/Kolkata"
    currency: str = "INR"
    telegram_chat_id: str | None = None
    join_code: str | None = None
    settings: dict[str, Any] = field(default_factory=dict)


@dataclass
class Supplier:
    id: str
    shop_id: str
    name: str
    phone: str  # digits only, with country code, e.g. 919000000001
    language: str = "en"


@dataclass
class Product:
    shop_id: str
    sku: str
    name: str
    unit: str
    price: float
    cost: float
    stock: float
    reorder_point: float
    lead_time_days: int
    supplier_id: str
    pack_size: int = 1
    aliases: list[str] = field(default_factory=list)
    # How many days one order should cover beyond the delivery wait. Short for
    # perishables (milk, bread), longer for staples (rice, dal).
    review_days: int = 7


@dataclass
class SaleRow:
    shop_id: str
    sale_date: date
    sku: str
    qty: float
    revenue: float
    id: int | None = None


@dataclass
class ReorderItem:
    sku: str
    name: str
    qty: int
    unit: str


@dataclass
class ReorderDraft:
    id: str
    shop_id: str
    supplier_id: str
    supplier_name: str
    items: list[ReorderItem]
    status: str  # pending | approved | rejected | expired
    message: str
    rationale: str
    created_at: str
    decided_at: str | None = None
    decided_by: str | None = None
