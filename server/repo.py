"""Data-access layer. The ONLY place that queries the database.

Rules:
  * Every race query is filtered by owner_id = the acting user. There is no
    function that reads another user's races.
  * Every write checks the actor's role first. Deny by default.
  * Queries are built with SQLModel select() / ORM objects, so values are always
    bound parameters; no SQL is ever assembled from strings.
"""
from __future__ import annotations

from sqlmodel import Session, func, select

from .config import settings
from .db import RaceRecord, Role, User


class PermissionDenied(Exception):
    pass


def _require(actor: User, *roles: Role) -> None:
    if actor.role not in roles:
        raise PermissionDenied()


# ------------------------------------------------------------------ users
def create_user(s: Session, email: str, password_hash: str) -> User:
    first = s.exec(select(func.count()).select_from(User)).one() == 0
    user = User(email=email, password_hash=password_hash,
                role=Role.admin if first else Role(settings.default_role))
    s.add(user)
    s.commit()
    s.refresh(user)
    return user


def user_by_email(s: Session, email: str) -> User | None:
    return s.exec(select(User).where(User.email == email)).first()


def user_by_id(s: Session, user_id: int) -> User | None:
    return s.get(User, user_id)


def revoke_tokens(s: Session, user: User) -> None:
    user.token_version += 1
    s.add(user)
    s.commit()


def list_users(s: Session, actor: User) -> list[User]:
    _require(actor, Role.admin)
    return list(s.exec(select(User).order_by(User.id)))


def set_role(s: Session, actor: User, user_id: int, role: Role) -> User | None:
    _require(actor, Role.admin)
    if user_id == actor.id:
        raise PermissionDenied()          # admins can't demote themselves into a lockout
    user = s.get(User, user_id)
    if user is None:
        return None
    user.role = role
    user.token_version += 1               # force re-login with the new role
    s.add(user)
    s.commit()
    s.refresh(user)
    return user


# ------------------------------------------------------------------ races (always owner-scoped)
def save_race(s: Session, actor: User, **fields) -> RaceRecord:
    rec = RaceRecord(owner_id=actor.id, **fields)
    s.add(rec)
    s.commit()
    s.refresh(rec)
    return rec


def list_races(s: Session, actor: User, limit: int = 50) -> list[RaceRecord]:
    q = (select(RaceRecord).where(RaceRecord.owner_id == actor.id)
         .order_by(RaceRecord.id.desc()).limit(limit))
    return list(s.exec(q))


def get_race(s: Session, actor: User, race_id: int) -> RaceRecord | None:
    q = select(RaceRecord).where(RaceRecord.id == race_id, RaceRecord.owner_id == actor.id)
    return s.exec(q).first()


def delete_race(s: Session, actor: User, race_id: int) -> bool:
    _require(actor, Role.principal, Role.admin, Role.viewer)
    rec = get_race(s, actor, race_id)
    if rec is None:
        return False
    s.delete(rec)
    s.commit()
    return True
