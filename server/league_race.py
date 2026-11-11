"""League race rooms: one shared race per league round.

Every member of a league opens the lobby, picks starting tyres and presses Ready. When every
member is ready, one race starts with all their teams on the same grid. Each player commands
only their own team; the league owner controls speed and pause. The result is saved into every
member's season.

Each player's connection (RaceSession in main.py) only ever writes to its own socket. The room
holds the shared engine and is advanced by whichever connection ticks first, so nothing here
sends across connections and it works the same with one event loop or several.
"""
from __future__ import annotations

import logging
import threading
import time

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from sim import RaceConfig, RaceEngine, default_grid
from sim.season import ROUNDS, round_weather, track_for_round

from . import repo
from .db import Role, User, engine as db_engine

log = logging.getLogger("f1sim")
MAX_STEP_S = 0.5          # never advance more than this much wall time in one tick


class RoomError(Exception):
    pass


class LeagueRoom:
    def __init__(self, league_id: int):
        self.league_id = league_id
        self.lock = threading.RLock()
        self.version = 0                         # bumped whenever the lobby changes
        self.online: dict[int, int] = {}         # user id -> open connections
        self.ready: dict[int, dict[str, str]] = {}   # user id -> starting tyres
        self.race_id = 0                         # bumped for each race started in this room
        self.engine: RaceEngine | None = None
        self.players: dict[int, dict] = {}       # user id -> lobby row, fixed when the race starts
        self.host_id: int | None = None
        self.round_no = 0
        self.seed = 0
        self.speed = 1
        self.paused = False
        self.saved = False
        self.last_tick: float | None = None
        self.snap: dict | None = None            # latest snapshot without events/laps
        self.seq = 0                             # snapshots taken so far
        self.events: list[tuple[int, dict]] = []     # (seq, event) for the whole race
        self.laps: list[tuple[int, dict]] = []

    # ------------------------------------------------------------ lobby
    def info(self, user: User) -> dict | None:
        with Session(db_engine) as s:
            return repo.league_lobby(s, user, self.league_id)

    def racing(self) -> bool:
        return self.engine is not None and not self.engine.finished

    def join(self, user: User) -> None:
        with self.lock:
            if self.engine is not None and self.engine.finished:
                self._reset()                    # someone is back for the next round
            self.online[user.id] = self.online.get(user.id, 0) + 1
            self.version += 1

    def leave(self, user: User) -> None:
        with self.lock:
            n = self.online.get(user.id, 0) - 1
            if n <= 0:
                self.online.pop(user.id, None)
                self.ready.pop(user.id, None)    # gone from the lobby: no longer ready
            else:
                self.online[user.id] = n
            if not self.online and self.racing():
                # Everyone has gone: the race is abandoned and the round can be raced again.
                log.info("League %s round %s abandoned: no players left", self.league_id, self.round_no)
                self._reset()
            self.version += 1

    def set_ready(self, user: User, ready: bool, tyres: dict[str, str]) -> None:
        if user.role not in (Role.principal, Role.admin):
            raise RoomError("Viewers can't race")
        info = self.info(user)
        if info is None:
            raise RoomError("League not found")
        me = next((p for p in info["players"] if p["user_id"] == user.id), None)
        if me is None:
            raise RoomError("League not found")
        own = {d.code for d in default_grid() if d.team == me["team"]}
        if not set(tyres) <= own:
            raise RoomError("Starting tyres can only be set for your own drivers")
        with self.lock:
            if self.racing():
                raise RoomError("This round is already racing")
            if ready:
                self.ready[user.id] = dict(tyres)
            else:
                self.ready.pop(user.id, None)
            self.version += 1
            self._maybe_start(info)

    def lobby_json(self, user: User, info: dict) -> dict:
        players = info["players"]
        rounds = {p["rounds_done"] for p in players}
        blocker = None
        if any(p["finished"] for p in players):
            blocker = "This league's season is over"
        elif len(rounds) > 1:
            blocker = "Players are on different rounds"
        round_no = (min(rounds) + 1) if rounds else 1
        me = next((p for p in players if p["user_id"] == user.id), None)
        return {
            "type": "lobby", "league": info["id"], "name": info["name"], "laps": info["laps"],
            "round": round_no, "rounds": ROUNDS,
            "track": track_for_round(round_no) if round_no <= ROUNDS else None,
            "status": "racing" if self.racing() else "waiting", "blocker": blocker,
            "host": info["owner_id"] == user.id, "your_season": me["season_id"] if me else None,
            "players": [{"nickname": p["nickname"], "team": p["team"], "you": p["user_id"] == user.id,
                         "online": p["user_id"] in self.online, "ready": p["user_id"] in self.ready}
                        for p in players],
        }

    def _maybe_start(self, info: dict) -> None:
        players = info["players"]
        if not players or self.racing():
            return
        if any(p["user_id"] not in self.ready for p in players):
            return
        if any(p["finished"] for p in players) or len({p["rounds_done"] for p in players}) != 1:
            return
        self.round_no = players[0]["rounds_done"] + 1
        lead = players[0]
        with Session(db_engine) as s:
            season = repo.get_season(s, repo.user_by_id(s, lead["user_id"]), lead["season_id"])
            self.seed = repo.season_round_seed(season, self.round_no)
        tyres: dict[str, str] = {}
        for p in players:
            tyres.update(self.ready[p["user_id"]])
        self.engine = RaceEngine(RaceConfig(
            laps=info["laps"], seed=self.seed, controlled_teams=tuple(p["team"] for p in players),
            track_id=track_for_round(self.round_no), start_tyres=tyres, weather=round_weather(self.seed)),
            drivers=default_grid())
        self.players = {p["user_id"]: dict(p) for p in players}
        self.host_id = info["owner_id"]
        self.race_id += 1
        self.speed, self.paused, self.saved = 1, False, False
        self.last_tick = time.monotonic()
        self.events, self.laps, self.seq = [], [], 0
        self.ready.clear()
        self._take_snapshot()
        self.version += 1

    def _reset(self) -> None:
        self.engine = None
        self.players = {}
        self.snap = None
        self.events, self.laps = [], []
        self.version += 1

    # ------------------------------------------------------------ race
    def tick(self) -> None:
        """Advance the shared race to now. Safe to call from every connection."""
        with self.lock:
            if self.engine is None or self.engine.finished:
                return
            now = time.monotonic()
            dt = min(MAX_STEP_S, now - (self.last_tick or now))
            self.last_tick = now
            if self.paused or dt <= 0:
                return
            self.engine.step(dt * self.speed)
            if self.engine.finished and not self.saved:
                self.save_result()
            self._take_snapshot()

    def _take_snapshot(self) -> None:
        snap = self.engine.snapshot()
        self.seq += 1
        self.events.extend((self.seq, e) for e in snap.pop("events"))
        self.laps.extend((self.seq, l) for l in snap.pop("laps"))
        self.snap = snap

    def team_of(self, user: User) -> str | None:
        p = self.players.get(user.id)
        return p["team"] if p else None

    def command(self, user: User, kind: str, **kw) -> str | None:
        """Apply a player's order. Returns an error message, or None if accepted."""
        with self.lock:
            if not self.racing():
                return "The race isn't running"
            if kind in ("speed", "pause"):
                if user.id != self.host_id:
                    return "Only the league owner controls speed and pause"
                if kind == "speed":
                    self.speed = kw["value"]
                else:
                    self.paused = kw["paused"]
                    self.last_tick = time.monotonic()
                return None
            if user.role not in (Role.principal, Role.admin):
                return "Viewers can watch but not give orders"
            car = self.engine.car(kw["driver"])
            # Authorisation: only your own team's cars, even though every player's cars are "controlled".
            if car is None or car.driver.team != self.team_of(user):
                return "Not your driver"
            ok = (self.engine.request_pit(kw["driver"], kw["compound"]) if kind == "pit"
                  else self.engine.set_mode(kw["driver"], kw["mode"]))
            return None if ok else "Order not accepted"

    def save_result(self) -> None:
        """Save the finished race into every player's season and race history."""
        self.saved = True
        order = self.engine.order()
        results = [(c.code, c.driver.team, i, c.retired) for i, c in enumerate(order, 1)]
        track = self.engine.track.id
        with Session(db_engine) as s:
            for uid, p in self.players.items():
                owner = repo.user_by_id(s, uid)
                if owner is None:
                    continue
                mine = [(i, c) for i, c in enumerate(order, 1) if c.driver.team == p["team"]]
                summary = f"League round {self.round_no}: " + ", ".join(
                    f"{c.code} {'DNF' if c.retired else f'P{i}'}" for i, c in mine)
                try:
                    repo.save_race(s, owner, team=p["team"], track=track, laps=self.engine.cfg.laps,
                                   seed=self.seed, best_position=min((i for i, c in mine if not c.retired), default=None),
                                   winner=order[0].driver.name, summary=summary[:400])
                    repo.record_round(s, owner, p["season_id"], self.round_no, track, self.seed, results)
                except (repo.SeasonError, repo.PermissionDenied, IntegrityError):
                    s.rollback()
                    log.warning("League round not recorded (league %s, season %s, round %s)",
                                self.league_id, p["season_id"], self.round_no)

    def state_for(self, user: User, since: int) -> dict:
        """The latest shared state as this player sees it: their own cars marked as theirs, and
        the events and laps they haven't been sent yet."""
        mine = self.team_of(user)
        nick = {p["team"]: p["nickname"] for p in self.players.values()}
        snap = dict(self.snap)
        snap["cars"] = [{**c, "controlled": c["team"] == mine, "player": nick.get(c["team"])}
                        for c in self.snap["cars"]]
        snap["events"] = [e for q, e in self.events if q > since]
        snap["laps"] = [l for q, l in self.laps if q > since]
        snap["type"] = "state"
        snap["speed"], snap["paused"] = self.speed, self.paused
        snap["host"] = user.id == self.host_id
        return snap


_rooms: dict[int, LeagueRoom] = {}
_rooms_lock = threading.Lock()


def room_for(league_id: int) -> LeagueRoom:
    with _rooms_lock:
        room = _rooms.get(league_id)
        if room is None:
            room = _rooms[league_id] = LeagueRoom(league_id)
        return room


def release(room: LeagueRoom) -> None:
    """Forget a room nobody is in, unless its race is still running."""
    with _rooms_lock, room.lock:
        if not room.online and not room.racing() and _rooms.get(room.league_id) is room:
            del _rooms[room.league_id]
