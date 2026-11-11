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
from sim import TEAMS, default_grid
from sim import career as career_rules
from sim.season import POINTS, ROUNDS, SCHEDULE, round_forecast, standings, track_for_round

from .schemas import (JoinLeagueIn, LeagueDetailOut, LeagueIn, LeagueOut, LeagueRowOut, NextRoundOut, CareerIn, CareerOut, ContractOut, DriverStandingOut, RenewIn, SignIn, UpgradeIn, UpgradeOut, LoginIn, RaceOut, RoleIn, RoundOut, SeasonDetailOut, SeasonIn, SeasonOut,
                      SignupIn, TeamStandingOut, UserOut)
from .security import LoginLockout, SlidingWindow

router = APIRouter(prefix="/api")
_login_ip = SlidingWindow(settings.login_attempts_per_min * 4, 60.0)
_login_email = SlidingWindow(settings.login_attempts_per_min, 60.0)
_lockout = LoginLockout(max_failures=10, window=15 * 60, lock_s=15 * 60)
_TOO_MANY = "Too many attempts, try again later"
_league_join = SlidingWindow(10, 60.0)        # per user: stops guessing invite codes


def _out(u: User) -> UserOut:
    return UserOut(id=u.id, email=u.email, role=u.role.value)


@router.post("/auth/signup", response_model=UserOut, status_code=201)
def signup(body: SignupIn, response: Response, request: Request, s: Session = Depends(get_session)):
    ip = request.client.host if request.client else "unknown"
    if not _login_ip.allow(ip):
        raise HTTPException(429, _TOO_MANY)
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
    if _lockout.is_locked(body.email) or not _login_ip.allow(ip) or not _login_email.allow(body.email):
        raise HTTPException(429, _TOO_MANY)
    user = repo.user_by_email(s, body.email)
    if not verify_password(body.password, user.password_hash if user else None):
        _lockout.failed(body.email)           # counted for unknown emails too, so both look the same
        raise HTTPException(401, "Wrong email or password")
    _lockout.succeeded(body.email)
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


# ------------------------------------------------------------------ seasons
_TEAM_NAMES = {t[0]: t[1] for t in TEAMS}
_DRIVER_NAMES = {d.code: d.name for d in default_grid()}


@router.post("/seasons", response_model=SeasonOut, status_code=201)
def new_season(body: SeasonIn, user: User = Depends(current_user), s: Session = Depends(get_session)):
    if body.team not in _TEAM_NAMES:
        raise HTTPException(400, "Unknown team")
    try:
        return repo.create_season(s, user, body.team, body.laps, body.weather)
    except repo.PermissionDenied:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not allowed")
    except repo.SeasonError as e:
        raise HTTPException(400, str(e))


@router.get("/seasons", response_model=list[SeasonOut])
def my_seasons(user: User = Depends(current_user), s: Session = Depends(get_session)):
    return repo.list_seasons(s, user)


@router.get("/seasons/{season_id}", response_model=SeasonDetailOut)
def my_season(season_id: int, user: User = Depends(current_user), s: Session = Depends(get_session)):
    season = repo.get_season(s, user, season_id)
    if season is None:                 # same 404 for "not yours" and "missing"
        raise HTTPException(404, "Not found")
    rows = repo.season_results(s, user, season.id)
    rounds = []
    for n, track in enumerate(SCHEDULE, 1):
        rr = [r for r in rows if r.round == n]
        winner = next((r.driver for r in rr if r.position == 1 and not r.retired), None)
        rounds.append(RoundOut(round=n, track=track, done=bool(rr), winner=winner,
                               your_points=sum(r.points for r in rr if r.team == season.team) if rr else None))
    drivers, teams = standings(rows)
    return SeasonDetailOut(
        season=SeasonOut.model_validate(season, from_attributes=True), points_system=list(POINTS), rounds=rounds,
        drivers=[DriverStandingOut(pos=i, code=d.key, name=_DRIVER_NAMES.get(d.key, d.key), team=d.team,
                                   points=d.points, wins=d.wins, podiums=d.podiums)
                 for i, d in enumerate(drivers, 1)],
        teams=[TeamStandingOut(pos=i, team=t.key, name=_TEAM_NAMES.get(t.key, t.key), points=t.points, wins=t.wins)
               for i, t in enumerate(teams, 1)])


