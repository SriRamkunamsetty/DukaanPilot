from __future__ import annotations

import itertools

import pytest

from dukaanpilot.api.telegram import handle_update
from dukaanpilot.data.models import Shop
from dukaanpilot.hub import Hub, RateLimiter
from dukaanpilot.services import inventory, reorders
from dukaanpilot.services.briefing import fallback_text

from .conftest import NOW, TODAY, make_world

_ids = itertools.count(1)


class FakeGateway:
    def __init__(self, reply="Sales were low because milk ran out.", fail=False):
        self.calls = []
        self._reply, self._fail = reply, fail

    async def reply(self, *, shop, chat_id, text, audio=None):
        self.calls.append({"shop": shop.id, "chat_id": chat_id, "text": text, "audio": audio})
        if self._fail:
            raise RuntimeError("model down")
        return self._reply


class FakeWriter:
    async def write(self, data):
        return fallback_text(data)


def msg(text=None, chat_id=1001, chat_type="private", **extra):
    m = {"chat": {"id": chat_id, "type": chat_type}, **extra}
    if text is not None:
        m["text"] = text
    return {"update_id": next(_ids), "message": m}


def tap(data, chat_id=1001):
    return {"update_id": next(_ids), "callback_query": {"id": f"cb{next(_ids)}", "data": data, "from": {"id": chat_id},
                                                         "message": {"chat": {"id": chat_id, "type": "private"}}}}


@pytest.fixture
def hub(linked):
    return Hub(c=linked.c, gateway=FakeGateway(), writer=FakeWriter(), limiter=RateLimiter(100))


def last(hub):
    return hub.c.messenger.sent[-1]


async def test_text_goes_to_the_agent_and_back(hub):
    assert await handle_update(msg("how were sales yesterday?"), hub) == "replied"
    assert hub.gateway.calls == [{"shop": "demo-kirana", "chat_id": "1001", "text": "how were sales yesterday?", "audio": None}]
    assert last(hub)[:2] == ("1001", "Sales were low because milk ran out.")


async def test_unknown_chat_never_reaches_the_model(world):
    hub = Hub(c=world.c, gateway=FakeGateway(), writer=FakeWriter(), limiter=RateLimiter(100))
    assert await handle_update(msg("hello", chat_id=777), hub) == "unregistered"
    assert hub.gateway.calls == []
    assert "/start" in last(hub)[1]


async def test_start_with_code_links_the_chat(world):
    hub = Hub(c=world.c, gateway=FakeGateway(), writer=FakeWriter(), limiter=RateLimiter(100))
    assert await handle_update(msg("/start DEMO-KIRANA", chat_id=2002), hub) == "linked"
    assert world.repo.get_shop_by_chat("2002").id == "demo-kirana"
    assert "Ramesh" in last(hub)[1] and "Lakshmi General Stores" in last(hub)[1]
    assert await handle_update(msg("/start nonsense", chat_id=3003), hub) == "bad_code"


async def test_join_code_is_single_use_in_production():
    w = make_world(environment="prod", default_shop_id="other")
    hub = Hub(c=w.c, gateway=FakeGateway(), writer=FakeWriter(), limiter=RateLimiter(100))
    assert await handle_update(msg("/start DEMO-KIRANA", chat_id=1), hub) == "linked"
    assert w.repo.get_shop("demo-kirana").join_code is None
    assert await handle_update(msg("/start DEMO-KIRANA", chat_id=2), hub) == "bad_code"
    # Even if a code leaks again, a linked shop cannot be taken over by another chat.
    shop = w.repo.get_shop("demo-kirana")
    shop.join_code = "LEAKED"
    w.repo.upsert_shop(shop)
    assert await handle_update(msg("/start LEAKED", chat_id=3), hub) == "relink_blocked"
    assert w.repo.get_shop_by_chat("1").id == "demo-kirana" and w.repo.get_shop_by_chat("3") is None


async def test_telegram_retries_are_ignored(hub):
    update = msg("hello")
    assert await handle_update(update, hub) == "replied"
    assert await handle_update(update, hub) == "duplicate"
    assert len(hub.gateway.calls) == 1


async def test_flooding_is_rate_limited_before_the_model(linked):
    hub = Hub(c=linked.c, gateway=FakeGateway(), writer=FakeWriter(), limiter=RateLimiter(2))
    outcomes = [await handle_update(msg(f"q{i}"), hub) for i in range(4)]
    assert outcomes == ["replied", "replied", "rate_limited", "rate_limited"]
    assert len(hub.gateway.calls) == 2


