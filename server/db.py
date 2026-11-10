"""Database tables. Only server/repo.py should query these."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import UniqueConstraint, event, inspect, text
from sqlmodel import Field, Session, SQLModel, create_engine

from .config import ROOT, settings


class Role(str, Enum):
    viewer = "viewer"          # can watch races, cannot give orders
    principal = "principal"    # can run races and command their own team
    admin = "admin"            # can also manage users' roles


def _now() -> datetime:
    return datetime.now(timezone.utc)


class User(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True, max_length=254)
    password_hash: str
    role: Role = Field(default=Role.principal)
    token_version: int = 0      # bumped on logout/role change to revoke refresh tokens
    created_at: datetime = Field(default_factory=_now)


class RaceRecord(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    owner_id: int = Field(foreign_key="user.id", index=True)
    team: str = Field(max_length=20)
    track: str = Field(default="harbourline", max_length=20)
    laps: int
    seed: int | None = None
    best_position: int | None = None
    winner: str | None = Field(default=None, max_length=64)
    summary: str = Field(default="", max_length=400)
    created_at: datetime = Field(default_factory=_now)


class Season(SQLModel, table=True):
    """An 8-round championship run by one user with one team."""
    id: int | None = Field(default=None, primary_key=True)
    owner_id: int = Field(foreign_key="user.id", index=True)
    team: str = Field(max_length=20)
    laps: int
    weather: str = Field(default="dry", max_length=12)
    rounds_done: int = 0
    finished: bool = False
    created_at: datetime = Field(default_factory=_now)


class SeasonResult(SQLModel, table=True):
    """One car's result in one round. owner_id is repeated here so every query can be owner-scoped."""
    __table_args__ = (UniqueConstraint("season_id", "round", "driver"),)
    id: int | None = Field(default=None, primary_key=True)
    season_id: int = Field(foreign_key="season.id", index=True)
    owner_id: int = Field(foreign_key="user.id", index=True)
    round: int
    track: str = Field(max_length=20)
    seed: int | None = None
    driver: str = Field(max_length=3)
    team: str = Field(max_length=20)
    position: int
    retired: bool = False
    points: int = 0


def _make_engine(url: str):
    if url.startswith("sqlite:///"):
        (ROOT / "data").mkdir(exist_ok=True)
    if url.startswith("sqlite"):
        eng = create_engine(url, connect_args={"check_same_thread": False})
    else:
        # pool_pre_ping drops connections the database closed while idle
        eng = create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)
    if url.startswith("sqlite"):
        @event.listens_for(eng, "connect")
        def _fk_on(dbapi_conn, _):
            dbapi_conn.execute("PRAGMA foreign_keys=ON")
    return eng


engine = _make_engine(settings.database_url)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    _upgrade()


def _upgrade() -> None:
    """Small in-place upgrades for databases created by earlier versions.
    (Switch to Alembic migrations before making bigger schema changes.)"""
    cols = {c["name"] for c in inspect(engine).get_columns("racerecord")}
    if "track" not in cols:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE racerecord ADD COLUMN track VARCHAR(20) NOT NULL DEFAULT 'harbourline'"))


def ping() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def get_session():
    with Session(engine) as session:
        yield session
