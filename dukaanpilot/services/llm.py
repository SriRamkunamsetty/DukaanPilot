"""Glue between our service and the ADK runner: chat replies and briefing text."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService, InMemorySessionService
from google.genai import types

from ..container import Container
from ..data.models import Shop
from ..i18n import t
from .briefing import fallback_text, is_grounded
from .timeutil import local_today

log = logging.getLogger(__name__)
APP_NAME = "dukaanpilot"


def _final_text(events: list[Any]) -> str:
    chunks = []
    for ev in events:
        if ev.is_final_response() and ev.content and ev.content.parts:
            chunks.append("".join(p.text for p in ev.content.parts if getattr(p, "text", None)))
    return "\n".join(c for c in chunks if c).strip()


class AgentGateway:
    """Runs one owner message (text and/or voice) through the agent and returns the reply."""

    def __init__(self, c: Container, agent: Any = None, session_service: BaseSessionService | None = None) -> None:
        self._c = c
        self._agent = agent
        self._svc = session_service
        self._runner: Runner | None = None

    def _ensure(self) -> tuple[Runner, BaseSessionService]:
        if self._runner is None:
            if self._agent is None:
                from ..agent import root_agent

                self._agent = root_agent
            if self._svc is None:
                from google.adk.sessions import DatabaseSessionService

                self._svc = DatabaseSessionService(db_url=self._c.settings.session_db_url)
            self._runner = Runner(app_name=APP_NAME, agent=self._agent, session_service=self._svc)
        return self._runner, self._svc  # type: ignore[return-value]

    async def reply(
        self, *, shop: Shop, chat_id: str, text: str, audio: tuple[bytes, str] | None = None
    ) -> str:
        runner, svc = self._ensure()
        user_id = f"tg:{chat_id}"
        # One session per chat per local day: bounded context, durable facts live in the database.
        day = local_today(shop, self._c.now()).strftime("%Y%m%d")
        session_id = f"tg-{chat_id}-{day}"
        if await svc.get_session(app_name=APP_NAME, user_id=user_id, session_id=session_id) is None:
            await svc.create_session(
                app_name=APP_NAME, user_id=user_id, session_id=session_id,
                state={"shop_id": shop.id, "chat_id": str(chat_id), "actor": f"owner:{chat_id}"},
            )
        parts: list[types.Part] = []
        if text:
            parts.append(types.Part(text=text))
        if audio:
            data, mime = audio
            if not text:
                parts.append(types.Part(text="This is a voice note from the shop owner. Understand it and respond to it."))
            parts.append(types.Part.from_bytes(data=data, mime_type=mime))
        message = types.Content(role="user", parts=parts)

        events: list[Any] = []
        async with asyncio.timeout(self._c.settings.agent_timeout_seconds):
            async for ev in runner.run_async(user_id=user_id, session_id=session_id, new_message=message):
                events.append(ev)
        answer = _final_text(events)
        if not answer:
            log.warning("agent_empty_reply", extra={"shop_id": shop.id})
            return t("error", shop.settings.get("reply_language") or shop.language)
        return answer


class BriefingWriter:
    """Phrases the briefing with the model, but never lets an ungrounded figure through."""

    def __init__(self, agent: Any = None, timeout: int = 30) -> None:
        self._agent = agent
        self._timeout = timeout
        self._runner: Runner | None = None
        self._svc = InMemorySessionService()

    def _ensure(self) -> Runner:
        if self._runner is None:
            if self._agent is None:
                from ..agent import briefing_agent

                self._agent = briefing_agent
            self._runner = Runner(app_name="briefing_writer", agent=self._agent, session_service=self._svc)
        return self._runner

    async def _generate(self, data: dict[str, Any]) -> str:
        import json

        runner = self._ensure()
        sid = uuid.uuid4().hex
        await self._svc.create_session(app_name="briefing_writer", user_id="system", session_id=sid)
        message = types.Content(role="user", parts=[types.Part(text=json.dumps(data, ensure_ascii=False, default=str))])
        events = []
        async with asyncio.timeout(self._timeout):
            async for ev in runner.run_async(user_id="system", session_id=sid, new_message=message):
                events.append(ev)
        await self._svc.delete_session(app_name="briefing_writer", user_id="system", session_id=sid)
        return _final_text(events)

    async def write(self, data: dict[str, Any]) -> str:
        try:
            text = await self._generate(data)
            if text and len(text) <= 1200 and is_grounded(text, data):
                return text
            log.warning("briefing_rejected", extra={"reason": "empty_or_ungrounded"})
        except Exception:  # noqa: BLE001 - the briefing must go out even if the model is down
            log.exception("briefing_model_failed")
        return fallback_text(data)
