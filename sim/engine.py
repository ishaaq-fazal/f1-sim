"""Race engine: advances a race in time and exposes a JSON-friendly snapshot.

The model per lap:
    lap time = reference lap + driver/car pace + tyre delta(wear) + fuel load
               + pace mode + random noise (+ standing start, mistakes, VSC)
Cars then move round the lap at that rate. A car that catches the one ahead is
held behind it unless it wins an overtake roll (better with pace delta and DRS).
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from .grid import Driver, default_grid
from .strategy import Plan, best_plan
from .track import Track, default_track
from .tyres import COMPOUNDS, PACE_MODES

FUEL_S_PER_KG = 0.032
FUEL_PER_LAP_KG = 1.75
MIN_GAP_S = 0.22          # closest a car can follow without passing
STANDING_START_S = 2.8
GRID_SLOT_M = 8.0
MAX_SUBSTEP_S = 0.25


@dataclass
class RaceConfig:
    laps: int = 30
    seed: int | None = None
    controlled_team: str | None = None   # team whose pit calls the player makes
    vsc_chance_per_lap: float = 0.025
    dnf_chance_per_lap: float = 0.0008
    mistake_chance_per_lap: float = 0.012


@dataclass
class Car:
    driver: Driver
    grid: int
    lap: int = 0                  # completed laps
    frac: float = 0.0             # time fraction into the current lap (can be <0 on the grid)
    lap_target: float = 90.0      # planned time for the current lap
    lap_start_t: float = 0.0
    sector_start_t: float = 0.0
    sector_idx: int = 0
    sectors: list[float | None] = field(default_factory=lambda: [None, None, None])
    best_sectors: list[float] = field(default_factory=lambda: [math.inf] * 3)
    laps: list[dict] = field(default_factory=list)
    best: float = math.inf
    tyre: str = "M"
    tyre_age: int = 0
    wear: float = 0.0             # effective laps of wear on the set
    used: set[str] = field(default_factory=set)
    fuel: float = 0.0
    pits: int = 0
    pit_timer: float = 0.0
    pit_request: str | None = None
    pit_this_lap: bool = False
    mode: str = "normal"
    plan: Plan | None = None
    plan_base: int = 0            # lap the current plan was made on
    controlled: bool = False
    passing: str | None = None
    finished: bool = False
    finish_t: float = 0.0
    retired: bool = False
    penalty_s: float = 0.0

    @property
    def code(self) -> str:
        return self.driver.code

    @property
    def dist(self) -> float:
        return self.lap + self.frac

    @property
    def running(self) -> bool:
        return not (self.finished or self.retired)


class RaceEngine:
    def __init__(self, config: RaceConfig | None = None, track: Track | None = None,
                 drivers: list[Driver] | None = None):
        self.cfg = config or RaceConfig()
        self.track = track or default_track()
        self.rng = random.Random(self.cfg.seed)
        self.t = 0.0
        self.chequered = False
        self.vsc_until = -1.0
        self.vsc_count = 0
        self.leader_lap_seen = 0
        self.fastest: dict | None = None
        self.events: list[dict] = []
        self.new_laps: list[dict] = []
        self.cars: list[Car] = []
        self._setup(drivers or default_grid())

    # ----------------------------------------------------------------- setup
    def _setup(self, drivers: list[Driver]) -> None:
        # Qualifying: pace plus noise sets the grid.
        quali = sorted(drivers, key=lambda d: d.pace_s + self.rng.gauss(0, 0.18))
        for slot, d in enumerate(quali):
            car = Car(driver=d, grid=slot + 1)
            car.controlled = d.team == self.cfg.controlled_team
            car.plan = best_plan(self.cfg.laps, self.track.pit_loss_s)
            if not car.controlled:
                # AI teams vary a little from the optimum so races differ.
                car.plan.stops = [(max(2, min(self.cfg.laps - 2, n + self.rng.randint(-2, 2))), c)
                                  for n, c in car.plan.stops]
                if self.rng.random() < 0.25:
                    alt = self.rng.choice([c for c in COMPOUNDS if c != car.plan.start_compound])
                    car.plan = _with_start(car.plan, alt, self.cfg.laps, self.track.pit_loss_s)
            car.tyre = car.plan.start_compound or "M"
            car.used = {car.tyre}
            car.fuel = self.cfg.laps * FUEL_PER_LAP_KG + 3.0
            car.frac = -(slot * GRID_SLOT_M + 6.0) / self.track.length_m
            car.lap_target = self._lap_time(car)
            self.cars.append(car)
        self._event("start", f"Lights out! {self.cfg.laps} laps of {self.track.name}.")

    # ------------------------------------------------------------- modelling
    def _lap_time(self, car: Car) -> float:
        d = car.driver
        mode_s, _ = PACE_MODES[car.mode]
        t = (self.track.base_lap_s + d.pace_s + COMPOUNDS[car.tyre].delta(car.wear)
             + car.fuel * FUEL_S_PER_KG + mode_s + self.rng.gauss(0, d.consistency_s))
        if car.lap == 0:
            t += STANDING_START_S
        if self.rng.random() < self.cfg.mistake_chance_per_lap:
            loss = self.rng.uniform(1.0, 4.0)
            t += loss
            self._event("mistake", f"{car.code} runs wide and loses {loss:.1f}s", car.code)
        return t

    @property
    def vsc(self) -> bool:
        return self.t < self.vsc_until

    def _event(self, kind: str, text: str, code: str | None = None) -> None:
        lap = min(self.cfg.laps, max(1, self.leader().lap + 1)) if self.cars else 1
        self.events.append({"t": round(self.t, 1), "lap": lap, "kind": kind, "text": text, "code": code})

    # ------------------------------------------------------------- commands
    def car(self, code: str) -> Car | None:
        return next((c for c in self.cars if c.code == code), None)

    def request_pit(self, code: str, compound: str | None) -> bool:
        """Box at the end of this lap; compound None cancels the call."""
        car = self.car(code)
        if not car or not car.running or (compound and compound not in COMPOUNDS):
            return False
        car.pit_request = compound
        if compound:
            self._event("radio", f"{car.code}: box this lap for {COMPOUNDS[compound].name}s", car.code)
        return True

    def set_mode(self, code: str, mode: str) -> bool:
        car = self.car(code)
        if not car or not car.running or mode not in PACE_MODES:
            return False
        car.mode = mode
        return True

    # ----------------------------------------------------------- simulation
    def step(self, dt: float) -> None:
        while dt > 1e-9 and not self.finished:
            h = min(dt, MAX_SUBSTEP_S)
            self._substep(h)
            dt -= h

    @property
    def finished(self) -> bool:
        return all(not c.running for c in self.cars)

    def _substep(self, dt: float) -> None:
        self.t += dt
        slow = 1.4 if self.vsc else 1.0
        for car in self.cars:
            if not car.running:
                continue
            if car.pit_timer > 0:
                car.pit_timer -= dt
                if car.pit_timer <= 0:
                    car.pit_timer = 0
                    self._event("pit-out", f"{car.code} rejoins on {COMPOUNDS[car.tyre].name}s", car.code)
                continue
            car.frac += dt / (car.lap_target * slow)
        self._interactions(dt)
        for car in self.cars:
            if car.running and car.pit_timer <= 0:
                self._sectors_and_line(car)

    def _interactions(self, dt: float) -> None:
        order = sorted((c for c in self.cars if c.running and c.pit_timer <= 0), key=lambda c: -c.dist)
        for ahead, car in zip(order, order[1:]):
            if ahead.dist - car.dist >= 0.5:      # not really racing each other on track
                continue
            gap_laps = MIN_GAP_S / car.lap_target
            if car.dist <= ahead.dist - gap_laps:
                if car.passing == ahead.code and car.dist < ahead.dist - 3 * gap_laps:
                    car.passing = None
                continue
            if car.passing == ahead.code:
                continue
            delta = ahead.lap_target - car.lap_target         # s/lap, + means car is faster
            drs = (not self.vsc and car.lap >= 2 and self.track.in_drs(car.frac))
            rate = 0.0 if self.vsc else max(0.0, 0.01 + 0.22 * delta + (0.35 if drs else 0.0))
            rate *= 0.6 + 0.8 * car.driver.racecraft - 0.4 * ahead.driver.racecraft
            if ahead.dist - car.dist > 0.45:  # lapping a backmarker: blue flags
                rate = 5.0
            if rate > 0 and self.rng.random() < 1 - math.exp(-rate * dt):
                car.passing = ahead.code
                if ahead.dist - car.dist < 0.45:
                    self._event("overtake", f"{car.code} passes {ahead.code}" + (" with DRS" if drs else ""), car.code)
                continue
            car.frac = ahead.dist - gap_laps - car.lap   # held up behind

    def _sectors_and_line(self, car: Car) -> None:
        bounds = self.track.sector_time
        while car.sector_idx < 2 and car.frac >= bounds[car.sector_idx]:
            t_cross = self.t - (car.frac - bounds[car.sector_idx]) * car.lap_target
            self._record_sector(car, car.sector_idx, t_cross)
            car.sector_idx += 1
        while car.frac >= 1.0 and car.running:
            t_cross = self.t - (car.frac - 1.0) * car.lap_target
            car.frac -= 1.0
            self._record_sector(car, 2, t_cross)
            self._complete_lap(car, t_cross)

    def _record_sector(self, car: Car, idx: int, t_cross: float) -> None:
        st = t_cross - car.sector_start_t
        car.sectors[idx] = st
        car.sector_start_t = t_cross
        if car.lap > 0 and not car.pit_this_lap:
            car.best_sectors[idx] = min(car.best_sectors[idx], st)

    def _complete_lap(self, car: Car, t_cross: float) -> None:
        lap_time = t_cross - car.lap_start_t
        car.lap += 1
        car.lap_start_t = t_cross
        car.sector_idx = 0
        car.tyre_age += 1
        _, wear_mult = PACE_MODES[car.mode]
        car.wear += wear_mult * (0.4 if self.vsc else 1.0)
        car.fuel = max(0.0, car.fuel - FUEL_PER_LAP_KG)
        record = {"code": car.code, "lap": car.lap, "time": round(lap_time, 3), "tyre": car.tyre,
                  "age": car.tyre_age, "pit": car.pit_this_lap, "sectors": [round(s or 0, 3) for s in car.sectors]}
        car.laps.append(record)
        self.new_laps.append(record)
        clean = car.lap > 1 and not car.pit_this_lap
        car.pit_this_lap = False
        if clean:
            car.best = min(car.best, lap_time)
            if self.fastest is None or lap_time < self.fastest["time"]:
                self.fastest = {"code": car.code, "time": lap_time, "lap": car.lap}
                if car.lap > 2:
                    self._event("fastest", f"Fastest lap: {car.code} {fmt_time(lap_time)}", car.code)

        if car.lap >= self.cfg.laps or self.chequered:
            if not self.chequered:
                self._event("finish", f"Chequered flag! {car.code} wins", car.code)
            self.chequered = True
            car.finished = True
            car.finish_t = t_cross
            if len(car.used) < 2:
                car.penalty_s = 10.0
                self._event("penalty", f"{car.code} +10s: only one compound used", car.code)
            return

        if self.rng.random() < self.cfg.dnf_chance_per_lap:
            car.retired = True
            self._event("dnf", f"{car.code} retires with a power unit failure", car.code)
            return

        self._race_control(car)
        self._maybe_pit(car)
        car.lap_target = self._lap_time(car)

    def _race_control(self, car: Car) -> None:
        if car is not self.leader() or car.lap <= self.leader_lap_seen:
            return
        self.leader_lap_seen = car.lap
        laps_left = self.cfg.laps - car.lap
        if (not self.vsc and self.vsc_count < 2 and 3 <= car.lap and laps_left > 3
                and self.rng.random() < self.cfg.vsc_chance_per_lap):
            self.vsc_until = self.t + self.track.base_lap_s * self.rng.uniform(1.5, 2.5)
            self.vsc_count += 1
            self._event("vsc", "Virtual Safety Car deployed: debris on track")

    def _maybe_pit(self, car: Car) -> None:
        laps_left = self.cfg.laps - car.lap
        compound = None
        if car.controlled:
            compound, car.pit_request = car.pit_request, None
        else:
            due = car.plan.stops[0] if car.plan and car.plan.stops else None
            if due:
                n, c = due
                laps_to_stop = n - (car.lap - car.plan_base)
                if laps_to_stop <= 0 or (self.vsc and laps_to_stop <= 6):
                    compound = c
            worn = car.wear > COMPOUNDS[car.tyre].cliff_lap + 2 and laps_left > 3
            if compound is None and (worn or (len(car.used) < 2 and laps_left == 2)):
                plan = best_plan(laps_left, self.track.pit_loss_s, car.tyre, car.wear, car.used)
                compound = plan.stops[0][1] if plan.stops else ("H" if car.tyre != "H" else "M")
        if compound is None or laps_left < 1:
            return
        loss = self.track.pit_loss_s * (0.55 if self.vsc else 1.0)
        stationary = 2.3 + abs(self.rng.gauss(0, 0.3))
        if self.rng.random() < 0.04:
            stationary += self.rng.uniform(2, 6)
            self._event("slow-stop", f"Slow stop for {car.code}! {stationary:.1f}s", car.code)
        car.pit_timer = loss - 2.3 + stationary
        car.pit_this_lap = True
        old = car.tyre
        car.tyre, car.tyre_age, car.wear = compound, 0, 0.0
        car.used.add(compound)
        car.pits += 1
        car.passing = None
        if not car.controlled and car.plan:
            # Re-plan the rest of the race from here.
            car.plan = best_plan(laps_left, self.track.pit_loss_s, compound, 0.0, car.used)
            car.plan_base = car.lap
        self._event("pit", f"{car.code} pits: {COMPOUNDS[old].name} -> {COMPOUNDS[compound].name}"
                    + (" (cheap stop under VSC)" if self.vsc else ""), car.code)

    # -------------------------------------------------------------- queries
    def order(self) -> list[Car]:
        def key(c: Car):
            if c.finished:
                return (0, -c.lap, c.finish_t + c.penalty_s)
            if c.retired:
                return (2, -c.dist, 0)
            return (1, -c.dist, 0)
        return sorted(self.cars, key=key)

    def leader(self) -> Car:
        return self.order()[0]

    def snapshot(self) -> dict:
        order = self.order()
        lead = order[0]
        rows = []
        prev = None
        for pos, c in enumerate(order, 1):
            rows.append({
                "pos": pos, "code": c.code, "name": c.driver.name, "team": c.driver.team,
                "color": c.driver.color, "grid": c.grid,
                "lap": c.lap, "track_pos": round(self.track.dist_frac(c.frac), 5),
                "in_pit": c.pit_timer > 0, "pit_timer": round(c.pit_timer, 1),
                "gap": self._gap(c, lead), "interval": self._gap(c, prev) if prev else "",
                "last": c.laps[-1]["time"] if c.laps else None,
                "best": None if c.best == math.inf else round(c.best, 3),
                "sectors": [None if s is None else round(s, 3) for s in c.sectors],
                "tyre": c.tyre, "tyre_age": c.tyre_age, "wear": round(COMPOUNDS[c.tyre].wear_pct(c.wear)),
                "used": sorted(c.used), "pits": c.pits, "mode": c.mode, "pit_request": c.pit_request,
                "controlled": c.controlled, "finished": c.finished, "retired": c.retired,
                "penalty": c.penalty_s,
            })
            prev = c
        events, self.events = self.events, []
        laps, self.new_laps = self.new_laps, []
        return {
            "t": round(self.t, 2), "lap": min(self.cfg.laps, lead.lap + (0 if lead.finished else 1)),
            "total_laps": self.cfg.laps, "vsc": self.vsc, "chequered": self.chequered,
            "finished": self.finished,
            "fastest": None if not self.fastest else {**self.fastest, "time": round(self.fastest["time"], 3)},
            "cars": rows, "events": events, "laps": laps,
        }

    def _gap(self, c: Car, ref: Car | None) -> str:
        if ref is None or ref is c:
            return "LEADER" if ref is c else ""
        if c.retired:
            return "DNF"
        if c.finished and ref.finished and c.lap == ref.lap:
            return f"+{(c.finish_t + c.penalty_s) - (ref.finish_t + ref.penalty_s):.3f}"
        laps_down = int(ref.dist - c.dist) if not ref.finished else ref.lap - c.lap - (0 if c.finished else 1)
        if laps_down >= 1:
            return f"+{laps_down} LAP" + ("S" if laps_down > 1 else "")
        return f"+{max(0.0, (ref.dist - c.dist) * c.lap_target):.1f}"

    def predicted_plan(self, code: str) -> dict | None:
        """The optimiser's best strategy from this car's current state (a prediction hint)."""
        car = self.car(code)
        if not car or not car.running:
            return None
        laps_left = self.cfg.laps - car.lap
        plan = best_plan(laps_left, self.track.pit_loss_s, car.tyre, car.wear, car.used)
        return {"code": code, "stops": [{"in_laps": n, "lap": car.lap + n, "compound": c} for n, c in plan.stops],
                "cost_s": round(plan.cost_s, 1)}


def _with_start(plan: Plan, start: str, laps: int, pit_loss: float) -> Plan:
    p = best_plan(laps, pit_loss, start, 0.0, set())
    p.start_compound = start
    return p


def fmt_time(s: float) -> str:
    m, sec = divmod(s, 60)
    return f"{int(m)}:{sec:06.3f}"
