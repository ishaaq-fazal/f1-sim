"""Data-access layer. The ONLY place that queries the database.

Rules:
  * Every race query is filtered by owner_id = the acting user. There is no
    function that reads another user's races.
  * Every write checks the actor's role first. Deny by default.
  * Queries are built with SQLModel select() / ORM objects, so values are always
    bound parameters; no SQL is ever assembled from strings.
"""
from __future__ import annotations

from sqlmodel import Session, delete, func, select

from .config import settings
import copy
import secrets

from sim import career as career_rules
from sim.season import ROUNDS, points_for, standings, track_for_round

from .db import Career, RaceRecord, Role, Season, SeasonResult, User


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


# ------------------------------------------------------------------ seasons (always owner-scoped)
MAX_SEASONS_PER_USER = 50


class SeasonError(Exception):
    """A season action that breaks the rules (wrong round, season over, too many seasons)."""


def create_season(s: Session, actor: User, team: str, laps: int, weather: str) -> Season:
    _require(actor, Role.principal, Role.admin)     # viewers watch, they don't run a team
    count = s.exec(select(func.count()).select_from(Season).where(Season.owner_id == actor.id)).one()
    if count >= MAX_SEASONS_PER_USER:
        raise SeasonError("Too many seasons: delete an old one first")
    season = Season(owner_id=actor.id, team=team, laps=laps, weather=weather)
    s.add(season)
    s.commit()
    s.refresh(season)
    return season


def list_seasons(s: Session, actor: User, limit: int = 50) -> list[Season]:
    """Stand-alone seasons only; a career's seasons are reached through the career."""
    q = (select(Season).where(Season.owner_id == actor.id, Season.career_id.is_(None))
         .order_by(Season.id.desc()).limit(limit))
    return list(s.exec(q))


def get_season(s: Session, actor: User, season_id: int) -> Season | None:
    q = select(Season).where(Season.id == season_id, Season.owner_id == actor.id)
    return s.exec(q).first()


def season_results(s: Session, actor: User, season_id: int) -> list[SeasonResult]:
    q = (select(SeasonResult)
         .where(SeasonResult.season_id == season_id, SeasonResult.owner_id == actor.id)
         .order_by(SeasonResult.round, SeasonResult.position))
    return list(s.exec(q))


def record_round(s: Session, actor: User, season_id: int, round_no: int, track: str,
                 seed: int | None, results: list[tuple[str, str, int, bool]]) -> Season:
    """Save one finished round. results = [(driver, team, position, retired), ...]."""
    _require(actor, Role.principal, Role.admin)
    season = get_season(s, actor, season_id)
    if season is None:
        raise SeasonError("Season not found")
    if season.finished or round_no != season.rounds_done + 1 or track != track_for_round(round_no):
        raise SeasonError("That round can't be recorded")
    for driver, team, position, retired in results:
        s.add(SeasonResult(season_id=season.id, owner_id=actor.id, round=round_no, track=track, seed=seed,
                           driver=driver, team=team, position=position, retired=retired,
                           points=points_for(position, retired)))
    season.rounds_done = round_no
    season.finished = round_no >= ROUNDS
    s.add(season)
    if season.career_id is not None:
        career = get_career(s, actor, season.career_id)
        if career is None:
            raise SeasonError("Season not found")
        state = copy.deepcopy(career.state)
        team_points = sum(points_for(p, r) for _, t, p, r in results if t == career.team)
        career_rules.credit_round(state, team_points)
        career.state = state
        s.add(career)
    s.commit()              # results, the round counter and prize money are saved together or not at all
    s.refresh(season)
    return season


def delete_season(s: Session, actor: User, season_id: int) -> bool:
    season = get_season(s, actor, season_id)
    if season is None or season.career_id is not None:   # career seasons go with their career
        return False
    s.exec(delete(SeasonResult).where(SeasonResult.season_id == season.id,
                                      SeasonResult.owner_id == actor.id))
    s.delete(season)
    s.commit()
    return True


