"""Login checks: lockout, password rules, unique emails, accounts that persist, cookie flags."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from server import routes
from server.config import settings
from server.db import User, engine
from server.main import app
from server.security import LoginLockout
from tests.conftest import PASSWORD, make_client


def login(c, email, password):
    return c.post("/api/auth/login", json={"email": email, "password": password})


# ---------------------------------------------------------------- accounts persist, emails are unique
def test_account_is_remembered_and_can_log_in_from_a_new_browser(alice):
    fresh = TestClient(app)                          # no cookies: like a different device
    assert fresh.get("/api/me").status_code == 401
    r = login(fresh, "alice@example.com", PASSWORD)
    assert r.status_code == 200 and r.json()["email"] == "alice@example.com"
    assert fresh.get("/api/me").status_code == 200


@pytest.mark.parametrize("email", ["alice@example.com", "ALICE@example.com", "Alice@Example.COM"])
def test_same_email_cannot_sign_up_twice_in_any_case(alice, email):
    r = TestClient(app).post("/api/auth/signup", json={"email": email, "password": "another-good-pass-42"})
    assert r.status_code == 400 and r.json() == {"detail": "Could not create account"}
    with Session(engine) as s:
        assert len(s.exec(select(User).where(User.email == "alice@example.com")).all()) == 1
    # and the original password still works: the failed signup changed nothing
    assert login(TestClient(app), "alice@example.com", PASSWORD).status_code == 200


def test_database_itself_refuses_duplicate_emails(alice):
    with Session(engine) as s, pytest.raises(IntegrityError):
        s.add(User(email="alice@example.com", password_hash="x"))
        s.commit()


# ---------------------------------------------------------------- brute force
def test_ten_wrong_passwords_lock_the_account_even_for_the_right_password(alice):
    c = TestClient(app)
    for _ in range(10):
        assert login(c, "alice@example.com", "wrong-password-123").status_code == 401
    locked = login(c, "alice@example.com", PASSWORD)
    assert locked.status_code == 429 and locked.json() == {"detail": "Too many attempts, try again later"}
    # other accounts are not affected
    make_client("bob@example.com")
    assert login(TestClient(app), "bob@example.com", PASSWORD).status_code == 200


def test_lockout_looks_the_same_for_emails_that_do_not_exist(alice):
    c = TestClient(app)
    for email in ("alice@example.com", "ghost@example.com"):
        for _ in range(10):
            login(c, email, "wrong-password-123")
    a, g = login(c, "alice@example.com", "x" * 12), login(c, "ghost@example.com", "x" * 12)
    assert a.status_code == g.status_code == 429 and a.json() == g.json()


def test_lockout_cannot_be_dodged_with_email_capitals(alice):
    c = TestClient(app)
    for i in range(10):
        login(c, "ALICE@example.com" if i % 2 else "alice@EXAMPLE.com", "wrong-password-123")
    assert login(c, "alice@example.com", PASSWORD).status_code == 429


def test_successful_login_resets_the_failure_count(alice):
    c = TestClient(app)
    for _ in range(9):
        login(c, "alice@example.com", "wrong-password-123")
    assert login(c, "alice@example.com", PASSWORD).status_code == 200
    for _ in range(9):
        login(c, "alice@example.com", "wrong-password-123")
    assert login(c, "alice@example.com", PASSWORD).status_code == 200


def test_lockout_expires(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("server.security.time.monotonic", lambda: clock[0])
    lock = LoginLockout(max_failures=3, window=60, lock_s=120)
    for _ in range(3):
        lock.failed("a@x.com")
    assert lock.is_locked("a@x.com")
    clock[0] += 119
    assert lock.is_locked("a@x.com")
    clock[0] += 2
    assert not lock.is_locked("a@x.com")


def test_lockout_memory_is_bounded():
    lock = LoginLockout(max_failures=10, window=0.0)
    for i in range(12_000):
        lock.failed(f"user{i}@spray.example")
    assert len(lock.failures) <= 10_001


# ---------------------------------------------------------------- password rules
@pytest.mark.parametrize("password,reason", [
    ("short", None),
    ("password123", "too common"),
    ("QWERTYUIOP", "too common"),
    ("aaaaaaaaaaaa", "variety"),
    ("ababababab12", None),          # 4 distinct characters: allowed
    ("charlie-rocks-99", "email"),
])
def test_weak_passwords_are_refused_at_signup(admin, password, reason):
    r = TestClient(app).post("/api/auth/signup", json={"email": "charlie@example.com", "password": password})
    if password == "ababababab12":
        assert r.status_code == 201
        return
    assert r.status_code == 422
    if reason:
        assert reason in str(r.json()["detail"])


def test_login_password_is_capped_so_huge_inputs_cannot_tie_up_the_server(alice):
    assert login(TestClient(app), "alice@example.com", "x" * 10_000).status_code == 422


# ---------------------------------------------------------------- cookies and sessions
def test_cookies_are_secure_in_production(alice):
    old = settings.env
    object.__setattr__(settings, "env", "prod")
    try:
        r = login(TestClient(app), "alice@example.com", PASSWORD)
    finally:
        object.__setattr__(settings, "env", old)
    for c in r.headers.get_list("set-cookie"):
        assert "Secure" in c and "HttpOnly" in c and "SameSite=strict" in c


def test_refresh_cookie_is_only_sent_to_the_auth_endpoints(alice):
    r = login(TestClient(app), "alice@example.com", PASSWORD)
    refresh = next(c for c in r.headers.get_list("set-cookie") if c.startswith("refresh_token="))
    assert "Path=/api/auth" in refresh


def test_logout_ends_every_session_of_that_user(alice):
    other_device = TestClient(app)
    login(other_device, "alice@example.com", PASSWORD)
    assert alice.post("/api/auth/logout").status_code == 204
    assert other_device.get("/api/me").status_code == 401
    assert other_device.post("/api/auth/refresh").status_code == 401
    assert login(TestClient(app), "alice@example.com", PASSWORD).status_code == 200   # can sign in again
