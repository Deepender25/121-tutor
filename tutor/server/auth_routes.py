"""Sign-in endpoints (/api/auth/*). With VOICE_TUTOR_AUTH=off (local use) they report that login is off."""

import logging

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from tutor import auth, config
from tutor.server.security import client_ip

log = logging.getLogger("auth")
router = APIRouter()
throttle = auth.LoginThrottle()
_state = None  # AppState, set by app.py (avoids an import cycle)


def bind(state) -> None:
    global _state
    _state = state


class LoginIn(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=1024)


class PasswordIn(BaseModel):
    current: str = Field(max_length=1024)
    new: str = Field(max_length=1024)


def _store() -> auth.AuthStore:
    if not config.AUTH_ENABLED or _state is None or _state.auth is None:
        raise HTTPException(404, "Login is off")
    return _state.auth


def _token(request: Request) -> str | None:
    return request.cookies.get(config.SESSION_COOKIE)


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(config.SESSION_COOKIE, token, max_age=auth.SESSION_MAX_S, path="/", domain=config.COOKIE_DOMAIN,
                        secure=config.COOKIE_SECURE, httponly=True, samesite="lax")


def _clear_cookie(response: Response) -> None:
    response.delete_cookie(config.SESSION_COOKIE, path="/", domain=config.COOKIE_DOMAIN, secure=config.COOKIE_SECURE,
                           httponly=True, samesite="lax")


def _wait_text(seconds: int) -> str:
    minutes = (seconds + 59) // 60
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


@router.get("/api/auth/me")
def me(request: Request):
    if not config.AUTH_ENABLED:
        return {"auth": "off"}
    user = _store().session_user(_token(request))
    if user is None:
        raise HTTPException(401, "Not signed in")
    return {"auth": "on", "email": user.email}


@router.post("/api/auth/login")
def login(body: LoginIn, request: Request, response: Response):
    store = _store()
    ip = client_ip(request.scope)
    wait = throttle.retry_after(ip, body.email)
    if wait:
        log.warning("Sign-in blocked (too many attempts) for %r from %s", body.email[:100], ip)
        raise HTTPException(429, f"Too many attempts. Try again in {_wait_text(wait)}.",
                            headers={"Retry-After": str(wait)})
    user = store.authenticate(body.email, body.password)
    if user is None:
        throttle.failed(ip, body.email)
        log.warning("Failed sign-in for %r from %s", body.email[:100], ip)
        raise HTTPException(401, "Email or password is incorrect.")
    throttle.succeeded(body.email)
    store.purge_expired()
    token = store.create_session(user, ip, request.headers.get("user-agent", ""))
    _set_cookie(response, token)
    log.info("Signed in: %s from %s", user.email, ip)
    return {"auth": "on", "email": user.email}


@router.post("/api/auth/logout")
def logout(request: Request, response: Response):
    if config.AUTH_ENABLED and _state is not None and _state.auth is not None:
        _state.auth.end_session(_token(request))
    _clear_cookie(response)
    return {"ok": True}


@router.post("/api/auth/logout-all")
def logout_all(request: Request, response: Response):
    store = _store()
    user = request.state.user  # set by SecurityMiddleware
    ended = store.end_all_sessions(user)
    _clear_cookie(response)
    log.info("Signed out everywhere: %s (%d session%s)", user.email, ended, "" if ended == 1 else "s")
    return {"ok": True}


@router.post("/api/auth/password")
def change_password(body: PasswordIn, request: Request):
    store = _store()
    user = request.state.user
    ip = client_ip(request.scope)
    wait = throttle.retry_after(ip, user.email)
    if wait:
        raise HTTPException(429, f"Too many attempts. Try again in {_wait_text(wait)}.",
                            headers={"Retry-After": str(wait)})
    if store.authenticate(user.email, body.current) is None:
        throttle.failed(ip, user.email)
        raise HTTPException(400, "Your current password is incorrect.")
    try:
        store.set_password(user.email, body.new, keep_session=_token(request))
    except auth.AuthError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}
