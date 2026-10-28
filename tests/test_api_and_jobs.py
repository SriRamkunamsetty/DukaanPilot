from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from dukaanpilot.api import create_app
from dukaanpilot.hub import Hub, RateLimiter
from dukaanpilot.jobs import run_morning_briefings, run_stock_watch

from .conftest import NOW, make_world
from .test_telegram import FakeGateway, FakeWriter, msg

SECRET = "tg-secret"
INTERNAL = "internal-secret"
SA = "scheduler@proj.iam.gserviceaccount.com"
AUD = "https://dukaanpilot-abc.a.run.app"


def client_for(**settings_kw):
    w = make_world(**settings_kw)
    w.repo.link_chat(w.shop_id, "1001")
    hub = Hub(c=w.c, gateway=FakeGateway(), writer=FakeWriter(), limiter=RateLimiter(100))

    def verifier(token, audience):
        assert audience == AUD
        if token == "good":
            return {"email": SA, "email_verified": True}
        if token == "other-sa":
            return {"email": "attacker@evil.iam.gserviceaccount.com", "email_verified": True}
        raise ValueError("bad signature")

    return TestClient(create_app(hub, verifier=verifier)), w, hub


PROD = dict(environment="prod", telegram_webhook_secret=SECRET, internal_shared_secret=INTERNAL,
            scheduler_audience=AUD, scheduler_service_account=SA)


def test_health_is_public_even_in_production():
    client, *_ = client_for(**PROD)
    assert client.get("/healthz").json()["status"] == "ok"
    assert client.get("/readyz").json() == {"status": "ready"}


