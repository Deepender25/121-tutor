"""Hosted-mode security: the account and sessions (AuthStore), sign-in throttling, and the API's login, origin,
CORS and header checks. Runs without the embedding model (the app's lifespan isn't started)."""

import importlib
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tutor import auth, config

PASSWORD = "correct horse battery"
FRONTEND = "https://tutor.example.app"


# ---- account + sessions

def _on_disk(tmp_path) -> bytes:
    """Everything SQLite wrote: the database plus its write-ahead log (new rows live there until a checkpoint)."""
    return b"".join(f.read_bytes() for f in sorted(tmp_path.glob("auth.sqlite3*")))


@pytest.fixture
def store(tmp_path):
    s = auth.AuthStore(tmp_path / "auth.sqlite3")
    yield s
    s.close()


def test_one_account_with_a_hashed_password(store, tmp_path):
    user = store.create_user("  Me@Example.COM ", PASSWORD)
    assert user.email == "me@example.com"
    with pytest.raises(auth.AuthError, match="already exists"):
        store.create_user("other@example.com", PASSWORD)
    raw = _on_disk(tmp_path)
    assert PASSWORD.encode() not in raw and b"$argon2id$" in raw


@pytest.mark.parametrize("email, password, message", [
    ("not-an-email", PASSWORD, "valid email"),
    ("me@example.com", "short", "at least"),
    ("me@example.com", "x" * 129, "at most"),
    ("me@example.com", "me@example.com", "can't be your email"),
])
def test_account_rules(store, email, password, message):
    with pytest.raises(auth.AuthError, match=message):
        store.create_user(email, password)


def test_sign_in_checks_the_password_and_never_reveals_which_part_was_wrong(store):
    store.create_user("me@example.com", PASSWORD)
    assert store.authenticate("ME@example.com", PASSWORD).email == "me@example.com"
    assert store.authenticate("me@example.com", "wrong password!") is None
    assert store.authenticate("nobody@example.com", PASSWORD) is None  # same answer for an unknown email
    assert store.authenticate("garbage", PASSWORD) is None
    assert store.authenticate("me@example.com", "x" * 5000) is None


def test_sessions_store_only_a_hash_and_expire(store, tmp_path, monkeypatch):
    user = store.create_user("me@example.com", PASSWORD)
    token = store.create_session(user, "1.2.3.4", "test")
    assert store.session_user(token) == user
    raw = _on_disk(tmp_path)
    assert token.encode() not in raw and auth._token_hash(token).encode() in raw
    assert store.session_user("forged") is None and store.session_user(None) is None

    now = time.time()
    monkeypatch.setattr(auth.time, "time", lambda: now + auth.SESSION_IDLE_S + 1)  # unused for 30 days
    assert store.session_user(token) is None
    monkeypatch.undo()

    token = store.create_session(user)
    for day in range(1, 102, 20):  # used every 20 days, until past the 90-day maximum (day 101)
        monkeypatch.setattr(auth.time, "time", lambda d=day: now + d * 86400)
        result = store.session_user(token)
    assert result is None


def test_password_change_signs_out_every_other_session(store):
    user = store.create_user("me@example.com", PASSWORD)
    keep, other = store.create_session(user), store.create_session(user)
    store.set_password("me@example.com", "a brand new passphrase", keep_session=keep)
    assert store.session_user(keep) == user and store.session_user(other) is None
    assert store.authenticate("me@example.com", PASSWORD) is None
    assert store.authenticate("me@example.com", "a brand new passphrase") == user


def test_deleting_the_account_ends_its_sessions(store):
    user = store.create_user("me@example.com", PASSWORD)
    token = store.create_session(user)
    assert store.delete_user("me@example.com")
    assert store.session_user(token) is None and store.users() == []


def test_throttle_locks_an_email_after_repeated_failures():
    clock = [0.0]
    t = auth.LoginThrottle(window_s=900, per_email=5, per_ip=20, clock=lambda: clock[0])
    for _ in range(5):
        assert t.retry_after("1.1.1.1", "me@example.com") == 0
        t.failed("1.1.1.1", "me@example.com")
    assert t.retry_after("9.9.9.9", "ME@example.com") > 0  # from any IP
    assert t.retry_after("1.1.1.1", "other@example.com") == 0  # the IP alone isn't over its limit
    clock[0] = 901
    assert t.retry_after("1.1.1.1", "me@example.com") == 0


