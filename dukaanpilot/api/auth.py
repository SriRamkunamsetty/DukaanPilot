"""Authentication for everything that is not the public health check or the Telegram webhook.

Two ways in, both checked in constant time / by signature:
* Cloud Scheduler (and other Google callers): an OIDC token whose audience is this
  service's URL and whose email is the expected service account.
* Developers: a shared secret header (``X-Internal-Secret``), for local use.

In production the gate fails closed: with nothing configured, nothing gets in.
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import Callable
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from ..config import Settings

log = logging.getLogger(__name__)
PUBLIC_PREFIXES = ("/healthz", "/readyz", "/webhooks/telegram")


def _default_verifier(token: str, audience: str) -> dict[str, Any]:
    from google.auth.transport import requests as g_requests
    from google.oauth2 import id_token

    return id_token.verify_oauth2_token(token, g_requests.Request(), audience=audience)


class InternalAuth:
    def __init__(self, settings: Settings, verifier: Callable[[str, str], dict[str, Any]] | None = None) -> None:
        self._s = settings
        self._verify = verifier or _default_verifier

    @property
    def configured(self) -> bool:
        s = self._s
        return bool(s.internal_shared_secret or (s.scheduler_audience and s.scheduler_service_account))

    async def check(self, request: Request) -> str | None:
        """Return None if allowed, else a short reason."""
        s = self._s
        if not self.configured:
            if s.is_prod:
                return "internal authentication is not configured"
            return None  # local development convenience only

        secret = request.headers.get("x-internal-secret")
        if secret and s.internal_shared_secret and hmac.compare_digest(secret, s.internal_shared_secret):
            return None

        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer ") and s.scheduler_audience and s.scheduler_service_account:
            token = auth[7:].strip()
            try:
                claims = await run_in_threadpool(self._verify, token, s.scheduler_audience)
            except Exception:  # noqa: BLE001 - any verification failure is a rejection
                log.warning("oidc_verification_failed")
                return "invalid token"
            email = str(claims.get("email", ""))
            if claims.get("email_verified") and hmac.compare_digest(email, s.scheduler_service_account):
                return None
            log.warning("oidc_wrong_identity", extra={"email": email})
            return "token is not from the expected service account"
        return "missing credentials"


class AuthGate(BaseHTTPMiddleware):
    def __init__(self, app: Any, auth: InternalAuth) -> None:
        super().__init__(app)
        self._auth = auth

    async def dispatch(self, request: Request, call_next: Callable[[Request], Any]) -> Any:
        path = request.url.path
        if path.startswith(PUBLIC_PREFIXES):
            return await call_next(request)
        reason = await self._auth.check(request)
        if reason:
            return JSONResponse({"detail": "unauthorized", "reason": reason}, status_code=401)
        return await call_next(request)
