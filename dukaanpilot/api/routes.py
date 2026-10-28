"""HTTP routes added on top of ADK's own API server."""

from __future__ import annotations

import hmac
import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request

from ..hub import Hub
from ..jobs import run_morning_briefings, run_stock_watch
from .auth import AuthGate, InternalAuth
from .telegram import handle_update

log = logging.getLogger(__name__)
VERSION = "0.1.0"


def build_router(hub: Hub) -> APIRouter:
    r = APIRouter()
    settings = hub.c.settings

    @r.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": VERSION}

    @r.get("/readyz")
    async def readyz() -> dict[str, Any]:
        try:
            hub.c.repo.list_shops()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=503, detail="storage unavailable") from exc
        return {"status": "ready"}

    @r.post("/webhooks/telegram")
    async def telegram_webhook(request: Request) -> dict[str, bool]:
        secret = settings.telegram_webhook_secret
        if not secret:
            if settings.is_prod:
                raise HTTPException(status_code=503, detail="webhook secret not configured")
            log.warning("telegram_webhook_unauthenticated_dev_mode")
        else:
            supplied = request.headers.get("x-telegram-bot-api-secret-token", "")
            if not hmac.compare_digest(supplied, secret):
                raise HTTPException(status_code=401, detail="bad secret")
        try:
            update = await request.json()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail="invalid json") from exc
        try:
            outcome = await handle_update(update, hub)
            log.info("telegram_update", extra={"outcome": outcome, "update_id": update.get("update_id")})
        except Exception:  # noqa: BLE001 - always 200 so Telegram does not retry-storm us
            log.exception("telegram_update_failed", extra={"update_id": update.get("update_id")})
        return {"ok": True}

    @r.post("/internal/jobs/run")
    async def run_jobs(
        job: str = Query("all", pattern="^(all|briefing|stock_watch)$"),
        force: bool = False,
        shop_id: str | None = None,
    ) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if job in {"all", "briefing"}:
            out["briefing"] = await run_morning_briefings(hub.c, hub.writer, force=force, shop_id=shop_id)
        if job in {"all", "stock_watch"}:
            out["stock_watch"] = await run_stock_watch(hub.c, shop_id=shop_id)
        log.info("jobs_run", extra={"job": job, "force": force, "result": out})
        return out

    return r


def create_app(hub: Hub, base_app: FastAPI | None = None, verifier: Callable[[str, str], dict[str, Any]] | None = None) -> FastAPI:
    """Attach our routes and the auth gate to ADK's app (or a bare app in tests)."""
    app = base_app or FastAPI(title="DukaanPilot", version=VERSION)
    app.include_router(build_router(hub))
    app.add_middleware(AuthGate, auth=InternalAuth(hub.c.settings, verifier))
    return app
