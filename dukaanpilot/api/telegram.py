"""Telegram update handling: onboarding, text, voice notes and approval buttons.

Order matters: dedupe -> identify the shop -> rate limit -> only then spend model
tokens. Unknown chats never reach the model.
"""

from __future__ import annotations

import logging
from typing import Any

from ..data.models import Shop
from ..hub import Hub
from ..i18n import t
from ..jobs import send_briefing
from ..services import reorders
from ..services.briefing import order_buttons
from ..services.messaging import Button

log = logging.getLogger(__name__)


def _lang(shop: Shop | None) -> str:
    return (shop.settings.get("reply_language") or shop.language) if shop else "en"


async def handle_update(update: dict[str, Any], hub: Hub) -> str:
    """Process one Telegram update and return a short outcome label (for logs and tests)."""
    c = hub.c
    update_id = update.get("update_id")
    if update_id is not None and not c.repo.claim_update("telegram", str(update_id)):
        return "duplicate"
    if "callback_query" in update:
        return await _handle_callback(update["callback_query"], hub)

    msg = update.get("message")
    if not msg:
        return "ignored"
    chat = msg.get("chat") or {}
    if chat.get("type") != "private" or "id" not in chat:
        return "ignored_non_private"
    chat_id = str(chat["id"])
    shop = c.repo.get_shop_by_chat(chat_id)
    text = (msg.get("text") or msg.get("caption") or "").strip()

    if text.startswith("/start"):
        return await _handle_start(chat_id, text, shop, hub)
    if shop is None:
        await c.messenger.send_text(chat_id, t("not_registered", "en"))
        return "unregistered"
    lang = _lang(shop)
    if not hub.limiter.allow(chat_id):
        await c.messenger.send_text(chat_id, t("slow_down", lang))
        return "rate_limited"

    if text.startswith("/help"):
        await c.messenger.send_text(chat_id, t("welcome", lang, owner=shop.owner_name, shop=shop.name))
        return "help"
    if text.startswith("/briefing"):
        await send_briefing(c, hub.writer, shop, c.now())
        return "briefing"
    if text.startswith("/pending"):
        pending = c.repo.list_drafts(shop.id, "pending")
        if not pending:
            await c.messenger.send_text(chat_id, t("no_pending", lang))
        for d in pending:
            body = "\n".join(f"- {i.name}: {i.qty} {i.unit}" for i in d.items)
            await c.messenger.send_text(chat_id, f"{d.id} → {d.supplier_name}\n{body}", order_buttons([{"id": d.id}], lang))
        return "pending"

    audio: tuple[bytes, str] | None = None
    media = msg.get("voice") or msg.get("audio")
    if media:
        if int(media.get("duration", 0)) > c.settings.max_voice_seconds:
            await c.messenger.send_text(chat_id, t("voice_too_long", lang, seconds=c.settings.max_voice_seconds))
            return "voice_too_long"
        try:
            audio = await c.messenger.download_voice(media["file_id"])
        except Exception:  # noqa: BLE001
            log.exception("voice_download_failed", extra={"shop_id": shop.id})
            await c.messenger.send_text(chat_id, t("error", lang))
            return "voice_failed"
        if not audio[0]:
            await c.messenger.send_text(chat_id, t("error", lang))
            return "voice_empty"
    elif msg.get("photo") or msg.get("document"):
        await c.messenger.send_text(chat_id, t("photo_soon", lang))
        return "photo_unsupported"
    if not text and audio is None:
        return "ignored_empty"

    try:
        reply = await hub.gateway.reply(shop=shop, chat_id=chat_id, text=text, audio=audio)
    except Exception:  # noqa: BLE001 - the owner always gets an answer
        log.exception("agent_failed", extra={"shop_id": shop.id})
        reply = t("error", lang)
        await c.messenger.send_text(chat_id, reply)
        return "agent_failed"
    await c.messenger.send_text(chat_id, reply)
    return "voice_replied" if audio else "replied"


async def _handle_start(chat_id: str, text: str, shop: Shop | None, hub: Hub) -> str:
    c = hub.c
    parts = text.split(maxsplit=1)
    code = parts[1].strip() if len(parts) > 1 else ""
    if not code:
        if shop:
            await c.messenger.send_text(chat_id, t("welcome", _lang(shop), owner=shop.owner_name, shop=shop.name))
            return "welcome_back"
        await c.messenger.send_text(chat_id, t("not_registered", "en"))
        return "unregistered"

    target = c.repo.get_shop_by_join_code(code)
    if target is None:
        await c.messenger.send_text(chat_id, t("bad_code", "en"))
        return "bad_code"
    demo = (not c.settings.is_prod) and target.id == c.settings.default_shop_id
    if target.telegram_chat_id and target.telegram_chat_id != chat_id and not demo:
        # A leaked code must not let a stranger take over an already-linked shop.
        await c.messenger.send_text(chat_id, t("bad_code", "en"))
        log.warning("join_code_reuse_blocked", extra={"shop_id": target.id})
        return "relink_blocked"
    c.repo.link_chat(target.id, chat_id)
    if not demo:
        target.join_code = None  # single use
        target.telegram_chat_id = chat_id
        c.repo.upsert_shop(target)
    c.repo.add_audit(target.id, f"owner:{chat_id}", "chat_linked", {}, c.now().isoformat())
    await c.messenger.send_text(chat_id, t("welcome", _lang(target), owner=target.owner_name, shop=target.name))
    return "linked"


async def _handle_callback(cb: dict[str, Any], hub: Hub) -> str:
    c = hub.c
    cb_id = cb.get("id", "")
    chat = (cb.get("message") or {}).get("chat") or {}
    chat_id = str(chat.get("id") or (cb.get("from") or {}).get("id") or "")
    shop = c.repo.get_shop_by_chat(chat_id) if chat_id else None
    if shop is None:
        await c.messenger.answer_callback(cb_id, t("not_registered", "en"))
        return "callback_unregistered"
    lang = _lang(shop)
    action, _, draft_id = str(cb.get("data", "")).partition(":")
    if action not in {"approve", "reject"} or not draft_id:
        await c.messenger.answer_callback(cb_id)
        return "callback_bad"
    actor = f"owner:{chat_id}"
    try:
        if action == "approve":
            decision = reorders.approve(c.repo, shop, draft_id, actor, c.now())
            await c.messenger.answer_callback(cb_id, t("approved", lang, id=decision.draft.id, supplier=decision.draft.supplier_name))
            buttons = [[Button(t("send_wa_btn", lang), url=decision.whatsapp_link)]] if decision.whatsapp_link else None
            await c.messenger.send_text(
                chat_id, t("approved", lang, id=decision.draft.id, supplier=decision.draft.supplier_name), buttons
            )
            return "approved"
        decision = reorders.reject(c.repo, shop, draft_id, actor, c.now())
        await c.messenger.answer_callback(cb_id, t("rejected", lang, id=decision.draft.id))
        await c.messenger.send_text(chat_id, t("rejected", lang, id=decision.draft.id))
        return "rejected"
    except reorders.DraftNotFound:
        await c.messenger.answer_callback(cb_id, t("draft_missing", lang))
        return "draft_missing"
    except reorders.InvalidTransition:
        await c.messenger.answer_callback(cb_id, t("draft_done", lang))
        return "draft_done"
