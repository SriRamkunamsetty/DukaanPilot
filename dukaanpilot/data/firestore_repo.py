"""Firestore implementation of the Repository contract (production persistence on Cloud Run).

Cloud Run's disk is ephemeral, so anything the shop owner would be upset to lose lives here.

Layout (everything for a shop sits under its own document, so tenants never mix):

    shops/{shop_id}                      the shop and its settings
        suppliers/{supplier_id}
        products/{sku}
        sales/{sale_id}                  one document per sale line
        snapshots/{YYYY-MM-DD}           {"stocks": {sku: qty}}
        drafts/{DRAFT_ID}                reorder drafts
        notifications/{kind}__{key}      "already told the owner" markers
        audit/{auto-id}
    processed_updates/{source}__{id}     Telegram update de-duplication

Guarantees that matter are enforced by Firestore itself, not by Python locks, because Cloud
Run may run several instances at once: ``create()`` fails if a document exists (idempotency
markers) and read-modify-write operations run inside transactions (stock, draft approval).

Sale ids are integers (the agent tools expose them for "undo that sale"). They are
microsecond timestamps, which are unique per shop in practice and need no shared counter.
"""

from __future__ import annotations

import time
from datetime import date
from typing import Any

from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore

from .models import Product, ReorderDraft, ReorderItem, SaleRow, Shop, Supplier

_BATCH = 400  # Firestore allows 500 writes per batch; stay under it


def _safe(part: str) -> str:
    """Document ids cannot contain '/'."""
    return str(part).replace("/", "∕")


