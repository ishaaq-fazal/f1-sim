"""REST routes for accounts, race history and admin. All data access goes through repo."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from . import repo
from .auth import (REFRESH_COOKIE, clear_auth_cookies, current_user, hash_password, require_role,
                   set_auth_cookies, user_from_token, verify_password)
from .config import settings
from .db import Role, User, get_session
from .schemas import LoginIn, RaceOut, RoleIn, SignupIn, UserOut
from .security import SlidingWindow

router = APIRouter(prefix="/api")
_login_ip = SlidingWindow(settings.login_attempts_per_min * 4, 60.0)
_login_email = SlidingWindow(settings.login_attempts_per_min, 60.0)


def _out(u: User) -> UserOut:
    return UserOut(id=u.id, email=u.email, role=u.role.value)


@router.post("/auth/signup", response_model=UserOut, status_code=201)
def signup(body: SignupIn, response: Response, request: Request, s: Session = Depends(get_session)):
    ip = request.client.host if request.client else "unknown"
    if not _login_ip.allow(ip):
        raise HTTPException(429, "Too many attempts, try again in a minute")
    try:
        user = repo.create_user(s, body.email, hash_password(body.password))
    except IntegrityError:
        s.rollback()
        # Same wording whether or not the address exists elsewhere would be ideal; keep it generic.
        raise HTTPException(400, "Could not create account")
    set_auth_cookies(response, user)
    return _out(user)


@router.post("/auth/login", response_model=UserOut)
def login(body: LoginIn, response: Response, request: Request, s: Session = Depends(get_session)):
    ip = request.client.host if request.client else "unknown"
    if not _login_ip.allow(ip) or not _login_email.allow(body.email):
        raise HTTPException(429, "Too many attempts, try again in a minute")
    user = repo.user_by_email(s, body.email)
    if not verify_password(body.password, user.password_hash if user else None):
        raise HTTPException(401, "Wrong email or password")
    set_auth_cookies(response, user)
    return _out(user)


@router.post("/auth/refresh", response_model=UserOut)
def refresh(request: Request, response: Response, s: Session = Depends(get_session)):
    user = user_from_token(s, request.cookies.get(REFRESH_COOKIE), "refresh")
    if user is None:
        clear_auth_cookies(response)
        raise HTTPException(401, "Not signed in")
    set_auth_cookies(response, user)
    return _out(user)


@router.post("/auth/logout", status_code=204)
def logout(response: Response, user: User = Depends(current_user), s: Session = Depends(get_session)):
    repo.revoke_tokens(s, user)       # every refresh/access token issued so far stops working
    clear_auth_cookies(response)


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(current_user)):
    return _out(user)


@router.get("/races", response_model=list[RaceOut])
def my_races(user: User = Depends(current_user), s: Session = Depends(get_session)):
    return repo.list_races(s, user)


@router.get("/races/{race_id}", response_model=RaceOut)
def my_race(race_id: int, user: User = Depends(current_user), s: Session = Depends(get_session)):
    rec = repo.get_race(s, user, race_id)
    if rec is None:                    # 404 for "not yours" too, so ids don't leak
        raise HTTPException(404, "Not found")
    return rec


@router.delete("/races/{race_id}", status_code=204)
def delete_my_race(race_id: int, user: User = Depends(current_user), s: Session = Depends(get_session)):
    if not repo.delete_race(s, user, race_id):
        raise HTTPException(404, "Not found")


@router.get("/admin/users", response_model=list[UserOut])
def admin_users(admin: User = Depends(require_role(Role.admin)), s: Session = Depends(get_session)):
    return [_out(u) for u in repo.list_users(s, admin)]


@router.patch("/admin/users/{user_id}/role", response_model=UserOut)
def admin_set_role(user_id: int, body: RoleIn, admin: User = Depends(require_role(Role.admin)),
                   s: Session = Depends(get_session)):
    try:
        user = repo.set_role(s, admin, user_id, Role(body.role))
    except repo.PermissionDenied:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not allowed")
    if user is None:
        raise HTTPException(404, "Not found")
    return _out(user)