@router.get("/seasons/{season_id}/next", response_model=NextRoundOut)
def next_round(season_id: int, user: User = Depends(current_user), s: Session = Depends(get_session)):
    """The next round's circuit and weather forecast, so starting tyres can be chosen for it."""
    season = repo.get_season(s, user, season_id)
    if season is None:
        raise HTTPException(404, "Not found")
    if season.finished:
        raise HTTPException(400, "That season is over")
    n = season.rounds_done + 1
    track = track_for_round(n)
    return NextRoundOut(round=n, track=track, laps=season.laps,
                        forecast=round_forecast(repo.season_round_seed(season, n), track, season.laps))


@router.delete("/seasons/{season_id}", status_code=204)
def delete_my_season(season_id: int, user: User = Depends(current_user), s: Session = Depends(get_session)):
    if not repo.delete_season(s, user, season_id):
        raise HTTPException(404, "Not found")


# ------------------------------------------------------------------ careers
def _career_out(s: Session, user: User, c) -> CareerOut:
    season = repo.career_season(s, user, c)
    st = c.state
    try:
        career_rules.ready_to_race(st)
        blocker = None
    except career_rules.CareerError as e:
        blocker = str(e)
    if season.finished:
        blocker = "Season over: start the next one"
    ups = [UpgradeOut(area=a, label=label, level=st["upgrades"][a],
                      next_cost=None if st["upgrades"][a] >= career_rules.MAX_LEVEL
                      else career_rules.upgrade_cost(st["upgrades"][a]))
           for a, label in career_rules.AREAS.items()]
    contract = lambda d: ContractOut(**{k: d[k] for k in ("code", "name", "rating", "salary", "years")})
    return CareerOut(id=c.id, team=c.team, laps=c.laps, weather=c.weather, season_no=st["season_no"],
                     season_id=season.id, rounds_done=season.rounds_done, season_finished=season.finished,
                     budget=st["budget"], income=st["income"], spent=st["spent"], upgrades=ups,
                     drivers=[contract(d) for d in st["drivers"]],
                     market=[contract(d) for d in sorted(st["market"], key=lambda d: -d["rating"])],
                     history=st["history"], ready=blocker is None, blocker=blocker, created_at=c.created_at)


def _career_call(s: Session, user: User, career_id: int, action: str, **kw) -> CareerOut:
    try:
        c = repo.career_action(s, user, career_id, action, **kw)
    except repo.PermissionDenied:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not allowed")
    except repo.SeasonError as e:
        if str(e) == "Career not found":
            raise HTTPException(404, "Not found")
        raise HTTPException(400, str(e))
    return _career_out(s, user, c)


@router.post("/careers", response_model=CareerOut, status_code=201)
def new_career(body: CareerIn, user: User = Depends(current_user), s: Session = Depends(get_session)):
    if body.team not in _TEAM_NAMES:
        raise HTTPException(400, "Unknown team")
    try:
        c = repo.create_career(s, user, body.team, body.laps, body.weather)
    except repo.PermissionDenied:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not allowed")
    except repo.SeasonError as e:
        raise HTTPException(400, str(e))
    return _career_out(s, user, c)


@router.get("/careers", response_model=list[CareerOut])
def my_careers(user: User = Depends(current_user), s: Session = Depends(get_session)):
    return [_career_out(s, user, c) for c in repo.list_careers(s, user)]


@router.get("/careers/{career_id}", response_model=CareerOut)
def my_career(career_id: int, user: User = Depends(current_user), s: Session = Depends(get_session)):
    c = repo.get_career(s, user, career_id)
    if c is None:
        raise HTTPException(404, "Not found")
    return _career_out(s, user, c)


@router.post("/careers/{career_id}/upgrade", response_model=CareerOut)
def career_upgrade(career_id: int, body: UpgradeIn, user: User = Depends(current_user),
                   s: Session = Depends(get_session)):
    return _career_call(s, user, career_id, "upgrade", area=body.area)


@router.post("/careers/{career_id}/sign", response_model=CareerOut)
def career_sign(career_id: int, body: SignIn, user: User = Depends(current_user), s: Session = Depends(get_session)):
    return _career_call(s, user, career_id, "sign", code=body.code, replace=body.replace, years=body.years)


