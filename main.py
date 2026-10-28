"""Cloud Run entry point.

ADK's API server (sessions, /run, evals, tracing) plus our own routes: Telegram
webhook, scheduled jobs and health checks. Everything except /healthz and the
Telegram webhook sits behind the auth gate in dukaanpilot/api/auth.py.
"""

from __future__ import annotations

import logging
import os

import uvicorn
from google.adk.cli.fast_api import get_fast_api_app

from dukaanpilot.api import create_app
from dukaanpilot.config import Settings
from dukaanpilot.container import build_container, set_container
from dukaanpilot.hub import Hub, RateLimiter
from dukaanpilot.logging_setup import setup_logging
from dukaanpilot.services.llm import AgentGateway, BriefingWriter

ROOT = os.path.dirname(os.path.abspath(__file__))

settings = Settings.from_env()
setup_logging(settings.log_level)
log = logging.getLogger("dukaanpilot")

for path in (settings.db_path, settings.session_db_url.split("///")[-1]):
    parent = os.path.dirname(os.path.abspath(path))
    if parent and parent != "/":
        os.makedirs(parent, exist_ok=True)

container = build_container(settings)
set_container(container)
hub = Hub(
    c=container,
    gateway=AgentGateway(container),
    writer=BriefingWriter(),
    limiter=RateLimiter(settings.rate_limit_per_minute),
)

adk_app = get_fast_api_app(
    agents_dir=ROOT,
    session_service_uri=settings.session_db_url,
    allow_origins=[],
    web=settings.serve_web_ui,
    trace_to_cloud=settings.trace_to_cloud,
)
app = create_app(hub, adk_app)

log.info(
    "dukaanpilot_started",
    extra={"environment": settings.environment, "model": settings.model, "store": settings.store},
)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))  # noqa: S104 - Cloud Run needs 0.0.0.0