def test_webhook_requires_the_telegram_secret():
    client, w, hub = client_for(**PROD)
    body = msg("sales?")
    assert client.post("/webhooks/telegram", json=body).status_code == 401
    assert client.post("/webhooks/telegram", json=body, headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"}).status_code == 401
    assert hub.gateway.calls == []
    ok = client.post("/webhooks/telegram", json=body, headers={"X-Telegram-Bot-Api-Secret-Token": SECRET})
    assert ok.status_code == 200 and ok.json() == {"ok": True}
    assert len(hub.gateway.calls) == 1


def test_production_webhook_fails_closed_without_a_secret():
    client, *_ = client_for(environment="prod", internal_shared_secret=INTERNAL)
    assert client.post("/webhooks/telegram", json=msg("hi")).status_code == 503


def test_webhook_swallows_internal_errors_so_telegram_does_not_retry_forever(monkeypatch):
    client, w, hub = client_for(**PROD)
    hub.gateway = FakeGateway(fail=True)
    r = client.post("/webhooks/telegram", json=msg("hi"), headers={"X-Telegram-Bot-Api-Secret-Token": SECRET})
    assert r.status_code == 200


def test_malformed_webhook_body_is_a_400():
    client, *_ = client_for(**PROD)
    r = client.post("/webhooks/telegram", content=b"not json", headers={"X-Telegram-Bot-Api-Secret-Token": SECRET})
    assert r.status_code == 400


def test_internal_routes_reject_everyone_without_credentials():
    client, *_ = client_for(**PROD)
    assert client.post("/internal/jobs/run").status_code == 401
    assert client.post("/internal/jobs/run", headers={"X-Internal-Secret": "wrong"}).status_code == 401
    assert client.get("/list-apps").status_code == 401  # ADK-style routes are behind the same gate
    assert client.post("/internal/jobs/run", headers={"Authorization": "Bearer garbage"}).status_code == 401
    assert client.post("/internal/jobs/run", headers={"Authorization": "Bearer other-sa"}).status_code == 401


def test_scheduler_with_the_right_identity_runs_jobs():
    client, w, hub = client_for(**PROD)
    r = client.post("/internal/jobs/run?job=briefing&force=true", headers={"Authorization": "Bearer good"})
    assert r.status_code == 200 and r.json()["briefing"] == {"demo-kirana": "sent"}
    assert w.messenger.sent and "శుభోదయం" in w.messenger.sent[-1][1]


def test_shared_secret_also_works_for_developers():
    client, *_ = client_for(**PROD)
    r = client.post("/internal/jobs/run?job=stock_watch", headers={"X-Internal-Secret": INTERNAL})
    assert r.status_code == 200 and "stock_watch" in r.json()


def test_production_with_nothing_configured_denies_everything():
    client, *_ = client_for(environment="prod", telegram_webhook_secret=SECRET)
    assert client.post("/internal/jobs/run").status_code == 401


def test_local_dev_without_credentials_is_open_for_convenience():
    client, *_ = client_for(environment="dev")
    assert client.post("/internal/jobs/run?job=stock_watch").status_code == 200


def test_job_parameter_is_validated():
    client, *_ = client_for(**PROD)
    r = client.post("/internal/jobs/run?job=rm-rf", headers={"X-Internal-Secret": INTERNAL})
    assert r.status_code == 422


# -- job behaviour -----------------------------------------------------------------

async def test_briefing_is_sent_once_per_day(linked):
    writer = FakeWriter()
    assert await run_morning_briefings(linked.c, writer) == {"demo-kirana": "sent"}
    assert await run_morning_briefings(linked.c, writer) == {"demo-kirana": "already_sent"}
    assert len(linked.messenger.sent) == 1
    tomorrow = NOW + timedelta(days=1)
    assert await run_morning_briefings(linked.c, writer, tomorrow) == {"demo-kirana": "sent"}


async def test_briefing_waits_for_the_shops_local_hour(linked):
    early = NOW - timedelta(hours=3)  # 05:30 in India
    assert await run_morning_briefings(linked.c, FakeWriter(), early) == {"demo-kirana": "not_due"}
    linked.repo.set_setting("demo-kirana", "briefing_hour", 5)
    assert await run_morning_briefings(linked.c, FakeWriter(), early) == {"demo-kirana": "sent"}


async def test_failed_briefing_is_retried_on_the_next_run(linked):
    class Boom:
        async def write(self, data):
            raise RuntimeError("model and fallback both down")

    assert await run_morning_briefings(linked.c, Boom()) == {"demo-kirana": "failed"}
    assert await run_morning_briefings(linked.c, FakeWriter()) == {"demo-kirana": "sent"}


async def test_shops_without_a_chat_are_skipped(world):
    assert await run_morning_briefings(world.c, FakeWriter()) == {"demo-kirana": "no_chat"}
    assert await run_stock_watch(world.c) == {"demo-kirana": "no_chat"}


async def test_briefing_creates_drafts_with_approval_buttons(linked):
    await run_morning_briefings(linked.c, FakeWriter())
    _, text, buttons = linked.messenger.sent[-1]
    assert len(linked.repo.list_drafts("demo-kirana", "pending")) == 2
    assert [b.callback_data.split(":")[0] for row in buttons for b in row] == ["approve", "reject", "approve", "reject"]


async def test_stock_watch_does_not_repeat_what_the_briefing_just_said(linked):
    await run_morning_briefings(linked.c, FakeWriter())
    assert await run_stock_watch(linked.c) == {"demo-kirana": "nothing_new"}
    assert len(linked.messenger.sent) == 1


async def test_stock_watch_alerts_once_per_item_per_day(linked):
    first = await run_stock_watch(linked.c)
    assert first["demo-kirana"].startswith("alerted:")
    assert await run_stock_watch(linked.c) == {"demo-kirana": "nothing_new"}
    assert len(linked.messenger.sent) == 1
    assert linked.messenger.sent[0][2] is not None  # drafts to approve came with the alert


async def test_stock_watch_stays_quiet_at_night_and_when_disabled(linked):
    night = NOW + timedelta(hours=14)  # ~22:30 in India
    assert await run_stock_watch(linked.c, night) == {"demo-kirana": "quiet_hours"}
    linked.repo.set_setting("demo-kirana", "stock_alerts", False)
    assert await run_stock_watch(linked.c) == {"demo-kirana": "disabled"}
    assert linked.messenger.sent == []


@pytest.mark.parametrize("n", [1])
def test_rate_limiter_window(n):
    t = [0.0]
    rl = RateLimiter(2, clock=lambda: t[0])
    assert rl.allow("a") and rl.allow("a") and not rl.allow("a")
    assert rl.allow("b")
    t[0] = 61
    assert rl.allow("a")
