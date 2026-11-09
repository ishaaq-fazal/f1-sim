"""Accounts, roles, and per-user data isolation."""
import json
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select
from starlette.websockets import WebSocketDisconnect

from server import repo
from server.config import settings
from server.db import RaceRecord, User, engine
from server.main import RaceSession, app
from sim import RaceConfig, RaceEngine
from tests.conftest import ORIGIN, PASSWORD, make_client

SECRET_FIELDS = ("password", "hash", "token_version")


def no_secrets(payload):
    text = json.dumps(payload).lower()
    return not any(f in text for f in SECRET_FIELDS) and "$argon2" not in text


def add_race(email: str, **kw) -> int:
    with Session(engine) as s:
        user = repo.user_by_email(s, email)
        rec = repo.save_race(s, user, team="aurora", laps=10, seed=1, best_position=3,
                             winner="Kai Marlow", summary="MAR P3", **kw)
        return rec.id


# ---------------------------------------------------------------- sign-up / login
def test_signup_returns_only_public_fields_and_first_user_is_admin(admin, alice):
    me = admin.get("/api/me").json()
    assert me["role"] == "admin" and set(me) == {"id", "email", "role"}
    assert alice.get("/api/me").json()["role"] == "principal"
    assert no_secrets(admin.get("/api/admin/users").json())


def test_passwords_are_stored_as_argon2_hashes(alice):
    with Session(engine) as s:
        u = s.exec(select(User).where(User.email == "alice@example.com")).one()
    assert u.password_hash.startswith("$argon2id$") and PASSWORD not in u.password_hash


def test_duplicate_and_weak_signups_rejected(alice):
    c = TestClient(app)
    assert c.post("/api/auth/signup", json={"email": "ALICE@example.com", "password": PASSWORD}).status_code == 400
    assert c.post("/api/auth/signup", json={"email": "new@example.com", "password": "short"}).status_code == 422
    assert c.post("/api/auth/signup", json={"email": "not-an-email", "password": PASSWORD}).status_code == 422
    assert c.post("/api/auth/signup", json={"email": "x@example.com", "password": PASSWORD,
                                            "role": "admin"}).status_code == 422   # can't self-assign a role


def test_login_errors_do_not_reveal_which_part_was_wrong(alice):
    c = TestClient(app)
    wrong_pw = c.post("/api/auth/login", json={"email": "alice@example.com", "password": "nope-nope-nope"})
    no_user = c.post("/api/auth/login", json={"email": "ghost@example.com", "password": "nope-nope-nope"})
    assert wrong_pw.status_code == no_user.status_code == 401
    assert wrong_pw.json() == no_user.json()
    ok = c.post("/api/auth/login", json={"email": "Alice@Example.com", "password": PASSWORD})
    assert ok.status_code == 200 and no_secrets(ok.json())


def test_auth_cookies_are_httponly_and_samesite(alice):
    r = TestClient(app).post("/api/auth/login", json={"email": "alice@example.com", "password": PASSWORD})
    cookies = r.headers.get_list("set-cookie")
    assert len(cookies) == 2
    for c in cookies:
        assert "HttpOnly" in c and "SameSite=strict" in c


# ---------------------------------------------------------------- SQL injection
@pytest.mark.parametrize("email,password", [
    ("' OR '1'='1", "anything"),
    ("alice@example.com' --", "anything"),
    ("alice@example.com", "' OR '1'='1"),
    ("alice@example.com", "x'); DROP TABLE user; --"),
])
def test_sql_injection_in_login_fails_and_tables_survive(alice, email, password):
    r = TestClient(app).post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code in (401, 422)
    with Session(engine) as s:
        assert len(s.exec(select(User)).all()) == 2


@pytest.mark.parametrize("path", ["/api/races/1%20OR%201=1", "/api/races/1;DROP%20TABLE%20raceRecord",
                                  "/api/races/-1%20UNION%20SELECT%20*%20FROM%20user"])
def test_sql_injection_in_path_rejected(alice, path):
    add_race("alice@example.com")
    assert alice.get(path).status_code in (404, 422)
    with Session(engine) as s:
        assert len(s.exec(select(RaceRecord)).all()) == 1


