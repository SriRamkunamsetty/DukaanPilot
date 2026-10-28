"""Dependency container: one place that wires settings, storage, channel and clock.

Tools and request handlers get their dependencies from here, which is also what
lets tests swap in an in-memory database, a fake messenger and a frozen clock.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from .config import Settings
from .data.repository import Repository, open_repository
from .services.festivals import FestivalCalendar
from .services.messaging import Messenger, RecordingMessenger, TelegramMessenger

log = logging.getLogger(__name__)


@dataclass
class Container:
    settings: Settings
    repo: Repository
    messenger: Messenger
    calendar: FestivalCalendar
    clock: Callable[[], datetime]

    def now(self) -> datetime:
        return self.clock()


_container: Container | None = None
_lock = threading.Lock()


def build_container(settings: Settings | None = None) -> Container:
    settings = settings or Settings.from_env()
    repo = open_repository(settings)
    messenger: Messenger = (
        TelegramMessenger(settings.telegram_bot_token) if settings.telegram_bot_token else RecordingMessenger()
    )
    c = Container(
        settings=settings, repo=repo, messenger=messenger, calendar=FestivalCalendar.load(),
        clock=lambda: datetime.now(UTC),
    )
    if settings.seed_demo and repo.get_shop(settings.default_shop_id) is None:
        from .data.seed import seed_demo

        today = c.now().astimezone(ZoneInfo(settings.default_timezone)).date()
        seed_demo(repo, today, shop_id=settings.default_shop_id)
        log.info("demo_data_seeded", extra={"shop_id": settings.default_shop_id})
    return c


def get_container() -> Container:
    global _container
    if _container is None:
        with _lock:
            if _container is None:
                _container = build_container()
    return _container


def set_container(c: Container | None) -> None:
    """Install (or clear) the process-wide container. Used by tests and main.py."""
    global _container
    _container = c
