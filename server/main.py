"""FastAPI app: serves the web page and streams races over a WebSocket.

Run:  uvicorn server.main:app --reload
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections import Counter
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from sqlmodel import Session

from sim import TEAMS, RaceConfig, RaceEngine, default_grid
from sim.track import TRACK_SPECS, get_track
from sim.tyres import COMPOUNDS

from . import repo
from .auth import ACCESS_COOKIE, user_from_token
from .config import ROOT, settings
from .db import Role, User, engine as db_engine, init_db, ping
from .routes import router
from .schemas import (DriverOut, ModeCmd, PauseCmd, PitCmd, SetupOut, SpeedCmd, StartCmd, TeamOut, TrackOut,
                      command_adapter)
from .security import RateLimit, SameOriginWrites, SecurityHeaders, SlidingWindow, origin_allowed

log = logging.getLogger("f1sim")
TICK_S = 0.1           # wall-clock seconds between updates sent to the browser
TEAM_IDS = {t[0] for t in TEAMS}



@asynccontextmanager
async def lifespan(_app):
    init_db()
    yield


app = FastAPI(title="F1 Strategy Sim", debug=False, lifespan=lifespan, docs_url="/docs" if settings.debug else None,
              redoc_url=None, openapi_url="/openapi.json" if settings.debug else None)
app.add_middleware(SameOriginWrites, allowed=settings.allowed_origins)
app.add_middleware(SecurityHeaders, hsts=not settings.debug)
app.add_middleware(RateLimit, per_minute=settings.http_rate_per_min)
app.add_middleware(CORSMiddleware, allow_origins=settings.allowed_origins, allow_methods=["GET"],
                   allow_headers=[], allow_credentials=False)


app.include_router(router)


@app.exception_handler(Exception)
async def generic_error(request: Request, exc: Exception):
    log.exception("Unhandled error on %s", request.url.path)   # details stay in the server log
    return JSONResponse({"detail": "Internal error"}, status_code=500)


@app.get("/healthz", include_in_schema=False)
def healthz():
    # Used by the host to decide if the app is up. Says nothing beyond ok/not ok.
    if not ping():
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return {"status": "ok"}


@app.get("/api/setup", response_model=SetupOut)
def setup() -> SetupOut:
    grid = default_grid()
    teams = [TeamOut(id=i, name=n, color=c,
                     drivers=[DriverOut(code=d.code, name=d.name) for d in grid if d.team == i])
             for i, n, c, _ in TEAMS]
    tracks = []
    for tid in TRACK_SPECS:
        t = get_track(tid)
        step = max(1, len(t.points) // 120)
        tracks.append(TrackOut(id=tid, name=t.name, length_m=round(t.length_m), base_lap_s=t.base_lap_s,
                               wear=t.wear_mult, overtaking=t.overtake_mult, blurb=t.blurb,
                               outline=[[round(x), round(y)] for x, y in t.points[::step]]))
    return SetupOut(teams=teams, tracks=tracks, laps_default=30, speeds=[1, 2, 5, 10, 20, 50])


_ws_per_ip: Counter[str] = Counter()


@app.websocket("/ws/race")
async def race_socket(ws: WebSocket):
    ip = ws.client.host if ws.client else "unknown"
    if not origin_allowed(ws.headers.get("origin"), settings.allowed_origins):
        await ws.close(code=1008)   # policy violation
        return
    with Session(db_engine) as s:
        user = user_from_token(s, ws.cookies.get(ACCESS_COOKIE), "access")
        if user is not None:
            s.expunge(user)
    if user is None:
        await ws.close(code=4401)   # not signed in
        return
    if _ws_per_ip[ip] >= settings.ws_max_per_ip:
        await ws.close(code=1013)   # try again later
        return
    _ws_per_ip[ip] += 1
    try:
        await ws.accept()
        await RaceSession(ws, user).run()
    except WebSocketDisconnect:
        pass
    finally:
        _ws_per_ip[ip] -= 1


class RaceSession:
    """One race per connection. The browser only sends validated commands."""

    def __init__(self, ws: WebSocket, user: User):
        self.ws = ws
        self.user = user
        self.saved = False
        self.seed: int | None = None
        self.engine: RaceEngine | None = None
        self.team: str | None = None
        self.speed = 1
        self.paused = False
        self.limiter = SlidingWindow(settings.ws_msgs_per_sec, 1.0)
        self.pred_cache: dict[str, tuple[tuple, dict | None]] = {}

    async def run(self) -> None:
        receiver = asyncio.create_task(self._receive())
        try:
            while not receiver.done():
                if self.engine and not self.paused and not self.engine.finished:
                    self.engine.step(TICK_S * self.speed)
                    await self._send_state()
                    if self.engine.finished and not self.saved:
                        self._save_result()
                await asyncio.sleep(TICK_S)
            receiver.result()  # re-raise disconnects
        finally:
            receiver.cancel()

    async def _receive(self) -> None:
        while True:
            raw = await self.ws.receive_text()
            if len(raw) > settings.ws_max_msg_bytes:
                await self.ws.close(code=1009)
                return
            if not self.limiter.allow("msg"):
                await self._error("Slow down")
                continue
            try:
                cmd = command_adapter.validate_json(raw)
            except ValidationError:
                await self._error("Invalid message")
                continue
            await self._handle(cmd)

    async def _handle(self, cmd) -> None:
        if isinstance(cmd, StartCmd):
            if cmd.team not in TEAM_IDS:
                return await self._error("Unknown team")
            if cmd.track not in TRACK_SPECS:
                return await self._error("Unknown track")
            team_codes = {d.code for d in default_grid() if d.team == cmd.team}
            if cmd.tyres and not set(cmd.tyres) <= team_codes:
                return await self._error("Starting tyres can only be set for your own drivers")
            self.team = cmd.team
            # Always record a seed so any race can be replayed exactly.
            self.seed = cmd.seed if cmd.seed is not None else secrets.randbelow(2**31)
            self.engine = RaceEngine(RaceConfig(laps=cmd.laps, seed=self.seed, controlled_team=cmd.team,
                                                track_id=cmd.track, start_tyres=dict(cmd.tyres or {})))
            self.paused, self.pred_cache, self.saved = False, {}, False
            await self._send_init()
        elif self.engine is None:
            await self._error("Start a race first")
        elif isinstance(cmd, SpeedCmd):
            self.speed = cmd.value
        elif isinstance(cmd, PauseCmd):
            self.paused = cmd.paused
        elif isinstance(cmd, (PitCmd, ModeCmd)):
            if self.user.role not in (Role.principal, Role.admin):
                return await self._error("Viewers can watch but not give orders")
            car = self.engine.car(cmd.driver)
            # Authorisation: you may only give orders to your own team's cars.
            if car is None or car.driver.team != self.team:
                return await self._error("Not your driver")
            ok = (self.engine.request_pit(cmd.driver, cmd.compound) if isinstance(cmd, PitCmd)
                  else self.engine.set_mode(cmd.driver, cmd.mode))
            if not ok:
                await self._error("Order not accepted")

    async def _send_init(self) -> None:
        e = self.engine
        await self.ws.send_text(json.dumps({
            "type": "init", "track": e.track.to_json(), "team": self.team, "laps": e.cfg.laps,
            "seed": self.seed, "can_command": self.user.role in (Role.principal, Role.admin),
            "compounds": {k: {"name": c.name, "color": c.color} for k, c in COMPOUNDS.items()},
        }))
        await self._send_state()

    async def _send_state(self) -> None:
        snap = self.engine.snapshot()
        snap["type"] = "state"
        snap["speed"], snap["paused"] = self.speed, self.paused
        snap["predictions"] = [p for p in (self._prediction(c) for c in self.engine.cars
                                           if c.driver.team == self.team) if p]
        await self.ws.send_text(json.dumps(snap))

    def _prediction(self, car) -> dict | None:
        key = (car.lap, car.tyre, round(car.wear, 1), tuple(sorted(car.used)), car.running)
        cached = self.pred_cache.get(car.code)
        if cached and cached[0] == key:
            return cached[1]
        pred = self.engine.predicted_plan(car.code)
        self.pred_cache[car.code] = (key, pred)
        return pred

    def _save_result(self) -> None:
        self.saved = True
        order = self.engine.order()
        mine = [(i, c) for i, c in enumerate(order, 1) if c.driver.team == self.team]
        summary = ", ".join(f"{c.code} {'DNF' if c.retired else f'P{i}'}" for i, c in mine)
        with Session(db_engine) as s:
            owner = repo.user_by_id(s, self.user.id)
            if owner is not None:
                repo.save_race(s, owner, team=self.team, track=self.engine.track.id,
                               laps=self.engine.cfg.laps, seed=self.seed,
                               best_position=min((i for i, c in mine if not c.retired), default=None),
                               winner=order[0].driver.name, summary=summary[:400])

    async def _error(self, detail: str) -> None:
        await self.ws.send_text(json.dumps({"type": "error", "detail": detail}))


# The web page is served last so API routes win. Only the web/ folder is public.
app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")
