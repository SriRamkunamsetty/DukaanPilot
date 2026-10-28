"""SQLite implementation of the repository.

Good for local development, tests and single-instance demos. Cloud Run's disk is
ephemeral, so production should use the Firestore repository instead.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from typing import Any

from .models import Product, ReorderDraft, ReorderItem, SaleRow, Shop, Supplier

SCHEMA = """
CREATE TABLE IF NOT EXISTS shops(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, owner_name TEXT NOT NULL,
  language TEXT NOT NULL DEFAULT 'en', timezone TEXT NOT NULL DEFAULT 'Asia/Kolkata',
  currency TEXT NOT NULL DEFAULT 'INR', telegram_chat_id TEXT UNIQUE,
  join_code TEXT UNIQUE, settings TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS suppliers(
  shop_id TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL, phone TEXT NOT NULL,
  language TEXT NOT NULL DEFAULT 'en', PRIMARY KEY(shop_id, id));
CREATE TABLE IF NOT EXISTS products(
  shop_id TEXT NOT NULL, sku TEXT NOT NULL, name TEXT NOT NULL, unit TEXT NOT NULL,
  price REAL NOT NULL, cost REAL NOT NULL, stock REAL NOT NULL, reorder_point REAL NOT NULL,
  lead_time_days INTEGER NOT NULL, supplier_id TEXT NOT NULL, pack_size INTEGER NOT NULL DEFAULT 1,
  aliases TEXT NOT NULL DEFAULT '[]', review_days INTEGER NOT NULL DEFAULT 7, PRIMARY KEY(shop_id, sku));
CREATE TABLE IF NOT EXISTS sales(
  id INTEGER PRIMARY KEY AUTOINCREMENT, shop_id TEXT NOT NULL, sale_date TEXT NOT NULL,
  sku TEXT NOT NULL, qty REAL NOT NULL, revenue REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_sales_shop_date ON sales(shop_id, sale_date);
CREATE TABLE IF NOT EXISTS stock_snapshots(
  shop_id TEXT NOT NULL, snap_date TEXT NOT NULL, sku TEXT NOT NULL, stock REAL NOT NULL,
  PRIMARY KEY(shop_id, snap_date, sku));
CREATE TABLE IF NOT EXISTS reorder_drafts(
  id TEXT NOT NULL, shop_id TEXT NOT NULL, supplier_id TEXT NOT NULL, supplier_name TEXT NOT NULL,
  items TEXT NOT NULL, status TEXT NOT NULL, message TEXT NOT NULL, rationale TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, decided_at TEXT, decided_by TEXT, PRIMARY KEY(shop_id, id));
CREATE TABLE IF NOT EXISTS notifications(
  shop_id TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL, sent_at TEXT NOT NULL,
  PRIMARY KEY(shop_id, kind, key));
CREATE TABLE IF NOT EXISTS processed_updates(
  source TEXT NOT NULL, update_id TEXT NOT NULL, PRIMARY KEY(source, update_id));
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, shop_id TEXT NOT NULL, ts TEXT NOT NULL,
  actor TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}');
"""


def _shop(r: sqlite3.Row) -> Shop:
    return Shop(
        id=r["id"], name=r["name"], owner_name=r["owner_name"], language=r["language"],
        timezone=r["timezone"], currency=r["currency"], telegram_chat_id=r["telegram_chat_id"],
        join_code=r["join_code"], settings=json.loads(r["settings"] or "{}"),
    )


def _product(r: sqlite3.Row) -> Product:
    return Product(
        shop_id=r["shop_id"], sku=r["sku"], name=r["name"], unit=r["unit"], price=r["price"],
        cost=r["cost"], stock=r["stock"], reorder_point=r["reorder_point"],
        lead_time_days=r["lead_time_days"], supplier_id=r["supplier_id"],
        pack_size=r["pack_size"], aliases=json.loads(r["aliases"] or "[]"), review_days=r["review_days"],
    )


def _sale(r: sqlite3.Row) -> SaleRow:
    return SaleRow(
        id=r["id"], shop_id=r["shop_id"], sale_date=date.fromisoformat(r["sale_date"]),
        sku=r["sku"], qty=r["qty"], revenue=r["revenue"],
    )


def _draft(r: sqlite3.Row) -> ReorderDraft:
    return ReorderDraft(
        id=r["id"], shop_id=r["shop_id"], supplier_id=r["supplier_id"],
        supplier_name=r["supplier_name"],
        items=[ReorderItem(**i) for i in json.loads(r["items"])],
        status=r["status"], message=r["message"], rationale=r["rationale"],
        created_at=r["created_at"], decided_at=r["decided_at"], decided_by=r["decided_by"],
    )


class SqliteRepository:
    def __init__(self, path: str = ":memory:") -> None:
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)

    # -- helpers ---------------------------------------------------------
    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            else:
                self._db.execute("COMMIT")

    def _q(self, sql: str, args: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    # -- shops -----------------------------------------------------------
    def upsert_shop(self, s: Shop) -> None:
        with self._tx() as db:
            db.execute(
                "INSERT INTO shops(id,name,owner_name,language,timezone,currency,telegram_chat_id,join_code,settings)"
                " VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,"
                " owner_name=excluded.owner_name, language=excluded.language, timezone=excluded.timezone,"
                " currency=excluded.currency, telegram_chat_id=excluded.telegram_chat_id,"
                " join_code=excluded.join_code, settings=excluded.settings",
                (s.id, s.name, s.owner_name, s.language, s.timezone, s.currency,
                 s.telegram_chat_id, s.join_code, json.dumps(s.settings)),
            )

    def get_shop(self, shop_id: str) -> Shop | None:
        rows = self._q("SELECT * FROM shops WHERE id=?", (shop_id,))
        return _shop(rows[0]) if rows else None

    def get_shop_by_chat(self, chat_id: str) -> Shop | None:
        rows = self._q("SELECT * FROM shops WHERE telegram_chat_id=?", (str(chat_id),))
        return _shop(rows[0]) if rows else None

    def get_shop_by_join_code(self, code: str) -> Shop | None:
        rows = self._q("SELECT * FROM shops WHERE join_code=?", (code.strip().upper(),))
        return _shop(rows[0]) if rows else None

    def list_shops(self) -> list[Shop]:
        return [_shop(r) for r in self._q("SELECT * FROM shops ORDER BY id")]

    def link_chat(self, shop_id: str, chat_id: str) -> None:
        with self._tx() as db:
            db.execute("UPDATE shops SET telegram_chat_id=NULL WHERE telegram_chat_id=?", (str(chat_id),))
            db.execute("UPDATE shops SET telegram_chat_id=? WHERE id=?", (str(chat_id), shop_id))

    def set_setting(self, shop_id: str, key: str, value: Any) -> None:
        with self._tx() as db:
            row = db.execute("SELECT settings FROM shops WHERE id=?", (shop_id,)).fetchone()
            if row is None:
                raise KeyError(shop_id)
            settings = json.loads(row["settings"] or "{}")
            settings[key] = value
            db.execute("UPDATE shops SET settings=? WHERE id=?", (json.dumps(settings), shop_id))

    # -- suppliers & products ---------------------------------------------
    def upsert_supplier(self, s: Supplier) -> None:
        with self._tx() as db:
            db.execute(
                "INSERT INTO suppliers(shop_id,id,name,phone,language) VALUES(?,?,?,?,?)"
                " ON CONFLICT(shop_id,id) DO UPDATE SET name=excluded.name, phone=excluded.phone,"
                " language=excluded.language",
                (s.shop_id, s.id, s.name, s.phone, s.language),
            )

    def get_supplier(self, shop_id: str, supplier_id: str) -> Supplier | None:
        rows = self._q("SELECT * FROM suppliers WHERE shop_id=? AND id=?", (shop_id, supplier_id))
        if not rows:
            return None
        r = rows[0]
        return Supplier(id=r["id"], shop_id=r["shop_id"], name=r["name"], phone=r["phone"], language=r["language"])

    def list_suppliers(self, shop_id: str) -> list[Supplier]:
        rows = self._q("SELECT * FROM suppliers WHERE shop_id=? ORDER BY id", (shop_id,))
        return [Supplier(id=r["id"], shop_id=r["shop_id"], name=r["name"], phone=r["phone"], language=r["language"]) for r in rows]

    def upsert_product(self, p: Product) -> None:
        with self._tx() as db:
            db.execute(
                "INSERT INTO products(shop_id,sku,name,unit,price,cost,stock,reorder_point,lead_time_days,supplier_id,pack_size,aliases,review_days)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(shop_id,sku) DO UPDATE SET name=excluded.name,"
                " unit=excluded.unit, price=excluded.price, cost=excluded.cost, stock=excluded.stock,"
                " reorder_point=excluded.reorder_point, lead_time_days=excluded.lead_time_days,"
                " supplier_id=excluded.supplier_id, pack_size=excluded.pack_size, aliases=excluded.aliases,"
                " review_days=excluded.review_days",
                (p.shop_id, p.sku, p.name, p.unit, p.price, p.cost, p.stock, p.reorder_point,
                 p.lead_time_days, p.supplier_id, p.pack_size, json.dumps(p.aliases), p.review_days),
            )

    def list_products(self, shop_id: str) -> list[Product]:
        return [_product(r) for r in self._q("SELECT * FROM products WHERE shop_id=? ORDER BY sku", (shop_id,))]

    def get_product(self, shop_id: str, sku: str) -> Product | None:
        rows = self._q("SELECT * FROM products WHERE shop_id=? AND sku=?", (shop_id, sku))
        return _product(rows[0]) if rows else None

    def adjust_stock(self, shop_id: str, sku: str, delta: float) -> float:
        with self._tx() as db:
            row = db.execute("SELECT stock FROM products WHERE shop_id=? AND sku=?", (shop_id, sku)).fetchone()
            if row is None:
                raise KeyError(sku)
            new = max(0.0, row["stock"] + delta)
            db.execute("UPDATE products SET stock=? WHERE shop_id=? AND sku=?", (new, shop_id, sku))
            return new

    def set_stock(self, shop_id: str, sku: str, stock: float) -> None:
        with self._tx() as db:
            db.execute("UPDATE products SET stock=? WHERE shop_id=? AND sku=?", (max(0.0, stock), shop_id, sku))

    # -- sales ---------------------------------------------------------------
    def record_sale(self, shop_id: str, sale_date: date, sku: str, qty: float, revenue: float) -> int:
        with self._tx() as db:
            cur = db.execute(
                "INSERT INTO sales(shop_id,sale_date,sku,qty,revenue) VALUES(?,?,?,?,?)",
                (shop_id, sale_date.isoformat(), sku, qty, revenue),
            )
            return int(cur.lastrowid)

    def add_sales(self, rows: list[SaleRow]) -> None:
        with self._tx() as db:
            db.executemany(
                "INSERT INTO sales(shop_id,sale_date,sku,qty,revenue) VALUES(?,?,?,?,?)",
                [(r.shop_id, r.sale_date.isoformat(), r.sku, r.qty, r.revenue) for r in rows],
            )

    def delete_sale(self, shop_id: str, sale_id: int) -> SaleRow | None:
        with self._tx() as db:
            row = db.execute("SELECT * FROM sales WHERE shop_id=? AND id=?", (shop_id, sale_id)).fetchone()
            if row is None:
                return None
            db.execute("DELETE FROM sales WHERE shop_id=? AND id=?", (shop_id, sale_id))
            return _sale(row)

    def sales_between(self, shop_id: str, start: date, end: date) -> list[SaleRow]:
        rows = self._q(
            "SELECT * FROM sales WHERE shop_id=? AND sale_date>=? AND sale_date<=? ORDER BY sale_date, id",
            (shop_id, start.isoformat(), end.isoformat()),
        )
        return [_sale(r) for r in rows]

    # -- stock snapshots -----------------------------------------------------
    def save_snapshot(self, shop_id: str, day: date, stocks: dict[str, float]) -> None:
        with self._tx() as db:
            db.executemany(
                "INSERT INTO stock_snapshots(shop_id,snap_date,sku,stock) VALUES(?,?,?,?)"
                " ON CONFLICT(shop_id,snap_date,sku) DO UPDATE SET stock=excluded.stock",
                [(shop_id, day.isoformat(), sku, s) for sku, s in stocks.items()],
            )

    def snapshots_between(self, shop_id: str, start: date, end: date) -> dict[date, dict[str, float]]:
        rows = self._q(
            "SELECT snap_date, sku, stock FROM stock_snapshots WHERE shop_id=? AND snap_date>=? AND snap_date<=?",
            (shop_id, start.isoformat(), end.isoformat()),
        )
        out: dict[date, dict[str, float]] = {}
        for r in rows:
            out.setdefault(date.fromisoformat(r["snap_date"]), {})[r["sku"]] = r["stock"]
        return out

    # -- reorder drafts ------------------------------------------------------
    def save_draft(self, d: ReorderDraft) -> None:
        with self._tx() as db:
            db.execute(
                "INSERT INTO reorder_drafts(id,shop_id,supplier_id,supplier_name,items,status,message,rationale,created_at,decided_at,decided_by)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (d.id, d.shop_id, d.supplier_id, d.supplier_name,
                 json.dumps([i.__dict__ for i in d.items]), d.status, d.message, d.rationale,
                 d.created_at, d.decided_at, d.decided_by),
            )

    def get_draft(self, shop_id: str, draft_id: str) -> ReorderDraft | None:
        rows = self._q("SELECT * FROM reorder_drafts WHERE shop_id=? AND id=?", (shop_id, draft_id.upper()))
        return _draft(rows[0]) if rows else None

    def list_drafts(self, shop_id: str, status: str | None = None) -> list[ReorderDraft]:
        if status:
            rows = self._q(
                "SELECT * FROM reorder_drafts WHERE shop_id=? AND status=? ORDER BY created_at DESC", (shop_id, status)
            )
        else:
            rows = self._q("SELECT * FROM reorder_drafts WHERE shop_id=? ORDER BY created_at DESC", (shop_id,))
        return [_draft(r) for r in rows]

    def transition_draft(
        self, shop_id: str, draft_id: str, expected: str, new: str, decided_by: str, now_iso: str
    ) -> bool:
        """Compare-and-set the status. Returns False if the draft was not in ``expected``."""
        with self._tx() as db:
            cur = db.execute(
                "UPDATE reorder_drafts SET status=?, decided_at=?, decided_by=? WHERE shop_id=? AND id=? AND status=?",
                (new, now_iso, decided_by, shop_id, draft_id.upper(), expected),
            )
            return cur.rowcount == 1

    # -- notifications, idempotency, audit --------------------------------------
    def was_notified(self, shop_id: str, kind: str, key: str) -> bool:
        return bool(self._q("SELECT 1 FROM notifications WHERE shop_id=? AND kind=? AND key=?", (shop_id, kind, key)))

    def mark_notified(self, shop_id: str, kind: str, key: str, now_iso: str) -> bool:
        """Returns True if this call recorded it, False if it was already recorded."""
        with self._tx() as db:
            cur = db.execute(
                "INSERT OR IGNORE INTO notifications(shop_id,kind,key,sent_at) VALUES(?,?,?,?)",
                (shop_id, kind, key, now_iso),
            )
            return cur.rowcount == 1

    def unmark_notified(self, shop_id: str, kind: str, key: str) -> None:
        with self._tx() as db:
            db.execute("DELETE FROM notifications WHERE shop_id=? AND kind=? AND key=?", (shop_id, kind, key))

    def claim_update(self, source: str, update_id: str) -> bool:
        with self._tx() as db:
            cur = db.execute(
                "INSERT OR IGNORE INTO processed_updates(source,update_id) VALUES(?,?)", (source, str(update_id))
            )
            return cur.rowcount == 1

    def add_audit(self, shop_id: str, actor: str, action: str, detail: dict[str, Any], ts_iso: str) -> None:
        with self._tx() as db:
            db.execute(
                "INSERT INTO audit_log(shop_id,ts,actor,action,detail) VALUES(?,?,?,?,?)",
                (shop_id, ts_iso, actor, action, json.dumps(detail, default=str)),
            )

    def list_audit(self, shop_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._q("SELECT * FROM audit_log WHERE shop_id=? ORDER BY id DESC LIMIT ?", (shop_id, limit))
        return [
            {"ts": r["ts"], "actor": r["actor"], "action": r["action"], "detail": json.loads(r["detail"])}
            for r in rows
        ]

    def reset_shop_data(self, shop_id: str) -> None:
        with self._tx() as db:
            for table in ("sales", "stock_snapshots", "reorder_drafts", "notifications", "audit_log", "products", "suppliers"):
                db.execute(f"DELETE FROM {table} WHERE shop_id=?", (shop_id,))  # noqa: S608 - fixed table names