# ---------------------------------------------------------------- tokens
def _forge(claims, key=None, alg="HS256"):
    return jwt.encode(claims, key or settings.secret_key, algorithm=alg)


def test_requests_without_valid_tokens_are_refused(alice):
    anon = TestClient(app)
    assert anon.get("/api/me").status_code == 401
    assert anon.get("/api/races").status_code == 401
    uid = alice.get("/api/me").json()["id"]
    now = datetime.now(timezone.utc)
    good = {"sub": str(uid), "typ": "access", "ver": 0, "iat": now, "exp": now + timedelta(minutes=5)}
    bad_tokens = [
        _forge(good, key="some-other-secret-key-0123456789abcdef"),               # wrong key
        _forge({**good, "exp": now - timedelta(seconds=1)}),                     # expired
        _forge({**good, "typ": "refresh"}),                                      # wrong token type
        _forge({k: v for k, v in good.items() if k != "exp"}),                   # no expiry
        jwt.encode(good, None, algorithm="none") if hasattr(jwt, "encode") else "x",  # alg=none
        _forge(good)[:-3] + "abc",                                               # tampered signature
    ]
    for tok in bad_tokens:
        c = TestClient(app, cookies={"access_token": tok})
        assert c.get("/api/me").status_code == 401, tok


def test_logout_revokes_old_tokens(alice):
    old = dict(alice.cookies)
    assert alice.post("/api/auth/logout").status_code == 204
    replay = TestClient(app, cookies=old)
    assert replay.get("/api/me").status_code == 401
    assert replay.post("/api/auth/refresh").status_code == 401


def test_refresh_issues_new_access_token(alice):
    refresh_only = TestClient(app, cookies={"refresh_token": alice.cookies.get("refresh_token")})
    # the refresh cookie is scoped to /api/auth, and works there
    assert refresh_only.post("/api/auth/refresh").status_code == 200
    assert refresh_only.get("/api/me").status_code == 200


# ---------------------------------------------------------------- data isolation
def test_users_only_see_their_own_races(alice, bob):
    a_id = add_race("alice@example.com")
    b_id = add_race("bob@example.com")
    assert [r["id"] for r in alice.get("/api/races").json()] == [a_id]
    assert [r["id"] for r in bob.get("/api/races").json()] == [b_id]
    assert alice.get(f"/api/races/{a_id}").status_code == 200
    # another user's race looks exactly like a missing one
    assert bob.get(f"/api/races/{a_id}").json() == bob.get("/api/races/999999").json()
    assert bob.get(f"/api/races/{a_id}").status_code == 404
    assert no_secrets(alice.get("/api/races").json())
    r = alice.get(f"/api/races/{a_id}").json()
    assert "owner_id" not in r


def test_users_cannot_delete_others_races(alice, bob):
    a_id = add_race("alice@example.com")
    assert bob.delete(f"/api/races/{a_id}").status_code == 404
    assert alice.get(f"/api/races/{a_id}").status_code == 200
    assert alice.delete(f"/api/races/{a_id}").status_code == 204
    assert alice.get(f"/api/races/{a_id}").status_code == 404


def test_finished_race_is_saved_to_its_owner(alice, bob):
    with Session(engine) as s:
        user = repo.user_by_email(s, "alice@example.com")
        s.expunge(user)
    session = RaceSession(ws=None, user=user)
    session.team, session.seed = "aurora", 11
    session.engine = RaceEngine(RaceConfig(laps=5, seed=11, controlled_team="aurora"))
    while not session.engine.finished:
        session.engine.step(5.0)
    session._save_result()
    races = alice.get("/api/races").json()
    assert len(races) == 1 and races[0]["seed"] == 11 and races[0]["team"] == "aurora"
    assert bob.get("/api/races").json() == []


