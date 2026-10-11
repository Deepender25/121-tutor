"""Request security for the API: one ASGI layer in front of every HTTP request and the voice WebSocket.

1. Origin check (always on). A browser names the site a request comes from; requests that change something
   (POST/PUT/PATCH/DELETE) and WebSocket connections are refused unless that site is the app itself (local UI)
   or a configured frontend (VOICE_TUTOR_PUBLIC_ORIGINS). This stops other websites from acting with your
   session (CSRF), including the local, login-free setup. Requests without an Origin (curl, server-side
   rendering, tests) don't come from another site and pass to the login check.
2. Login (VOICE_TUTOR_AUTH=on). Every /api route needs a valid session cookie, except the health check and the
   sign-in endpoints; the WebSocket is checked before it's accepted.
3. Headers: nosniff, no framing, a strict referrer policy, and no caching of sign-in responses.
CORS (which sites may read responses) is ConfiguredCORS below, added outside this layer in app.py.
"""

import json
import logging

from starlette.datastructures import Headers
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import cookie_parser

from tutor import config

log = logging.getLogger("security")

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
OPEN_PATHS = {"/api/health", "/api/auth/login", "/api/auth/logout", "/api/auth/me"}  # these check for themselves
_HEADERS = [(b"x-content-type-options", b"nosniff"), (b"x-frame-options", b"DENY"),
            (b"referrer-policy", b"strict-origin-when-cross-origin")]


class ConfiguredCORS(CORSMiddleware):
    """Starlette's CORS, allowing exactly the configured frontends (VOICE_TUTOR_PUBLIC_ORIGINS), read per request.
    With none configured (local use), no response is labelled for another site and preflights are refused."""

    def is_allowed_origin(self, origin: str) -> bool:
        return origin in config.PUBLIC_ORIGINS


def allowed_origins() -> set[str]:
    local = {f"http://{h}:{p}" for h in ("127.0.0.1", "localhost") for p in (config.PORT, config.API_PORT)}
    return local | {config.UI_URL} | set(config.PUBLIC_ORIGINS)


def client_ip(scope) -> str:
    client = scope.get("client")
    return client[0] if client else ""  # uvicorn applies X-Forwarded-For from the local reverse proxy (Caddy)


class SecurityMiddleware:
    def __init__(self, app, auth_store_getter):
        self.app = app
        self._auth = auth_store_getter  # () -> AuthStore | None, resolved per request (created at startup)

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        path = scope.get("path", "")
        method = scope.get("method", "GET")

        origin = headers.get("origin")
        if origin and (kind == "websocket" or method in UNSAFE_METHODS) and origin.rstrip("/") not in allowed_origins():
            log.warning("Refused %s %s from origin %s (%s)", method if kind == "http" else "WebSocket", path,
                        origin[:100], client_ip(scope))
            return await _deny(scope, receive, send, 403, "Requests from this site aren't allowed.")

        if config.AUTH_ENABLED and path.startswith("/api/") and path not in OPEN_PATHS and method != "OPTIONS":
            store = self._auth()
            token = cookie_parser(headers.get("cookie", "")).get(config.SESSION_COOKIE)
            user = store.session_user(token) if store else None
            if user is None:
                return await _deny(scope, receive, send, 401, "Sign in to continue.")
            scope.setdefault("state", {})["user"] = user

        if kind == "websocket":
            return await self.app(scope, receive, send)

        no_store = path.startswith("/api/auth/")

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                present = {k.lower() for k, _ in message.get("headers", [])}
                extra = [(k, v) for k, v in _HEADERS if k not in present]
                if no_store:
                    extra.append((b"cache-control", b"no-store"))
                message = {**message, "headers": [*message.get("headers", []), *extra]}
            await send(message)

        await self.app(scope, receive, send_with_headers)


async def _deny(scope, receive, send, status: int, detail: str) -> None:
    if scope["type"] == "websocket":
        await receive()  # websocket.connect
        await send({"type": "websocket.close", "code": 4401 if status == 401 else 4403, "reason": detail})
        return
    body = json.dumps({"detail": detail}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                            *_HEADERS, (b"cache-control", b"no-store")]})
    await send({"type": "http.response.body", "body": body})
