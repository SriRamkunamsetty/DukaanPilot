from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dukaanpilot.config import Settings  # noqa: E402
from dukaanpilot.container import Container, set_container  # noqa: E402
from dukaanpilot.data.seed import seed_demo  # noqa: E402
from dukaanpilot.data.sqlite_repo import SqliteRepository  # noqa: E402
from dukaanpilot.services.festivals import FestivalCalendar  # noqa: E402
from dukaanpilot.services.messaging import RecordingMessenger  # noqa: E402

# Friday 16 Oct 2026, 08:30 in India. Dussehra is four days away.
NOW = datetime(2026, 10, 16, 3, 0, tzinfo=UTC)
TODAY = NOW.astimezone(ZoneInfo("Asia/Kolkata")).date()


@dataclass
class World:
    c: Container
    repo: SqliteRepository
    shop_id: str
    messenger: RecordingMessenger
    clock: list  # one-element list so tests can move time


def make_world(**settings_kw) -> World:
    repo = SqliteRepository(":memory:")
    shop = seed_demo(repo, TODAY)
    messenger = RecordingMessenger()
    clock = [NOW]
    settings = Settings(**{"environment": "dev", **settings_kw})
    c = Container(settings=settings, repo=repo, messenger=messenger, calendar=FestivalCalendar.load(), clock=lambda: clock[0])
    set_container(c)
    return World(c=c, repo=repo, shop_id=shop.id, messenger=messenger, clock=clock)


@pytest.fixture
def world():
    w = make_world()
    yield w
    set_container(None)


@pytest.fixture
def linked(world):
    """The demo shop with a Telegram chat linked."""
    world.repo.link_chat(world.shop_id, "1001")
    return world


def tool_ctx(shop_id: str | None = "demo-kirana", chat_id: str | None = None):
    state = {}
    if shop_id:
        state["shop_id"] = shop_id
    if chat_id:
        state["chat_id"] = chat_id
    state["actor"] = "owner:test"
    return SimpleNamespace(state=state)