async def test_group_chats_are_ignored(hub):
    assert await handle_update(msg("hi", chat_type="group"), hub) == "ignored_non_private"
    assert hub.gateway.calls == []


async def test_voice_note_is_passed_to_the_model_as_audio(hub):
    hub.c.messenger.voice["f1"] = (b"OggS-bytes", "audio/ogg")
    assert await handle_update(msg(voice={"file_id": "f1", "duration": 6}), hub) == "voice_replied"
    call = hub.gateway.calls[0]
    assert call["text"] == "" and call["audio"] == (b"OggS-bytes", "audio/ogg")


async def test_long_voice_note_is_refused_politely(hub):
    assert await handle_update(msg(voice={"file_id": "f1", "duration": 600}), hub) == "voice_too_long"
    assert hub.gateway.calls == [] and "60" in last(hub)[1] or "౬౦" in last(hub)[1]


async def test_photo_bills_are_not_silently_dropped(hub):
    assert await handle_update(msg(photo=[{"file_id": "p"}]), hub) == "photo_unsupported"
    assert hub.gateway.calls == []


async def test_agent_failure_still_answers_the_owner(linked):
    hub = Hub(c=linked.c, gateway=FakeGateway(fail=True), writer=FakeWriter(), limiter=RateLimiter(100))
    assert await handle_update(msg("sales?"), hub) == "agent_failed"
    assert "క్షమించండి" in last(hub)[1]  # the shop's language is Telugu


async def test_briefing_command_sends_buttons_for_pending_orders(hub):
    assert await handle_update(msg("/briefing"), hub) == "briefing"
    chat, text, buttons = last(hub)
    assert "శుభోదయం Ramesh" in text
    labels = [b.callback_data for row in buttons for b in row]
    assert len(labels) == 4 and all(label.split(":")[0] in {"approve", "reject"} for label in labels)


async def test_pending_command_lists_drafts_with_buttons(hub):
    assert await handle_update(msg("/pending"), hub) == "pending"
    assert "no orders" in last(hub)[1].lower() or "ఆర్డర్" in last(hub)[1]
    shop = hub.c.repo.get_shop("demo-kirana")
    reorders.create_drafts(hub.c.repo, shop, inventory.stock_statuses(hub.c.repo, shop.id, TODAY, hub.c.calendar), NOW)
    await handle_update(msg("/pending"), hub)
    assert last(hub)[2] is not None


async def _first_draft(hub):
    shop = hub.c.repo.get_shop("demo-kirana")
    return reorders.create_drafts(hub.c.repo, shop, inventory.stock_statuses(hub.c.repo, shop.id, TODAY, hub.c.calendar), NOW)[0]


async def test_tapping_approve_returns_a_whatsapp_link_and_is_idempotent(hub):
    d = await _first_draft(hub)
    assert await handle_update(tap(f"approve:{d.id}"), hub) == "approved"
    chat, text, buttons = last(hub)
    assert buttons[0][0].url.startswith("https://wa.me/")
    assert hub.c.repo.get_draft("demo-kirana", d.id).status == "approved"
    assert await handle_update(tap(f"approve:{d.id}"), hub) == "draft_done"  # double tap is harmless
    assert sum(1 for _, _, b in hub.c.messenger.sent if b and b[0][0].url) == 1  # link sent only once


async def test_tapping_cancel_sends_nothing(hub):
    d = await _first_draft(hub)
    assert await handle_update(tap(f"reject:{d.id}"), hub) == "rejected"
    assert hub.c.repo.get_draft("demo-kirana", d.id).status == "rejected"
    assert all(not (b and b[0][0].url) for _, _, b in hub.c.messenger.sent)


async def test_callbacks_from_strangers_or_garbage_do_nothing(hub):
    d = await _first_draft(hub)
    assert await handle_update(tap(f"approve:{d.id}", chat_id=999), hub) == "callback_unregistered"
    assert await handle_update(tap("approve:"), hub) == "callback_bad"
    assert await handle_update(tap("delete:everything"), hub) == "callback_bad"
    assert await handle_update(tap("approve:RDNOPE"), hub) == "draft_missing"
    assert hub.c.repo.get_draft("demo-kirana", d.id).status == "pending"


async def test_typing_yes_does_not_approve_anything(hub):
    d = await _first_draft(hub)
    await handle_update(msg("yes approve it"), hub)
    assert hub.c.repo.get_draft("demo-kirana", d.id).status == "pending"


def test_shop_dataclass_defaults():
    assert Shop("a", "b", "c").language == "en"
