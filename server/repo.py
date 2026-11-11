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
import hashlib
import hmac
import secrets

from sim import career as career_rules
from sim.season import ROUNDS, points_for, standings, track_for_round

from .db import Career, League, LeagueMember, RaceRecord, Role, Season, SeasonResult, User


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


def round_seed(season_id: int, round_no: int) -> int:
    """Seed for one round of one season: fixed (so the forecast matches the race and a round
    can't be re-rolled for better weather) and unguessable without the server's secret."""
    mac = hmac.new(settings.secret_key.encode(), f"round:{season_id}:{round_no}".encode(), hashlib.sha256)
    return int.from_bytes(mac.digest()[:4], "big") % (2**31)


def season_round_seed(season: Season, round_no: int) -> int:
    """Seed for a season's round. League seasons share one seed per round, so every member
    races the same weather and conditions and the points are a fair comparison."""
    if season.league_id is not None:
        mac = hmac.new(settings.secret_key.encode(), f"league:{season.league_id}:{round_no}".encode(),
                       hashlib.sha256)
        return int.from_bytes(mac.digest()[:4], "big") % (2**31)
    return round_seed(season.id, round_no)


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
    entry = s.exec(select(LeagueMember).where(LeagueMember.season_id == season.id,
                                              LeagueMember.user_id == actor.id)).first()
    if entry is not None:
        _leave(s, actor, entry)          # deleting a league season leaves that league
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
                raise career_rules.CareerError(f"Finish this season's {ROUNDS} rounds first")
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


# ------------------------------------------------------------------ leagues (members only)
# A league is visible only to its members. Every read goes through _membership(), which
# returns None for non-members, and routes turn that into the same 404 as a missing league.
# Other members' data is only ever exposed as their nickname, team and season totals.
MAX_LEAGUES_PER_USER = 20
MAX_LEAGUE_MEMBERS = 20
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"     # no 0/O or 1/I, easy to read out


class LeagueError(Exception):
    """A league action that breaks the rules (league full, name taken, too many leagues)."""


def _new_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))


def _membership(s: Session, actor: User, league_id: int) -> LeagueMember | None:
    q = select(LeagueMember).where(LeagueMember.league_id == league_id, LeagueMember.user_id == actor.id)
    return s.exec(q).first()


def _join(s: Session, actor: User, league: League, team: str, nickname: str) -> LeagueMember:
    count = s.exec(select(func.count()).select_from(LeagueMember)
                   .where(LeagueMember.user_id == actor.id)).one()
    if count >= MAX_LEAGUES_PER_USER:
        raise LeagueError("You're in too many leagues: leave one first")
    members = s.exec(select(LeagueMember).where(LeagueMember.league_id == league.id)).all()
    if any(m.user_id == actor.id for m in members):
        raise LeagueError("You're already in this league")
    if len(members) >= MAX_LEAGUE_MEMBERS:
        raise LeagueError("That league is full")
    if any(m.nickname.casefold() == nickname.casefold() for m in members):
        raise LeagueError("That name is taken in this league")
    seasons = s.exec(select(func.count()).select_from(Season).where(Season.owner_id == actor.id)).one()
    if seasons >= MAX_SEASONS_PER_USER:
        raise LeagueError("Too many seasons: delete an old one first")
    season = Season(owner_id=actor.id, league_id=league.id, team=team, laps=league.laps, weather="dry")
    s.add(season)
    s.flush()
    member = LeagueMember(league_id=league.id, user_id=actor.id, season_id=season.id, nickname=nickname)
    s.add(member)
    return member


def create_league(s: Session, actor: User, name: str, laps: int, team: str, nickname: str) -> League:
    """Create a league and join it, with a fresh season for the creator."""
    _require(actor, Role.principal, Role.admin)      # members run a season, so viewers can't
    for _ in range(5):                               # a clash of 8-letter codes is very unlikely
        code = _new_code()
        if s.exec(select(League).where(League.code == code)).first() is None:
            break
    else:
        raise LeagueError("Couldn't make an invite code, try again")
    league = League(owner_id=actor.id, name=name, code=code, laps=laps)
    s.add(league)
    s.flush()
    try:
        _join(s, actor, league, team, nickname)
    except LeagueError:
        s.rollback()
        raise
    s.commit()
    s.refresh(league)
    return league


def join_league(s: Session, actor: User, code: str, team: str, nickname: str) -> League:
    """Join by invite code. An unknown code and a wrong code look the same."""
    _require(actor, Role.principal, Role.admin)
    league = s.exec(select(League).where(League.code == code.upper())).first()
    if league is None:
        raise LeagueError("No league with that code")
    try:
        _join(s, actor, league, team, nickname)
    except LeagueError:
        s.rollback()
        raise
    s.commit()
    s.refresh(league)
    return league