def test_throttle_limits_one_ip_trying_many_emails():
    t = auth.LoginThrottle(per_email=5, per_ip=20)
    for i in range(20):
        t.failed("6.6.6.6", f"user{i}@example.com")
    assert t.retry_after("6.6.6.6", "fresh@example.com") > 0


# ---- startup safety

@pytest.mark.parametrize("auth_on, origins, host, ok", [
    (False, [], "127.0.0.1", True),  # local, as today
    (False, [FRONTEND], "127.0.0.1", False),  # hosted without login
    (False, [], "0.0.0.0", False),  # reachable on the network without login
    (True, [FRONTEND], "127.0.0.1", True),
])
def test_the_api_refuses_to_run_open_to_others(monkeypatch, auth_on, origins, host, ok):
    monkeypatch.setattr(config, "AUTH_ENABLED", auth_on)
    monkeypatch.setattr(config, "PUBLIC_ORIGINS", origins)
    monkeypatch.setattr(config, "HOST", host)
    assert (config.hosting_error() is None) == ok


def test_a_typo_in_the_login_setting_keeps_login_on(monkeypatch):
    monkeypatch.setenv("VOICE_TUTOR_AUTH", "yes please")
    reloaded = importlib.reload(config)
    try:
        assert reloaded.AUTH_ENABLED is True and reloaded.hosting_error()
    finally:
        monkeypatch.delenv("VOICE_TUTOR_AUTH")
        importlib.reload(config)


# ---- the API: login, origins, CORS, headers

@pytest.fixture
def hosted(tmp_path, monkeypatch):
    """The API as hosted: login on, one frontend origin. The settings are read per request, so the same app works."""
    import tutor.server.app as module

    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    monkeypatch.setattr(config, "PUBLIC_ORIGINS", [FRONTEND])
    monkeypatch.setattr(config, "COOKIE_SECURE", False)  # the test client talks plain http
    monkeypatch.setattr(config, "SESSION_COOKIE", "vt_session")
    store = auth.AuthStore(tmp_path / "auth.sqlite3")
    store.create_user("me@example.com", PASSWORD)
    monkeypatch.setattr(module.state, "auth", store)
    monkeypatch.setattr(module.auth_routes, "throttle", auth.LoginThrottle())
    client = TestClient(module.app)  # no `with`: the lifespan (embedding model) isn't started
    yield client, module
    store.close()


def _login(client, password=PASSWORD, origin=FRONTEND):
    return client.post("/api/auth/login", json={"email": "me@example.com", "password": password},
                       headers={"Origin": origin})


def test_everything_needs_a_session_except_signing_in(hosted):
    client, _ = hosted
    for path in ("/api/notebooks", "/api/status", "/api/docs/abc/outline", "/api/docs/abc/page/1.png"):
        assert client.get(path).status_code == 401, path
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/auth/me").status_code == 401

    r = _login(client)
    assert r.status_code == 200 and r.json() == {"auth": "on", "email": "me@example.com"}
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Max-Age=7776000" in cookie
    assert client.get("/api/notebooks").status_code == 200
    assert client.get("/api/auth/me").json()["email"] == "me@example.com"

    assert client.post("/api/auth/logout", headers={"Origin": FRONTEND}).status_code == 200
    assert client.get("/api/notebooks").status_code == 401


def test_wrong_password_is_generic_and_throttled(hosted):
    client, _ = hosted
    for _ in range(5):
        r = _login(client, "wrong password!!")
        assert r.status_code == 401 and r.json()["detail"] == "Email or password is incorrect."
    r = _login(client)  # even the right password waits now
    assert r.status_code == 429 and "Try again in" in r.json()["detail"] and "retry-after" in r.headers


