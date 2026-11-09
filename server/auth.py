"""Password hashing, JWT cookies, and FastAPI dependencies for auth + roles."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request, Response, status
from sqlmodel import Session

from . import repo
from .config import settings
from .db import Role, User, get_session

_hasher = PasswordHasher()        # Argon2id with library defaults
_DUMMY_HASH = _hasher.hash("timing-equaliser")
ALGO = "HS256"
ACCESS_COOKIE, REFRESH_COOKIE = "access_token", "refresh_token"


def hash_password(pw: str) -> str:
    return _hasher.hash(pw)


def verify_password(pw: str, hashed: str | None) -> bool:
    try:
        return _hasher.verify(hashed or _DUMMY_HASH, pw) and hashed is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def _token(user: User, kind: str, ttl: timedelta) -> str:
    now = datetime.now(timezone.utc)
    claims = {"sub": str(user.id), "typ": kind, "ver": user.token_version, "iat": now, "exp": now + ttl}
    return jwt.encode(claims, settings.secret_key, algorithm=ALGO)


def set_auth_cookies(resp: Response, user: User) -> None:
    common = dict(httponly=True, secure=settings.cookie_secure, samesite="strict")
    resp.set_cookie(ACCESS_COOKIE, _token(user, "access", timedelta(minutes=settings.access_ttl_min)),
                    max_age=settings.access_ttl_min * 60, path="/", **common)
    resp.set_cookie(REFRESH_COOKIE, _token(user, "refresh", timedelta(days=settings.refresh_ttl_days)),
                    max_age=settings.refresh_ttl_days * 86400, path="/api/auth", **common)


def clear_auth_cookies(resp: Response) -> None:
    resp.delete_cookie(ACCESS_COOKIE, path="/")
    resp.delete_cookie(REFRESH_COOKIE, path="/api/auth")


def user_from_token(s: Session, token: str | None, kind: str) -> User | None:
    if not token:
        return None
    try:
        claims = jwt.decode(token, settings.secret_key, algorithms=[ALGO],
                            options={"require": ["exp", "sub", "typ", "ver"]})
    except jwt.PyJWTError:
        return None
    if claims.get("typ") != kind:
        return None
    try:
        user = repo.user_by_id(s, int(claims["sub"]))
    except (TypeError, ValueError):
        return None
    if user is None or user.token_version != claims.get("ver"):
        return None
    return user


def current_user(request: Request, s: Session = Depends(get_session)) -> User:
    user = user_from_token(s, request.cookies.get(ACCESS_COOKIE), "access")
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in")
    return user


def require_role(*roles: Role):
    def guard(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Not allowed")
        return user
    return guard