# ------------------------------------------------------------------ careers (always owner-scoped)
MAX_CAREERS_PER_USER = 20


def create_career(s: Session, actor: User, team: str, laps: int, weather: str) -> Career:
    _require(actor, Role.principal, Role.admin)
    count = s.exec(select(func.count()).select_from(Career).where(Career.owner_id == actor.id)).one()
    if count >= MAX_CAREERS_PER_USER:
        raise SeasonError("Too many careers: delete an old one first")
    try:
        state = career_rules.new_career(team, seed=secrets.randbelow(2**31))
    except career_rules.CareerError as e:
        raise SeasonError(str(e))
    career = Career(owner_id=actor.id, team=team, laps=laps, weather=weather, state=state)
    s.add(career)
    s.flush()
    s.add(Season(owner_id=actor.id, career_id=career.id, team=team, laps=laps, weather=weather))
    s.commit()
    s.refresh(career)
    return career


def list_careers(s: Session, actor: User, limit: int = 20) -> list[Career]:
    q = select(Career).where(Career.owner_id == actor.id).order_by(Career.id.desc()).limit(limit)
    return list(s.exec(q))


def get_career(s: Session, actor: User, career_id: int) -> Career | None:
    q = select(Career).where(Career.id == career_id, Career.owner_id == actor.id)
    return s.exec(q).first()


def career_season(s: Session, actor: User, career: Career) -> Season:
    """The career's current (latest) season."""
    q = (select(Season).where(Season.career_id == career.id, Season.owner_id == actor.id)
         .order_by(Season.id.desc()))
    return s.exec(q).first()


def career_seasons(s: Session, actor: User, career: Career) -> list[Season]:
    q = (select(Season).where(Season.career_id == career.id, Season.owner_id == actor.id)
         .order_by(Season.id))
    return list(s.exec(q))


def career_action(s: Session, actor: User, career_id: int, action: str, **kw) -> Career:
    """Apply one rule from sim/career.py to a career the actor owns, and save it."""
    _require(actor, Role.principal, Role.admin)
    career = get_career(s, actor, career_id)
    if career is None:
        raise SeasonError("Career not found")
    season = career_season(s, actor, career)
    state = copy.deepcopy(career.state)
    try:
        if action == "upgrade":
            career_rules.buy_upgrade(state, kw["area"])
        elif action == "sign":
            career_rules.sign_driver(state, kw["code"], kw["replace"], kw["years"], season.rounds_done)
        elif action == "renew":
            career_rules.renew_driver(state, kw["code"], kw["years"], season.rounds_done)
        elif action == "next_season":
            if not season.finished:
                raise career_rules.CareerError("Finish this season's 8 rounds first")
            _, teams = standings(season_results(s, actor, season.id))
            drivers, _ = standings(season_results(s, actor, season.id))
            mine = next((i for i, t in enumerate(teams, 1) if t.key == career.team), len(teams))
            pts = next((t.points for t in teams if t.key == career.team), 0)
            career_rules.end_season(state, mine, pts, drivers[0].key if drivers else "",
                                    seed=state["seed"] + state["season_no"])
            s.add(Season(owner_id=actor.id, career_id=career.id, team=career.team,
                         laps=career.laps, weather=career.weather))
        else:
            raise career_rules.CareerError("Unknown action")
    except career_rules.CareerError as e:
        s.rollback()
        raise SeasonError(str(e))
    career.state = state
    s.add(career)
    s.commit()
    s.refresh(career)
    return career


def delete_career(s: Session, actor: User, career_id: int) -> bool:
    career = get_career(s, actor, career_id)
    if career is None:
        return False
    for season in career_seasons(s, actor, career):
        s.exec(delete(SeasonResult).where(SeasonResult.season_id == season.id,
                                          SeasonResult.owner_id == actor.id))
        s.delete(season)
    s.flush()
    s.delete(career)
    s.commit()
    return True
