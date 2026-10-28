"""Runtime configuration, read once from environment variables.

Everything that differs between a laptop and Cloud Run lives here so the rest of
the code never touches ``os.environ`` directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        return int(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def _opt(name: str) -> str | None:
    raw = os.environ.get(name)
    return raw.strip() if raw and raw.strip() else None


@dataclass(frozen=True)
class Settings:
    # "dev" relaxes a few safety defaults (demo shop fallback, shared secret).
    # "prod" fails closed.
    environment: str = "dev"
    model: str = "gemini-3.5-flash"
    default_timezone: str = "Asia/Kolkata"
    default_shop_id: str = "demo-kirana"

    # Storage.
    store: str = "sqlite"  # "sqlite" | "firestore"
    db_path: str = "./.local/dukaanpilot.db"
    session_db_url: str = "sqlite+aiosqlite:///./.local/sessions.db"
    seed_demo: bool = False

    # Telegram channel.
    telegram_bot_token: str | None = field(default=None, repr=False)
    telegram_webhook_secret: str | None = field(default=None, repr=False)
    max_voice_seconds: int = 60
    rate_limit_per_minute: int = 20

    # Internal (Cloud Scheduler / developer) authentication.
    scheduler_audience: str | None = None
    scheduler_service_account: str | None = None
    internal_shared_secret: str | None = field(default=None, repr=False)

    # Agent behaviour.
    agent_timeout_seconds: int = 45
    briefing_hour_local: int = 8
    draft_expiry_hours: int = 48

    # Ops.
    serve_web_ui: bool = False
    trace_to_cloud: bool = False
    log_level: str = "INFO"

    @property
    def is_prod(self) -> bool:
        return self.environment == "prod"

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            environment=os.environ.get("DUKAANPILOT_ENV", "dev").strip().lower(),
            model=os.environ.get("DUKAANPILOT_MODEL", cls.model),
            default_timezone=os.environ.get("DUKAANPILOT_TZ", cls.default_timezone),
            default_shop_id=os.environ.get("DUKAANPILOT_DEFAULT_SHOP", cls.default_shop_id),
            store=os.environ.get("DUKAANPILOT_STORE", cls.store).strip().lower(),
            db_path=os.environ.get("DUKAANPILOT_DB_PATH", cls.db_path),
            session_db_url=os.environ.get("DUKAANPILOT_SESSION_DB_URL", cls.session_db_url),
            seed_demo=_bool("DUKAANPILOT_SEED_DEMO", False),
            telegram_bot_token=_opt("TELEGRAM_BOT_TOKEN"),
            telegram_webhook_secret=_opt("TELEGRAM_WEBHOOK_SECRET"),
            max_voice_seconds=_int("DUKAANPILOT_MAX_VOICE_SECONDS", 60),
            rate_limit_per_minute=_int("DUKAANPILOT_RATE_LIMIT_PER_MIN", 20),
            scheduler_audience=_opt("SCHEDULER_AUDIENCE"),
            scheduler_service_account=_opt("SCHEDULER_SERVICE_ACCOUNT"),
            internal_shared_secret=_opt("INTERNAL_SHARED_SECRET"),
            agent_timeout_seconds=_int("DUKAANPILOT_AGENT_TIMEOUT", 45),
            briefing_hour_local=_int("DUKAANPILOT_BRIEFING_HOUR", 8),
            draft_expiry_hours=_int("DUKAANPILOT_DRAFT_EXPIRY_HOURS", 48),
            serve_web_ui=_bool("DUKAANPILOT_WEB_UI", False),
            trace_to_cloud=_bool("DUKAANPILOT_TRACE_TO_CLOUD", False),
            log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        )
