import os
import sys
import tempfile
from pathlib import Path

import pytest

# Test settings must be in place before the app is imported.
_tmp = tempfile.mkdtemp(prefix="f1sim-test-")
# Set TEST_DATABASE_URL to run the suite against Postgres instead of a temp SQLite file.
os.environ["DATABASE_URL"] = os.getenv("TEST_DATABASE_URL", f"sqlite:///{_tmp}/test.db")
os.environ["APP_ENV"] = "dev"
os.environ["SECRET_KEY"] = "test-secret-key-not-for-production-0123456789"
os.environ["LOGIN_ATTEMPTS_PER_MIN"] = "1000"
os.environ["HTTP_RATE_PER_MIN"] = "100000"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from server.db import RaceRecord, SQLModel, User, engine  # noqa: E402
from server.main import app  # noqa: E402

ORIGIN = {"origin": "http://localhost:8000"}
PASSWORD = "correct-horse-battery"


@pytest.fixture(autouse=True)
def fresh_db():
    SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)
    from server import routes
    routes._lockout.failures.clear()          # each test starts with no failed-login history
    routes._lockout.locked_until.clear()
    yield


def make_client(email: str, password: str = PASSWORD) -> TestClient:
    c = TestClient(app)
    r = c.post("/api/auth/signup", json={"email": email, "password": password})
    assert r.status_code == 201, r.text
    return c


@pytest.fixture()
def admin():
    return make_client("admin@example.com")      # first account becomes admin


@pytest.fixture()
def alice(admin):
    return make_client("alice@example.com")


@pytest.fixture()
def bob(admin):
    return make_client("bob@example.com")


@pytest.fixture()
def client(alice):
    return alice