def list_leagues(s: Session, actor: User) -> list[tuple[League, LeagueMember]]:
    q = (select(League, LeagueMember).join(LeagueMember, LeagueMember.league_id == League.id)
         .where(LeagueMember.user_id == actor.id).order_by(League.id.desc()))
    return [(lg, m) for lg, m in s.exec(q)]


def get_league(s: Session, actor: User, league_id: int) -> tuple[League, LeagueMember] | None:
    """The league and the actor's own entry, or None if the actor isn't a member."""
    me = _membership(s, actor, league_id)
    if me is None:
        return None
    return s.get(League, league_id), me


def league_size(s: Session, me: LeagueMember) -> int:
    """Member count for a league, given the asker's own membership row (so only members can ask)."""
    return s.exec(select(func.count()).select_from(LeagueMember)
                  .where(LeagueMember.league_id == me.league_id)).one()


def league_table(s: Session, actor: User, league_id: int) -> list[dict] | None:
    """Leaderboard rows for a league the actor is in: each member's points with their own team
    in their own season. None if the actor isn't a member."""
    if _membership(s, actor, league_id) is None:
        return None
    members = s.exec(select(LeagueMember).where(LeagueMember.league_id == league_id)).all()
    rows = []
    for m in members:
        season = s.exec(select(Season).where(Season.id == m.season_id, Season.owner_id == m.user_id,
                                             Season.league_id == league_id)).first()
        if season is None:
            continue
        results = s.exec(select(SeasonResult).where(SeasonResult.season_id == season.id,
                                                    SeasonResult.owner_id == m.user_id,
                                                    SeasonResult.team == season.team)).all()
        finishes = [r.position for r in results if not r.retired]
        rows.append({"nickname": m.nickname, "team": season.team, "you": m.user_id == actor.id,
                     "owner": m.user_id == s.get(League, league_id).owner_id,
                     "rounds_done": season.rounds_done, "finished": season.finished,
                     "points": sum(r.points for r in results), "wins": finishes.count(1),
                     "podiums": sum(1 for p in finishes if p <= 3),
                     "best_finish": min(finishes) if finishes else None, "_finishes": finishes,
                     "_member_id": m.id})
    # Most points first, then countback on finishes (like F1), then fewer rounds used.
    rows.sort(key=lambda r: (-r["points"], *[-r["_finishes"].count(p) for p in range(1, 21)],
                             r["rounds_done"], r["nickname"].casefold()))
    for i, r in enumerate(rows, 1):
        r["pos"] = i
        r.pop("_finishes")
    return rows


def _unlink(s: Session, member: LeagueMember) -> None:
    season = s.get(Season, member.season_id)
    if season is not None and season.owner_id == member.user_id:
        season.league_id = None              # the season stays in the player's own list
        s.add(season)
    s.delete(member)


def leave_league(s: Session, actor: User, league_id: int) -> bool:
    """Leave a league. If the owner leaves, the longest-standing member takes over; the last
    one out deletes it."""
    me = _membership(s, actor, league_id)
    if me is None:
        return False
    _leave(s, actor, me)
    s.commit()
    return True


def _leave(s: Session, actor: User, me: LeagueMember) -> None:
    league = s.get(League, me.league_id)
    _unlink(s, me)
    s.flush()
    rest = s.exec(select(LeagueMember).where(LeagueMember.league_id == league.id)
                  .order_by(LeagueMember.joined_at, LeagueMember.id)).all()
    if not rest:
        s.delete(league)
    elif league.owner_id == actor.id:
        league.owner_id = rest[0].user_id
        s.add(league)
    s.flush()


def remove_member(s: Session, actor: User, league_id: int, member_id: int) -> bool:
    """The league's owner removes someone else. False if not allowed or no such member
    (the same answer, so non-owners learn nothing)."""
    league = s.get(League, league_id)
    if league is None or league.owner_id != actor.id or _membership(s, actor, league_id) is None:
        return False
    m = s.exec(select(LeagueMember).where(LeagueMember.id == member_id,
                                          LeagueMember.league_id == league_id)).first()
    if m is None or m.user_id == actor.id:
        return False
    _unlink(s, m)
    s.commit()
    return True


def delete_league(s: Session, actor: User, league_id: int) -> bool:
    """Only the owner can delete a league. Members keep their seasons."""
    league = s.get(League, league_id)
    if league is None or league.owner_id != actor.id or _membership(s, actor, league_id) is None:
        return False
    for m in s.exec(select(LeagueMember).where(LeagueMember.league_id == league_id)).all():
        _unlink(s, m)
    s.flush()
    s.delete(league)
    s.commit()
    return True
