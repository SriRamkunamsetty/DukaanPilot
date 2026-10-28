"""Shared plumbing for agent tools.

Security rule: a tool never accepts a shop identifier from the model. The shop
comes from session state set by our own code, so the model cannot be talked into
reading or changing another business's data.
"""

from __future__ import annotations

import functools
import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from google.adk.tools import ToolContext

from ..container import Container, get_container
from ..data.models import Product, Shop
from ..services import catalog
from ..services.timeutil import local_today

log = logging.getLogger(__name__)


class ToolFailure(Exception):
    """An expected, user-explainable failure. The message is shown to the model."""


@dataclass
class Env:
    c: Container
    shop: Shop
    today: date
    now: datetime
    actor: str
    chat_id: str | None

    @property
    def lang(self) -> str:
        return self.shop.settings.get("reply_language") or self.shop.language


def tool_env(tool_context: ToolContext | None) -> Env:
    c = get_container()
    state = tool_context.state if tool_context is not None else {}
    shop_id = state.get("shop_id") or (None if c.settings.is_prod else c.settings.default_shop_id)
    if not shop_id:
        raise ToolFailure("This conversation is not linked to a shop.")
    shop = c.repo.get_shop(shop_id)
    if shop is None:
        raise ToolFailure("This shop is not set up yet.")
    now = c.now()
    return Env(
        c=c, shop=shop, today=local_today(shop, now), now=now,
        actor=state.get("actor") or "agent", chat_id=state.get("chat_id"),
    )


def resolve_product(env: Env, query: str) -> Product:
    if not query or not query.strip():
        raise ToolFailure("Which product? Please name it.")
    product, candidates = catalog.resolve(env.c.repo.list_products(env.shop.id), query)
    if product:
        return product
    if candidates:
        names = ", ".join(p.name for p in candidates[:4])
        raise ToolFailure(f"'{query}' could match several products: {names}. Ask the owner which one they mean.")
    raise ToolFailure(f"No product matches '{query}'. Ask the owner to check the name.")


def safe_tool(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Turn exceptions into the ``{"status": "error"}`` shape ADK tools should return."""

    def _fail(exc: Exception) -> dict[str, Any]:
        if isinstance(exc, ToolFailure | ValueError):
            return {"status": "error", "error_message": str(exc)}
        log.exception("tool_crashed", extra={"tool": fn.__name__})
        return {"status": "error", "error_message": "Something went wrong while running this tool."}

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def awrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
            try:
                return await fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - boundary: never let a tool crash the agent loop
                return _fail(exc)

        return awrapper

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            return _fail(exc)

    return wrapper


def ok(**fields: Any) -> dict[str, Any]:
    return {"status": "success", **fields}