@router.post("/careers/{career_id}/renew", response_model=CareerOut)
def career_renew(career_id: int, body: RenewIn, user: User = Depends(current_user), s: Session = Depends(get_session)):
    return _career_call(s, user, career_id, "renew", code=body.code, years=body.years)


@router.post("/careers/{career_id}/next-season", response_model=CareerOut)
def career_next(career_id: int, user: User = Depends(current_user), s: Session = Depends(get_session)):
    return _career_call(s, user, career_id, "next_season")


@router.delete("/careers/{career_id}", status_code=204)
def delete_my_career(career_id: int, user: User = Depends(current_user), s: Session = Depends(get_session)):
    if not repo.delete_career(s, user, career_id):
        raise HTTPException(404, "Not found")


# ------------------------------------------------------------------ leagues
# Members only: a league someone isn't in gets the same 404 as one that doesn't exist.
def _league_out(s: Session, league, me) -> LeagueOut:
    return LeagueOut(id=league.id, name=league.name, code=league.code, laps=league.laps,
                     members=repo.league_size(s, me), is_owner=league.owner_id == me.user_id, your_nickname=me.nickname,
                     your_season_id=me.season_id, created_at=league.created_at)


def _league_call(fn, *args):
    try:
        return fn(*args)
    except repo.PermissionDenied:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not allowed")
    except repo.LeagueError as e:
        raise HTTPException(400, str(e))
    except IntegrityError:            # two people grabbing the same name or team at once
        args[0].rollback()
        raise HTTPException(400, "That name or team was just taken in this league, pick another")


@router.post("/leagues", response_model=LeagueOut, status_code=201)
def new_league(body: LeagueIn, user: User = Depends(current_user), s: Session = Depends(get_session)):
    if body.team not in _TEAM_NAMES:
        raise HTTPException(400, "Unknown team")
    league = _league_call(repo.create_league, s, user, body.name.strip(), body.laps, body.team, body.nickname.strip())
    return _league_out(s, *repo.get_league(s, user, league.id))


@router.post("/leagues/join", response_model=LeagueOut, status_code=201)
def join_league(body: JoinLeagueIn, user: User = Depends(current_user), s: Session = Depends(get_session)):
    if not _league_join.allow(str(user.id)):
        raise HTTPException(429, _TOO_MANY)
    if body.team not in _TEAM_NAMES:
        raise HTTPException(400, "Unknown team")
    league = _league_call(repo.join_league, s, user, body.code, body.team, body.nickname.strip())
    return _league_out(s, *repo.get_league(s, user, league.id))


@router.get("/leagues", response_model=list[LeagueOut])
def my_leagues(user: User = Depends(current_user), s: Session = Depends(get_session)):
    return [_league_out(s, lg, m) for lg, m in repo.list_leagues(s, user)]


@router.get("/leagues/{league_id}", response_model=LeagueDetailOut)
def my_league(league_id: int, user: User = Depends(current_user), s: Session = Depends(get_session)):
    found = repo.get_league(s, user, league_id)
    if found is None:
        raise HTTPException(404, "Not found")
    league, me = found
    owner = league.owner_id == user.id
    rows = repo.league_table(s, user, league_id) or []
    table = [LeagueRowOut(**{k: v for k, v in r.items() if k != "_member_id"},
                          team_name=_TEAM_NAMES.get(r["team"], r["team"]),
                          member_id=r["_member_id"] if owner and not r["you"] else None) for r in rows]
    return LeagueDetailOut(league=_league_out(s, league, me), rounds=ROUNDS, schedule=list(SCHEDULE), table=table)


@router.post("/leagues/{league_id}/leave", status_code=204)
def leave_my_league(league_id: int, user: User = Depends(current_user), s: Session = Depends(get_session)):
    if not repo.leave_league(s, user, league_id):
        raise HTTPException(404, "Not found")


@router.delete("/leagues/{league_id}/members/{member_id}", status_code=204)
def remove_league_member(league_id: int, member_id: int, user: User = Depends(current_user),
                         s: Session = Depends(get_session)):
    if not repo.remove_member(s, user, league_id, member_id):
        raise HTTPException(404, "Not found")


@router.delete("/leagues/{league_id}", status_code=204)
def delete_my_league(league_id: int, user: User = Depends(current_user), s: Session = Depends(get_session)):
    if not repo.delete_league(s, user, league_id):
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