class FirestoreRepository:
    def __init__(self, client: firestore.Client | None = None) -> None:
        self._db = client or firestore.Client()
        self._last_sale_id = 0

    # -- helpers ---------------------------------------------------------------
    def _shop_ref(self, shop_id: str):
        return self._db.collection("shops").document(shop_id)

    def _col(self, shop_id: str, name: str):
        return self._shop_ref(shop_id).collection(name)

    def _delete_collection(self, col) -> None:
        batch, n = self._db.batch(), 0
        for doc in col.stream():
            batch.delete(doc.reference)
            n += 1
            if n % _BATCH == 0:
                batch.commit()
                batch = self._db.batch()
        if n % _BATCH:
            batch.commit()

    def _next_sale_id(self) -> int:
        candidate = time.time_ns() // 1000
        self._last_sale_id = max(candidate, self._last_sale_id + 1)
        return self._last_sale_id

    # -- shops -----------------------------------------------------------------
    @staticmethod
    def _shop(doc) -> Shop:
        d = doc.to_dict()
        return Shop(
            id=doc.id, name=d["name"], owner_name=d["owner_name"], language=d.get("language", "en"),
            timezone=d.get("timezone", "Asia/Kolkata"), currency=d.get("currency", "INR"),
            telegram_chat_id=d.get("telegram_chat_id"), join_code=d.get("join_code"),
            settings=d.get("settings") or {},
        )

    def upsert_shop(self, s: Shop) -> None:
        self._shop_ref(s.id).set(
            {
                "name": s.name, "owner_name": s.owner_name, "language": s.language, "timezone": s.timezone,
                "currency": s.currency, "telegram_chat_id": s.telegram_chat_id, "join_code": s.join_code,
                "settings": s.settings,
            }
        )

    def get_shop(self, shop_id: str) -> Shop | None:
        doc = self._shop_ref(shop_id).get()
        return self._shop(doc) if doc.exists else None

    def _first(self, field: str, value: str) -> Shop | None:
        for doc in self._db.collection("shops").where(filter=firestore.FieldFilter(field, "==", value)).limit(1).stream():
            return self._shop(doc)
        return None

    def get_shop_by_chat(self, chat_id: str) -> Shop | None:
        return self._first("telegram_chat_id", str(chat_id))

    def get_shop_by_join_code(self, code: str) -> Shop | None:
        return self._first("join_code", code.strip().upper())

    def list_shops(self) -> list[Shop]:
        return sorted((self._shop(d) for d in self._db.collection("shops").stream()), key=lambda s: s.id)

    def link_chat(self, shop_id: str, chat_id: str) -> None:
        batch = self._db.batch()
        flt = firestore.FieldFilter("telegram_chat_id", "==", str(chat_id))
        for doc in self._db.collection("shops").where(filter=flt).stream():
            batch.update(doc.reference, {"telegram_chat_id": None})
        batch.update(self._shop_ref(shop_id), {"telegram_chat_id": str(chat_id)})
        batch.commit()

    def set_setting(self, shop_id: str, key: str, value: Any) -> None:
        ref = self._shop_ref(shop_id)

        @firestore.transactional
        def run(tx):
            snap = ref.get(transaction=tx)
            if not snap.exists:
                raise KeyError(shop_id)
            settings = dict(snap.to_dict().get("settings") or {})
            settings[key] = value
            tx.update(ref, {"settings": settings})

        run(self._db.transaction())

    # -- suppliers & products ----------------------------------------------------
    @staticmethod
    def _supplier(doc, shop_id: str) -> Supplier:
        d = doc.to_dict()
        return Supplier(id=doc.id, shop_id=shop_id, name=d["name"], phone=d["phone"], language=d.get("language", "en"))

    def upsert_supplier(self, s: Supplier) -> None:
        self._col(s.shop_id, "suppliers").document(_safe(s.id)).set(
            {"name": s.name, "phone": s.phone, "language": s.language}
        )

    def get_supplier(self, shop_id: str, supplier_id: str) -> Supplier | None:
        doc = self._col(shop_id, "suppliers").document(_safe(supplier_id)).get()
        return self._supplier(doc, shop_id) if doc.exists else None

    def list_suppliers(self, shop_id: str) -> list[Supplier]:
        return sorted((self._supplier(d, shop_id) for d in self._col(shop_id, "suppliers").stream()), key=lambda s: s.id)

    @staticmethod
    def _product(doc, shop_id: str) -> Product:
        d = doc.to_dict()
        return Product(
            shop_id=shop_id, sku=doc.id, name=d["name"], unit=d["unit"], price=d["price"], cost=d["cost"],
            stock=d["stock"], reorder_point=d["reorder_point"], lead_time_days=d["lead_time_days"],
            supplier_id=d["supplier_id"], pack_size=d.get("pack_size", 1), aliases=list(d.get("aliases") or []),
            review_days=d.get("review_days", 7),
        )

    def upsert_product(self, p: Product) -> None:
        self._col(p.shop_id, "products").document(_safe(p.sku)).set(
            {
                "name": p.name, "unit": p.unit, "price": p.price, "cost": p.cost, "stock": p.stock,
                "reorder_point": p.reorder_point, "lead_time_days": p.lead_time_days,
                "supplier_id": p.supplier_id, "pack_size": p.pack_size, "aliases": p.aliases,
                "review_days": p.review_days,
            }
        )

    def list_products(self, shop_id: str) -> list[Product]:
        return sorted((self._product(d, shop_id) for d in self._col(shop_id, "products").stream()), key=lambda p: p.sku)

    def get_product(self, shop_id: str, sku: str) -> Product | None:
        doc = self._col(shop_id, "products").document(_safe(sku)).get()
        return self._product(doc, shop_id) if doc.exists else None

    def adjust_stock(self, shop_id: str, sku: str, delta: float) -> float:
        ref = self._col(shop_id, "products").document(_safe(sku))

        @firestore.transactional
        def run(tx) -> float:
            snap = ref.get(transaction=tx)
            if not snap.exists:
                raise KeyError(sku)
            new = max(0.0, snap.to_dict()["stock"] + delta)
            tx.update(ref, {"stock": new})
            return new

        return run(self._db.transaction())

    def set_stock(self, shop_id: str, sku: str, stock: float) -> None:
        self._col(shop_id, "products").document(_safe(sku)).update({"stock": max(0.0, stock)})

    # -- sales ---------------------------------------------------------------------
    @staticmethod
    def _sale(doc, shop_id: str) -> SaleRow:
        d = doc.to_dict()
        return SaleRow(shop_id=shop_id, sale_date=date.fromisoformat(d["sale_date"]), sku=d["sku"],
                       qty=d["qty"], revenue=d["revenue"], id=int(doc.id))

    def record_sale(self, shop_id: str, sale_date: date, sku: str, qty: float, revenue: float) -> int:
        sale_id = self._next_sale_id()
        self._col(shop_id, "sales").document(str(sale_id)).create(
            {"sale_date": sale_date.isoformat(), "sku": sku, "qty": qty, "revenue": revenue}
        )
        return sale_id

    def add_sales(self, rows: list[SaleRow]) -> None:
        for start in range(0, len(rows), _BATCH):
            batch = self._db.batch()
            for r in rows[start : start + _BATCH]:
                ref = self._col(r.shop_id, "sales").document(str(self._next_sale_id()))
                batch.create(ref, {"sale_date": r.sale_date.isoformat(), "sku": r.sku, "qty": r.qty, "revenue": r.revenue})
            batch.commit()

    def delete_sale(self, shop_id: str, sale_id: int) -> SaleRow | None:
        ref = self._col(shop_id, "sales").document(str(sale_id))

        @firestore.transactional
        def run(tx) -> SaleRow | None:
            snap = ref.get(transaction=tx)
            if not snap.exists:
                return None
            row = self._sale(snap, shop_id)
            tx.delete(ref)
            return row

        return run(self._db.transaction())

    def sales_between(self, shop_id: str, start: date, end: date) -> list[SaleRow]:
        query = (
            self._col(shop_id, "sales")
            .where(filter=firestore.FieldFilter("sale_date", ">=", start.isoformat()))
            .where(filter=firestore.FieldFilter("sale_date", "<=", end.isoformat()))
        )
        rows = [self._sale(d, shop_id) for d in query.stream()]
        rows.sort(key=lambda r: (r.sale_date, r.id or 0))
        return rows

    # -- stock snapshots -----------------------------------------------------------------
    def save_snapshot(self, shop_id: str, day: date, stocks: dict[str, float]) -> None:
        # merge so that a partial snapshot never erases SKUs recorded earlier the same day
        self._col(shop_id, "snapshots").document(day.isoformat()).set({"stocks": stocks}, merge=True)

    def snapshots_between(self, shop_id: str, start: date, end: date) -> dict[date, dict[str, float]]:
        out: dict[date, dict[str, float]] = {}
        for doc in self._col(shop_id, "snapshots").stream():
            day = date.fromisoformat(doc.id)
            if start <= day <= end:
                out[day] = {k: float(v) for k, v in (doc.to_dict().get("stocks") or {}).items()}
        return out

    # -- reorder drafts ----------------------------------------------------------------------
    @staticmethod
    def _draft(doc, shop_id: str) -> ReorderDraft:
        d = doc.to_dict()
        return ReorderDraft(
            id=doc.id, shop_id=shop_id, supplier_id=d["supplier_id"], supplier_name=d["supplier_name"],
            items=[ReorderItem(**i) for i in d["items"]], status=d["status"], message=d["message"],
            rationale=d["rationale"], created_at=d["created_at"], decided_at=d.get("decided_at"),
            decided_by=d.get("decided_by"),
        )

    def save_draft(self, d: ReorderDraft) -> None:
        self._col(d.shop_id, "drafts").document(d.id).create(
            {
                "supplier_id": d.supplier_id, "supplier_name": d.supplier_name,
                "items": [i.__dict__ for i in d.items], "status": d.status, "message": d.message,
                "rationale": d.rationale, "created_at": d.created_at, "decided_at": d.decided_at,
                "decided_by": d.decided_by,
            }
        )

    def get_draft(self, shop_id: str, draft_id: str) -> ReorderDraft | None:
        doc = self._col(shop_id, "drafts").document(_safe(draft_id.upper())).get()
        return self._draft(doc, shop_id) if doc.exists else None

    def list_drafts(self, shop_id: str, status: str | None = None) -> list[ReorderDraft]:
        col = self._col(shop_id, "drafts")
        query = col.where(filter=firestore.FieldFilter("status", "==", status)) if status else col
        drafts = [self._draft(d, shop_id) for d in query.stream()]
        drafts.sort(key=lambda d: d.created_at, reverse=True)  # sorted here: avoids a composite index
        return drafts

    def transition_draft(
        self, shop_id: str, draft_id: str, expected: str, new: str, decided_by: str, now_iso: str
    ) -> bool:
        """Compare-and-set the status in a transaction, so two taps (or two instances) cannot both win."""
        ref = self._col(shop_id, "drafts").document(_safe(draft_id.upper()))

        @firestore.transactional
        def run(tx) -> bool:
            snap = ref.get(transaction=tx)
            if not snap.exists or snap.to_dict()["status"] != expected:
                return False
            tx.update(ref, {"status": new, "decided_at": now_iso, "decided_by": decided_by})
            return True

        return run(self._db.transaction())

    # -- notifications, idempotency, audit ---------------------------------------------------------
    def _note_ref(self, shop_id: str, kind: str, key: str):
        return self._col(shop_id, "notifications").document(_safe(f"{kind}__{key}"))

    def was_notified(self, shop_id: str, kind: str, key: str) -> bool:
        return bool(self._note_ref(shop_id, kind, key).get().exists)

    def mark_notified(self, shop_id: str, kind: str, key: str, now_iso: str) -> bool:
        try:
            self._note_ref(shop_id, kind, key).create({"kind": kind, "key": key, "sent_at": now_iso})
            return True
        except AlreadyExists:
            return False

    def unmark_notified(self, shop_id: str, kind: str, key: str) -> None:
        self._note_ref(shop_id, kind, key).delete()

    def claim_update(self, source: str, update_id: str) -> bool:
        try:
            self._db.collection("processed_updates").document(_safe(f"{source}__{update_id}")).create(
                {"at": firestore.SERVER_TIMESTAMP}
            )
            return True
        except AlreadyExists:
            return False

    def add_audit(self, shop_id: str, actor: str, action: str, detail: dict[str, Any], ts_iso: str) -> None:
        # round-trip through str() for odd values (dates, Decimals) so the write cannot fail on types
        safe_detail = {str(k): v if isinstance(v, (str, int, float, bool, type(None))) else str(v) for k, v in detail.items()}
        self._col(shop_id, "audit").add({"ts": ts_iso, "actor": actor, "action": action, "detail": safe_detail})

    def list_audit(self, shop_id: str, limit: int = 50) -> list[dict[str, Any]]:
        q = self._col(shop_id, "audit").order_by("ts", direction=firestore.Query.DESCENDING).limit(limit)
        return [
            {"ts": d["ts"], "actor": d["actor"], "action": d["action"], "detail": d["detail"]}
            for d in (doc.to_dict() for doc in q.stream())
        ]

    def reset_shop_data(self, shop_id: str) -> None:
        for name in ("sales", "snapshots", "drafts", "notifications", "audit", "products", "suppliers"):
            self._delete_collection(self._col(shop_id, name))