# ---------------------------------------------------------------- roles
def test_only_admins_manage_users(admin, alice, bob):
    assert alice.get("/api/admin/users").status_code == 403
    bob_id = bob.get("/api/me").json()["id"]
    assert alice.patch(f"/api/admin/users/{bob_id}/role", json={"role": "admin"}).status_code == 403
    me_id = admin.get("/api/me").json()["id"]
    assert admin.patch(f"/api/admin/users/{me_id}/role", json={"role": "viewer"}).status_code == 403
    assert admin.patch(f"/api/admin/users/{bob_id}/role", json={"role": "superuser"}).status_code == 422
    assert admin.patch(f"/api/admin/users/{bob_id}/role", json={"role": "viewer"}).status_code == 200
    # a role change revokes old tokens; after signing in again bob is a viewer
    assert bob.get("/api/me").status_code == 401
    bob.post("/api/auth/login", json={"email": "bob@example.com", "password": PASSWORD})
    assert bob.get("/api/me").json()["role"] == "viewer"


def test_viewers_can_watch_but_not_give_orders(admin, bob):
    bob_id = bob.get("/api/me").json()["id"]
    admin.patch(f"/api/admin/users/{bob_id}/role", json={"role": "viewer"})
    bob.post("/api/auth/login", json={"email": "bob@example.com", "password": PASSWORD})
    with bob.websocket_connect("/ws/race", headers=ORIGIN) as ws:
        ws.send_text(json.dumps({"type": "start", "team": "aurora", "laps": 5, "seed": 1}))
        init = ws.receive_json()
        assert init["can_command"] is False
        state = ws.receive_json()
        mine = next(c for c in state["cars"] if c["controlled"])
        ws.send_text(json.dumps({"type": "pit", "driver": mine["code"], "compound": "H"}))
        for _ in range(50):
            m = ws.receive_json()
            if m["type"] == "error":
                assert "Viewers" in m["detail"]
                break
        else:
            raise AssertionError("no refusal")


def test_websocket_requires_sign_in():
    with pytest.raises(WebSocketDisconnect) as exc:
        with TestClient(app).websocket_connect("/ws/race", headers=ORIGIN) as ws:
            ws.receive_text()
    assert exc.value.code == 4401


# ---------------------------------------------------------------- CSRF
def test_cross_site_writes_blocked(alice):
    a_id = add_race("alice@example.com")
    r = alice.delete(f"/api/races/{a_id}", headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    r = alice.post("/api/auth/login", content="email=a&password=b",
                   headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 415
    assert alice.get(f"/api/races/{a_id}").status_code == 200


def test_saved_race_records_the_circuit(alice):
    with Session(engine) as s:
        user = repo.user_by_email(s, "alice@example.com")
        s.expunge(user)
    session = RaceSession(ws=None, user=user)
    session.team, session.seed = "aurora", 2
    session.engine = RaceEngine(RaceConfig(laps=5, seed=2, controlled_team="aurora", track_id="sandridge"))
    while not session.engine.finished:
        session.engine.step(5.0)
    session._save_result()
    assert alice.get("/api/races").json()[0]["track"] == "sandridge"


def test_old_databases_get_the_track_column(tmp_path):
    """A database made before circuits existed is upgraded in place, keeping its races."""
    from sqlalchemy import create_engine, inspect, text

    import server.db as db
    old = create_engine(f"sqlite:///{tmp_path}/old.db")
    with old.begin() as c:
        c.execute(text('CREATE TABLE "user" (id INTEGER PRIMARY KEY, email VARCHAR, password_hash VARCHAR, '
                       "role VARCHAR, token_version INTEGER, created_at DATETIME)"))
        c.execute(text("CREATE TABLE racerecord (id INTEGER PRIMARY KEY, owner_id INTEGER, team VARCHAR, laps INTEGER, "
                       "seed INTEGER, best_position INTEGER, winner VARCHAR, summary VARCHAR, created_at DATETIME)"))
        c.execute(text("INSERT INTO racerecord (owner_id, team, laps, summary) VALUES (1, 'aurora', 10, 'x')"))
    real = db.engine
    try:
        db.engine = old
        db._upgrade()
    finally:
        db.engine = real
    assert "track" in {col["name"] for col in inspect(old).get_columns("racerecord")}
    with old.connect() as c:
        assert c.execute(text("SELECT track FROM racerecord")).scalar() == "harbourline"
