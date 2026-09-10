"""Static bearer-token check for the bridge.

This is deliberately NOT a full OAuth authorization server — that's a lot of
machinery for a single-user personal project. Instead it matches Claude's
"static header" custom-connector mode: when you add this as a custom
connector, you (as the admin of your own org) enter one fixed value and
Claude sends it as `Authorization: Bearer <value>` on every request. This
middleware just checks that header against BRIDGE_TOKEN before anything
reaches Home Assistant.

Generate a long, random value for BRIDGE_TOKEN, e.g.:
    python3 -c "import secrets; print(secrets.token_urlsafe(32))"

If Claude's static-header beta isn't available on your account when you try
to add the connector, the fallback is to put this bridge behind a reverse
proxy / tunnel that itself requires a client certificate or a Cloudflare
Access policy — see README.md.
"""

from __future__ import annotations

import os
import secrets

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp


class BearerTokenMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, expected_token: str | None = None) -> None:
        super().__init__(app)
        self._expected = expected_token or os.environ["BRIDGE_TOKEN"]

    async def dispatch(self, request: Request, call_next):
        auth = request.headers.get("authorization", "")
        token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
        if not token or not secrets.compare_digest(token, self._expected):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


def protect(app: Starlette) -> Starlette:
    app.add_middleware(BearerTokenMiddleware)
    return app
