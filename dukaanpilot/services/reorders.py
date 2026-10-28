"""Reorder drafts and the human approval step.

The agent can *propose* an order. Only a person can approve it, and approval
never sends anything automatically: it hands the owner a ready-to-send WhatsApp
link, so the message goes from the owner's own number to the supplier.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import quote

from ..data.models import ReorderDraft, ReorderItem, Shop, Supplier
from ..data.repository import Repository
from .inventory import StockStatus

_TEMPLATES = {
    "en": ("Hello {supplier}, please send the following:", "Thank you."),
    "hi": ("नमस्ते {supplier}, कृपया ये सामान भेज दीजिए:", "धन्यवाद।"),
    "te": ("నమస్కారం {supplier}, దయచేసి ఈ సరుకులు పంపండి:", "ధన్యవాదాలు."),
}


class DraftNotFound(LookupError):
    pass


class InvalidTransition(RuntimeError):
    pass


@dataclass
class Decision:
    draft: ReorderDraft
    whatsapp_link: str | None


def render_message(shop: Shop, supplier: Supplier, items: list[ReorderItem]) -> str:
    head, tail = _TEMPLATES.get(supplier.language, _TEMPLATES["en"])
    lines = [head.format(supplier=supplier.name)]
    lines += [f"- {i.name}: {i.qty} {i.unit}" for i in items]
    lines += [tail, f"- {shop.name}"]
    return "\n".join(lines)


def whatsapp_link(phone: str, text: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    return f"https://wa.me/{digits}?text={quote(text)}"


def _rationale(s: StockStatus) -> str:
    p = s.product
    if s.risk == "stockout":
        return f"{p.name}: out of stock now, delivery takes {p.lead_time_days} day(s)"
    cover = "more than a month" if s.days_cover >= 30 else f"about {s.days_cover:.0f} day(s)"
    extra = f"; {s.festival} is coming" if s.festival else ""
    return f"{p.name}: {p.stock:g} {p.unit} left, lasts {cover}, delivery takes {p.lead_time_days} day(s){extra}"


def create_drafts(repo: Repository, shop: Shop, statuses: list[StockStatus], now_utc: datetime) -> list[ReorderDraft]:
    """One pending draft per supplier for items at risk. Never duplicates an item already pending."""
    already = {i.sku for d in repo.list_drafts(shop.id, "pending") for i in d.items}
    by_supplier: dict[str, list[StockStatus]] = {}
    for s in statuses:
        if s.risk == "ok" or s.suggested_qty <= 0 or s.product.sku in already:
            continue
        by_supplier.setdefault(s.product.supplier_id, []).append(s)

    created = []
    for supplier_id, group in by_supplier.items():
        supplier = repo.get_supplier(shop.id, supplier_id)
        if supplier is None:
            continue
        items = [ReorderItem(sku=s.product.sku, name=s.product.name, qty=s.suggested_qty, unit=s.product.unit) for s in group]
        draft = ReorderDraft(
            id="RD" + uuid.uuid4().hex[:4].upper(),
            shop_id=shop.id, supplier_id=supplier.id, supplier_name=supplier.name, items=items,
            status="pending", message=render_message(shop, supplier, items),
            rationale="; ".join(_rationale(s) for s in group),
            created_at=now_utc.isoformat(),
        )
        repo.save_draft(draft)
        repo.add_audit(shop.id, "agent", "reorder_draft_created",
                       {"draft": draft.id, "supplier": supplier.name, "items": [i.__dict__ for i in items]},
                       now_utc.isoformat())
        created.append(draft)
    return created


def _decide(repo: Repository, shop: Shop, draft_id: str, actor: str, new: str, now_utc: datetime) -> ReorderDraft:
    draft = repo.get_draft(shop.id, draft_id)
    if draft is None:
        raise DraftNotFound(f"No reorder draft {draft_id!r}.")
    if not repo.transition_draft(shop.id, draft.id, "pending", new, actor, now_utc.isoformat()):
        current = repo.get_draft(shop.id, draft.id)
        raise InvalidTransition(f"Draft {draft.id} is already {current.status if current else 'unavailable'}.")
    repo.add_audit(shop.id, actor, f"reorder_draft_{new}", {"draft": draft.id}, now_utc.isoformat())
    decided = repo.get_draft(shop.id, draft.id)
    assert decided is not None
    return decided


def approve(repo: Repository, shop: Shop, draft_id: str, actor: str, now_utc: datetime) -> Decision:
    draft = _decide(repo, shop, draft_id, actor, "approved", now_utc)
    supplier = repo.get_supplier(shop.id, draft.supplier_id)
    return Decision(draft, whatsapp_link(supplier.phone, draft.message) if supplier else None)


def reject(repo: Repository, shop: Shop, draft_id: str, actor: str, now_utc: datetime) -> Decision:
    return Decision(_decide(repo, shop, draft_id, actor, "rejected", now_utc), None)


def expire_stale(repo: Repository, shop: Shop, now_utc: datetime, hours: int) -> int:
    cutoff = now_utc - timedelta(hours=hours)
    n = 0
    for d in repo.list_drafts(shop.id, "pending"):
        if datetime.fromisoformat(d.created_at) < cutoff and repo.transition_draft(
            shop.id, d.id, "pending", "expired", "system", now_utc.isoformat()
        ):
            n += 1
    return n
