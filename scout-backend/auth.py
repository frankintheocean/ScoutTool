"""Optional HTTP Basic Auth for ScoutBot Web, plus the local-launch token
check Dashboard's Electron shell uses for its own loopback services.

ScoutBot has no user accounts — it's a single-operator tool where every
request (read or write) is trusted equally. That's fine bound to
127.0.0.1, but WEB_HOST can be changed to expose it on a LAN/the
internet, and until now doing that gave anyone who could reach the port
full read/write access with zero authentication.

This adds a single shared username/password (HTTP Basic Auth), checked
once per request via ASGI middleware so it covers every route uniformly
(the JSON API, the SSE live feed, and the static frontend) without
touching each endpoint individually. It's opt-in: if WEB_USERNAME/
WEB_PASSWORD aren't both set in .env, auth is skipped entirely and
behavior is unchanged from before (matches the default 127.0.0.1-only
setup where this isn't needed).
"""

import base64
import binascii
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

import config


class BasicAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if not config.auth_enabled():
            return await call_next(request)

        if _is_authorized(request.headers.get("authorization")):
            return await call_next(request)

        return Response(
            status_code=401,
            content="Authentication required",
            headers={"WWW-Authenticate": 'Basic realm="ScoutBot", charset="UTF-8"'},
        )


def _is_authorized(header_value):
    if not header_value or not header_value.startswith("Basic "):
        return False

    try:
        decoded = base64.b64decode(header_value[len("Basic "):]).decode("utf-8")
        username, _, password = decoded.partition(":")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False

    # constant-time comparisons so response timing can't leak how many
    # leading characters of the username/password were correct
    valid_user = secrets.compare_digest(username.encode("utf-8"), config.WEB_USERNAME.encode("utf-8"))
    valid_pass = secrets.compare_digest(password.encode("utf-8"), config.WEB_PASSWORD.encode("utf-8"))
    return valid_user and valid_pass


LOCAL_TOKEN_HEADER = "x-dashboard-local-token"


class LocalServiceTokenMiddleware(BaseHTTPMiddleware):
    """Requires DASHBOARD_LOCAL_TOKEN (see config.py) on every request when
    it's set — which Dashboard's Electron shell always sets on the child
    process env it spawns this service with (see electron/main/scoutBackend.ts
    and localServiceAuth.ts), and injects into every request the embedded UI
    makes via session.webRequest, so nothing in the app itself needs to know
    or handle this token directly.

    This exists alongside BasicAuthMiddleware, not instead of it: Basic Auth
    is an opt-in credential a *person* sets for LAN/remote exposure; this is
    an always-on, auto-generated secret that closes the local, same-machine
    gap — even bound to 127.0.0.1, any other process already running on the
    machine could otherwise reach this service exactly as easily as
    Dashboard's own renderer, with no credential at all required. Skipped
    entirely (matches prior behavior) if DASHBOARD_LOCAL_TOKEN isn't set —
    e.g. someone running scout-backend directly, outside the Dashboard app.
    """

    async def dispatch(self, request, call_next):
        if not config.LOCAL_AUTH_TOKEN:
            return await call_next(request)

        supplied = request.headers.get(LOCAL_TOKEN_HEADER, "")
        if secrets.compare_digest(supplied.encode("utf-8"), config.LOCAL_AUTH_TOKEN.encode("utf-8")):
            return await call_next(request)

        return Response(status_code=401, content="Missing or invalid local service token")
