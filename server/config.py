"""Settings from environment variables (and a local .env file, which is gitignored)."""
from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _list(name: str, default: str) -> list[str]:
    return [v.strip() for v in os.getenv(name, default).split(",") if v.strip()]


@dataclass(frozen=True)
class Settings:
    env: str = os.getenv("APP_ENV", "dev")
    allowed_origins: list[str] = field(default_factory=lambda: _list(
        "ALLOWED_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000"))
    http_rate_per_min: int = int(os.getenv("HTTP_RATE_PER_MIN", "120"))
    ws_msgs_per_sec: int = int(os.getenv("WS_MSGS_PER_SEC", "10"))
    ws_max_per_ip: int = int(os.getenv("WS_MAX_PER_IP", "3"))
    ws_max_msg_bytes: int = 2048
    # Database: kept in data/, outside the web root and gitignored.
    database_url: str = os.getenv("DATABASE_URL", f"sqlite:///{ROOT / 'data' / 'f1sim.db'}")
    # Auth
    secret_key: str = os.getenv("SECRET_KEY", "")
    access_ttl_min: int = int(os.getenv("ACCESS_TTL_MIN", "15"))
    refresh_ttl_days: int = int(os.getenv("REFRESH_TTL_DAYS", "7"))
    default_role: str = os.getenv("DEFAULT_ROLE", "principal")
    login_attempts_per_min: int = int(os.getenv("LOGIN_ATTEMPTS_PER_MIN", "5"))

    @property
    def debug(self) -> bool:
        return self.env == "dev"

    @property
    def cookie_secure(self) -> bool:
        return self.env != "dev"   # HTTPS-only cookies outside local dev


def _normalise_db_url(url: str) -> str:
    # Hosts like Render hand out postgres:// or postgresql:// URLs; use the psycopg 3 driver.
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def _dev_secret() -> str:
    """Local dev without SECRET_KEY: create one once in data/ (gitignored) and reuse it,
    so sign-ins survive restarts."""
    path = ROOT / "data" / ".secret_key"
    if path.exists() and len(path.read_text().strip()) >= 32:
        return path.read_text().strip()
    path.parent.mkdir(exist_ok=True)
    key = secrets.token_urlsafe(48)
    path.write_text(key)
    path.chmod(0o600)
    logging.getLogger("f1sim").warning("SECRET_KEY not set: generated one in data/.secret_key")
    return key


def _load(s: Settings | None = None) -> Settings:
    s = s or Settings()
    object.__setattr__(s, "database_url", _normalise_db_url(s.database_url))
    # Render sets RENDER_EXTERNAL_URL (https://<app>.onrender.com); trust it as an origin.
    external = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    if external and external not in s.allowed_origins:
        s.allowed_origins.append(external)
    if s.env != "dev" and any(o.startswith("http://") and "localhost" not in o and "127.0.0.1" not in o
                             for o in s.allowed_origins):
        raise RuntimeError("Outside dev, ALLOWED_ORIGINS must be https:// URLs")
    if not s.secret_key:
        if s.env != "dev":
            raise RuntimeError("SECRET_KEY must be set outside dev (see .env.example)")
        object.__setattr__(s, "secret_key", _dev_secret())
    if len(s.secret_key) < 32:
        raise RuntimeError("SECRET_KEY must be at least 32 characters")
    if s.default_role not in ("viewer", "principal"):
        raise RuntimeError("DEFAULT_ROLE must be viewer or principal")
    return s


settings = _load()
