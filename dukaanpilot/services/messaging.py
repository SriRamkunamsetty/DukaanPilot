"""Messaging channel abstraction. Telegram today; WhatsApp Cloud API plugs in here."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

import httpx

log = logging.getLogger(__name__)
TELEGRAM_LIMIT = 4000  # hard limit is 4096 characters


@dataclass(frozen=True)
class Button:
    text: str
    callback_data: str | None = None
    url: str | None = None


class Messenger(Protocol):
    async def send_text(self, chat_id: str, text: str, buttons: list[list[Button]] | None = None) -> None: ...
    async def answer_callback(self, callback_id: str, text: str | None = None) -> None: ...
    async def download_voice(self, file_id: str) -> tuple[bytes, str]: ...


def chunk_text(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split on paragraph/line boundaries so long answers stay readable."""
    text = text.strip() or "…"
    parts: list[str] = []
    while len(text) > limit:
        cut = max(text.rfind("\n\n", 0, limit), text.rfind("\n", 0, limit), text.rfind(" ", 0, limit))
        cut = cut if cut > limit // 2 else limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    parts.append(text)
    return parts


class TelegramMessenger:
    def __init__(self, token: str, client: httpx.AsyncClient | None = None) -> None:
        self._base = f"https://api.telegram.org/bot{token}"
        self._file_base = f"https://api.telegram.org/file/bot{token}"
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0))

    async def _post(self, method: str, payload: dict) -> dict:
        resp = await self._client.post(f"{self._base}/{method}", json=payload)
        if resp.status_code >= 400:
            # Never log the URL (it contains the token); the description is enough.
            log.error("telegram_api_error", extra={"method": method, "status": resp.status_code, "body": resp.text[:300]})
            resp.raise_for_status()
        return resp.json()

    async def send_text(self, chat_id: str, text: str, buttons: list[list[Button]] | None = None) -> None:
        chunks = chunk_text(text)
        for i, chunk in enumerate(chunks):
            payload: dict = {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True}
            if buttons and i == len(chunks) - 1:
                payload["reply_markup"] = {
                    "inline_keyboard": [
                        [{"text": b.text, **({"url": b.url} if b.url else {"callback_data": b.callback_data})} for b in row]
                        for row in buttons
                    ]
                }
            await self._post("sendMessage", payload)

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        payload: dict = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text[:200]
        await self._post("answerCallbackQuery", payload)

    async def download_voice(self, file_id: str) -> tuple[bytes, str]:
        info = await self._post("getFile", {"file_id": file_id})
        path = info["result"]["file_path"]
        resp = await self._client.get(f"{self._file_base}/{path}")
        resp.raise_for_status()
        mime = "audio/mpeg" if path.endswith(".mp3") else "audio/ogg"
        return resp.content, mime

    async def aclose(self) -> None:
        await self._client.aclose()


@dataclass
class RecordingMessenger:
    """Dev/test messenger: remembers what would have been sent."""

    sent: list[tuple[str, str, list[list[Button]] | None]] = field(default_factory=list)
    callbacks: list[tuple[str, str | None]] = field(default_factory=list)
    voice: dict[str, tuple[bytes, str]] = field(default_factory=dict)

    async def send_text(self, chat_id: str, text: str, buttons: list[list[Button]] | None = None) -> None:
        self.sent.append((str(chat_id), text, buttons))
        log.info("recording_messenger_send", extra={"chat_id": chat_id, "preview": text[:120]})

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        self.callbacks.append((callback_id, text))

    async def download_voice(self, file_id: str) -> tuple[bytes, str]:
        return self.voice.get(file_id, (b"", "audio/ogg"))