def test_sign_out_everywhere_ends_all_sessions(hosted):
    client, module = hosted
    _login(client)
    other = TestClient(module.app)
    _login(other)
    assert client.post("/api/auth/logout-all", headers={"Origin": FRONTEND}).status_code == 200
    assert client.get("/api/notebooks").status_code == 401
    assert other.get("/api/notebooks").status_code == 401


def test_other_sites_cant_act_with_your_session(hosted):
    client, _ = hosted
    _login(client)
    evil = {"Origin": "https://evil.example"}
    assert client.post("/api/notebooks", json={"name": "x"}, headers=evil).status_code == 403
    assert client.delete("/api/notebooks/abc", headers=evil).status_code == 403
    assert _login(client, origin="https://evil.example").status_code == 403
    created = client.post("/api/notebooks", json={"name": "From the frontend"}, headers={"Origin": FRONTEND})
    assert created.status_code == 201
    assert client.delete(f"/api/notebooks/{created.json()['id']}", headers={"Origin": FRONTEND}).status_code == 204


def test_cors_lets_only_the_frontend_read_responses(hosted):
    client, _ = hosted
    pre = client.options("/api/notebooks", headers={"Origin": FRONTEND, "Access-Control-Request-Method": "POST",
                                                    "Access-Control-Request-Headers": "content-type"})
    assert pre.status_code == 200
    assert pre.headers["access-control-allow-origin"] == FRONTEND
    assert pre.headers["access-control-allow-credentials"] == "true"
    denied = client.get("/api/notebooks", headers={"Origin": FRONTEND})  # signed out: 401, still labelled
    assert denied.status_code == 401 and denied.headers["access-control-allow-origin"] == FRONTEND
    evil = client.options("/api/notebooks", headers={"Origin": "https://evil.example",
                                                     "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in evil.headers


def test_voice_connection_needs_a_session_and_the_right_origin(hosted):
    client, module = hosted
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/notebooks/nope/live", headers={"Origin": FRONTEND}):
            pass
    _login(client)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/notebooks/nope/live", headers={"Origin": "https://evil.example"}):
            pass
    with client.websocket_connect("/api/notebooks/nope/live", headers={"Origin": FRONTEND}) as ws:
        msg = ws.receive_json()  # past the checks: the session itself answers
    assert msg["type"] == "error" and "no longer exists" in msg["message"]

    module.state.live_sessions = module.config.MAX_LIVE_SESSIONS  # all voice slots taken
    with client.websocket_connect("/api/notebooks/nope/live", headers={"Origin": FRONTEND}) as ws:
        msg = ws.receive_json()
    module.state.live_sessions = 0
    assert "already open" in msg["message"]


def test_security_headers(hosted):
    client, _ = hosted
    r = client.get("/api/health")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
    assert _login(client).headers["cache-control"] == "no-store"


def test_cookie_is_scoped_to_the_frontend_domain_and_secure_when_hosted(hosted, monkeypatch):
    client, _ = hosted
    monkeypatch.setattr(config, "COOKIE_DOMAIN", "tutor.example.app")
    monkeypatch.setattr(config, "COOKIE_SECURE", True)
    monkeypatch.setattr(config, "SESSION_COOKIE", "__Secure-vt_session")
    cookie = _login(client).headers["set-cookie"]
    assert cookie.startswith("__Secure-vt_session=")
    assert "Domain=tutor.example.app" in cookie and "Secure" in cookie and "HttpOnly" in cookie


# ---- local use is unchanged

def test_local_api_needs_no_login_but_still_refuses_other_sites():
    from tutor.server.app import app

    client = TestClient(app)
    assert config.AUTH_ENABLED is False
    assert client.get("/api/auth/me").json() == {"auth": "off"}
    assert client.get("/api/notebooks").status_code == 200
    local_ui = {"Origin": f"http://127.0.0.1:{config.PORT}"}
    r = client.post("/api/notebooks", json={"name": "Local"}, headers=local_ui)
    assert r.status_code == 201
    assert client.delete(f"/api/notebooks/{r.json()['id']}", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.delete(f"/api/notebooks/{r.json()['id']}", headers=local_ui).status_code == 204
