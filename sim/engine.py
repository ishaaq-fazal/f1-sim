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
from .track import Track, get_track
from .tyres import (COMPOUNDS, DRY, INTER_FROM, PACE_MODES, WET, WET_FROM, condition_delta, ideal_type,
                    needs_second_compound, wear_factor)
from .weather import Weather, rain_label

FUEL_S_PER_KG = 0.032
FUEL_PER_LAP_KG = 1.75
MIN_GAP_S = 0.22          # closest a car can follow without passing
STANDING_START_S = 2.8
GRID_SLOT_M = 8.0
MAX_SUBSTEP_S = 0.25
REPLAY_FRAME_S = 2.0       # race seconds between recorded replay frames
# Race-day form: how well each car suits this circuit and how sharp each driver is today.
# Drawn once per race, so it doesn't average out over the laps like lap-to-lap noise does.
TEAM_FORM_SD = 0.15       # s/lap, shared by both cars of a team
DRIVER_FORM_SD = 0.08     # s/lap, per driver
# Qualifying: one flying lap each on low fuel and the fastest tyre for the conditions.
QUALI_NOISE_S = 0.22      # lap noise for an average driver (scaled by their consistency)
QUALI_MISTAKE = 0.04      # chance of a scruffy lap on a normal run (more in the wet)
# Qualifying approach: (lap time offset s, noise multiplier, mistake chance multiplier).
QUALI_MODES: dict[str, tuple[float, float, float]] = {
    "safe": (0.15, 0.6, 0.25),
    "normal": (0.0, 1.0, 1.0),
    "push": (-0.25, 1.3, 3.0),
}
# Neutralisations. Under a VSC every car slows by the same factor, so gaps freeze. Under a Safety
# Car the leader follows the SC and everyone else closes up to the car ahead, so the field bunches.
VSC_SLOW = 1.4            # lap time multiplier under the VSC
SC_SLOW = 1.45            # Safety Car lap time vs a normal lap
SC_GAP_S = 1.2            # gap the queue closes up to behind the Safety Car
SC_CATCH = 1.12           # how much slower than racing pace cars run while catching the queue
# The pit lane speed limit doesn't change, so a stop loses less ground while the field is slow:
# about half the usual time under the SC and a bit less than usual under the VSC.
SC_PIT_MULT = 0.75
VSC_PIT_MULT = 0.9
MAX_NEUTRAL = 3           # most Safety Cars + VSCs in one race
SC_SHARE = {"crash": 0.7, "stopped": 0.25, "debris": 0.35}   # chance an incident needs a full SC
# Team orders: the car letting its team-mate by runs this much slower until it's been passed.
YIELD_SLOW = 1.05
SWAP_MAX_GAP_S = 3.0
RADIO = {"push": "push now, everything you've got", "normal": "back to normal pace",
         "conserve": "save the tyres, bring it home"}


@dataclass
class RaceConfig:
    laps: int = 30
    seed: int | None = None
    controlled_team: str | None = None   # team whose pit calls the player makes
    controlled_teams: tuple[str, ...] = ()   # more player teams (league races: one per player)
    track_id: str = "harbourline"
    start_tyres: dict[str, str] | None = None   # player's starting compound per driver code
    quali_modes: dict[str, str] | None = None   # player's qualifying approach per driver: safe, normal, push
    weather: str = "dry"                        # dry, wet, changeable, showers or drying
    sprint: bool = False     # sprint race: short, no mandatory stop (two-compound rule doesn't apply)
    vsc_chance_per_lap: float = 0.025       # debris on track (VSC or Safety Car) per leader lap
    crash_chance_per_lap: float = 0.0008     # per car per lap: crashes out and brings out a SC or VSC
    dnf_chance_per_lap: float = 0.0008
    mistake_chance_per_lap: float = 0.012
    # Career mode: per-team multipliers from car upgrades (1.0 = no change).
    team_wear: dict[str, float] | None = None   # tyre wear rate
    team_dnf: dict[str, float] | None = None    # chance of a mechanical retirement
    team_pit: dict[str, float] | None = None    # seconds the pit crew saves on each stop


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
    weather_threshold: float = 1.2  # s/lap gain a team needs before switching tyre type
    weather_look: float = 1.0       # laps ahead the team's radar looks
    controlled: bool = False
    passing: str | None = None
    finished: bool = False
    finish_t: float = 0.0
    retired: bool = False
    penalty_s: float = 0.0
    form_s: float = 0.0           # race-day form, s/lap (negative = faster)
    advice: dict | None = None    # box-now call made when a Safety Car or VSC came out
    yield_to: str | None = None   # team-mate this car is letting past (team orders)
    yield_until: float = 0.0
    last_order_lap: int = -99     # last lap the team gave this pair a swap order

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
        self.track = track or get_track(self.cfg.track_id)
        self.rng = random.Random(self.cfg.seed)
        self.weather = Weather(self.cfg.weather, random.Random(self.rng.random()),
                               self.cfg.laps * self.track.base_lap_s * 1.05)
        self._rain_on = self.weather.rain > 0.03
        self._ideal = ideal_type(self.weather.wetness)
        self.t = 0.0
        self.chequered = False
        self.vsc_until = -1.0
        self.vsc_count = 0
        self.sc = False               # full Safety Car out
        self.sc_end_lap = 0           # leader lap at the end of which the SC comes in
        self.sc_in = False            # "Safety Car in this lap" announced
        self.sc_count = 0
        self.drs_after_t = 0.0        # DRS stays off for a lap after a restart
        # Incidents and AI radio calls draw from their own stream, seeded from the race seed.
        self.inc_rng = random.Random(f"incidents-{self.cfg.seed}" if self.cfg.seed is not None else None)
        self.leader_lap_seen = 0
        self.fastest: dict | None = None
        self.events: list[dict] = []
        self.event_log: list[dict] = []   # every event of the race, kept for the replay
        self.new_laps: list[dict] = []
        self.frames: list[list] = []      # replay frames: [t, [distance per car], status per car]
        self._next_frame = 0.0
        self._replay: dict | None = None
        self.cars: list[Car] = []
        self.qualifying: list[dict] = []
        self._setup(drivers or default_grid())
        self._record_frame()              # the grid, at t = 0

    # ----------------------------------------------------------------- setup
    def _setup(self, drivers: list[Driver]) -> None:
        team_form = {t: self.rng.gauss(0, TEAM_FORM_SD) for t in sorted({d.team for d in drivers})}
        form = {d.code: team_form[d.team] + self.rng.gauss(0, DRIVER_FORM_SD) for d in drivers}
        quali = self._qualify(drivers, form)
        # Best plan for each possible starting compound on this circuit.
        by_start = {c: best_plan(self.cfg.laps, self.track.pit_loss_s, wear_mult=self.track.wear_mult,
                                 starts=[c], require_two=not self.cfg.sprint) for c in DRY}
        wet_start = ideal_type(self.weather.wetness)
        best_cost = min(p.cost_s for p in by_start.values())
        for slot, d in enumerate(quali):
            car = Car(driver=d, grid=slot + 1, form_s=form[d.code])
            car.controlled = d.team == self.cfg.controlled_team or d.team in self.cfg.controlled_teams
            chosen = (self.cfg.start_tyres or {}).get(d.code) if car.controlled else None
            car.weather_threshold = self.rng.uniform(0.6, 2.2)
            car.weather_look = self.rng.choice((0.5, 1.0, 1.5, 2.0))
            if chosen is None and wet_start != "dry":
                chosen = wet_start          # wet race: start on the tyre for the conditions
            if chosen in WET:
                car.plan = Plan(chosen, [], 0.0)   # the weather decides the stops
            elif chosen is None:
                # Teams pick among starting tyres whose plans are close to the best, so the grid
                # starts on a mix of compounds rather than everyone on the same one.
                options = [c for c, p in by_start.items() if p.cost_s <= best_cost + 8.0]
                weights = [math.exp(-(by_start[c].cost_s - best_cost) / 4.0) for c in options]
                chosen = self.rng.choices(options, weights)[0]
            if chosen in DRY:
                base = by_start[chosen]
                car.plan = Plan(chosen, list(base.stops), base.cost_s)
            if not car.controlled and chosen in DRY:
                # AI teams vary their stop laps a little so races differ.
                car.plan.stops = [(max(2, min(self.cfg.laps - 2, n + self.rng.randint(-2, 2))), c)
                                  for n, c in car.plan.stops]
            car.tyre = chosen
            car.used = {car.tyre}
            car.fuel = self.cfg.laps * FUEL_PER_LAP_KG + 3.0
            car.frac = -(slot * GRID_SLOT_M + 6.0) / self.track.length_m
            car.lap_target = self._lap_time(car)
            self.cars.append(car)
        q = self.qualifying
        self._event("quali", f"Pole position: {q[0]['code']} ({q[0]['time']:.3f}), "
                    f"{q[1]['code']} +{q[1]['gap']:.3f}, {q[2]['code']} +{q[2]['gap']:.3f}", q[0]["code"])
        for row in q:
            if row["mistake"]:
                self._event("quali", f"{row['code']} made a mistake on their qualifying lap: P{row['pos']}", row["code"])
        self._event("start", f"Lights out! {self.cfg.laps} laps of {self.track.name}"
                    + (" in the Sprint." if self.cfg.sprint else ".")
                    + {"dry": "", "wet": " It's raining: a wet race.", "changeable": " Changeable weather expected.",
                       "showers": " Showers expected on and off.", "drying": " Wet start, but it should dry out."}[self.cfg.weather])

    def _qualify(self, drivers: list[Driver], form: dict[str, float]) -> list[Driver]:
        """One flying lap each sets the grid. Pushing is quicker but riskier; playing safe is
        slower but rarely goes wrong. Rain makes every lap less predictable."""
        w = self.weather.wetness
        ideal = ideal_type(w)
        tyre = "S" if ideal == "dry" else ideal
        teams = set(self.cfg.controlled_teams) | ({self.cfg.controlled_team} if self.cfg.controlled_team else set())
        laps = []
        for d in drivers:
            mode = (self.cfg.quali_modes or {}).get(d.code, "normal") if d.team in teams else "normal"
            offset, noise, risk = QUALI_MODES.get(mode, QUALI_MODES["normal"])
            t = (self.track.base_lap_s + d.pace_s + form[d.code] + offset
                 + COMPOUNDS[tyre].delta(0) + 6.0 * w + condition_delta(tyre, w)
                 + self.rng.gauss(0, QUALI_NOISE_S * noise * (d.consistency_s / 0.24) * (1 + 2 * w)))
            mistake = self.rng.random() < QUALI_MISTAKE * risk * (1 + 3 * w)
            if mistake:
                t += self.rng.uniform(0.8, 3.0)
            laps.append((t, d, mode, mistake))
        laps.sort(key=lambda x: x[0])
        pole = laps[0][0]
        self.qualifying = [{"pos": i + 1, "code": d.code, "name": d.name, "team": d.team, "color": d.color,
                            "time": round(t, 3), "gap": round(t - pole, 3), "tyre": tyre, "mode": mode,
                            "mistake": mistake} for i, (t, d, mode, mistake) in enumerate(laps)]
        return [d for _, d, _, _ in laps]

    # ------------------------------------------------------------- modelling
    def _lap_time(self, car: Car) -> float:
        d = car.driver
        mode_s, _ = PACE_MODES[car.mode]
        # Wetness over the coming lap: average of now and one lap from now.
        w = (self.weather.wetness + self.weather.wetness_at(self.t, self.t + self.track.base_lap_s)) / 2
        t = (self.track.base_lap_s + d.pace_s + car.form_s + COMPOUNDS[car.tyre].delta(car.wear)
             + car.fuel * FUEL_S_PER_KG + mode_s + self.rng.gauss(0, d.consistency_s * (1 + w))
             + 6.0 * w + condition_delta(car.tyre, w))
        if car.lap == 0:
            t += STANDING_START_S
        mistake = self.cfg.mistake_chance_per_lap * (1 + 3 * w)
        if car.tyre in DRY and w > INTER_FROM:
            mistake += 0.08 * w          # slicks in the wet: easy to slide off
        if self.rng.random() < mistake:
            loss = self.rng.uniform(1.0, 4.0)
            t += loss
            self._event("mistake", f"{car.code} runs wide and loses {loss:.1f}s", car.code)
        return t

    @property
    def vsc(self) -> bool:
        return self.t < self.vsc_until

    @property
    def neutral(self) -> str | None:
        """'sc', 'vsc' or None."""
        return "sc" if self.sc else "vsc" if self.vsc else None

    def _needs_two(self, used: set[str]) -> bool:
        """Two-compound rule: a Grand Prix needs two dry compounds (unless a wet tyre was used);
        a sprint doesn't, so most cars run it without stopping."""
        return not self.cfg.sprint and needs_second_compound(used)

    def _pit_mult(self) -> float:
        return SC_PIT_MULT if self.sc else VSC_PIT_MULT if self.vsc else 1.0

    def stop_cost(self) -> float:
        """Time a pit stop costs right now compared with staying out at racing speed."""
        slow = SC_SLOW if self.sc else VSC_SLOW if self.vsc else 1.0
        return self.track.pit_loss_s * self._pit_mult() / slow

    def _event(self, kind: str, text: str, code: str | None = None) -> None:
        lap = min(self.cfg.laps, max(1, self.leader().lap + 1)) if self.cars else 1
        ev = {"t": round(self.t, 1), "lap": lap, "kind": kind, "text": text, "code": code}
        self.events.append(ev)
        self.event_log.append(ev)

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
        if car.controlled and mode != car.mode:
            self._event("radio", f"{car.code}: {RADIO[mode]}", car.code)
        car.mode = mode
        return True

    def team_order(self, team: str) -> str | None:
        """Tell the car ahead to let its team-mate past. Returns an error, or None if the order
        was given (the driver may still ignore it)."""
        cars = sorted((c for c in self.cars if c.driver.team == team and c.running and c.pit_timer <= 0),
                      key=lambda c: -c.dist)
        if len(cars) < 2:
            return "Both drivers need to be out on track"
        ahead, behind = cars[0], cars[1]
        if ahead.yield_to:
            return f"{ahead.code} is already letting {ahead.yield_to} through"
        if (ahead.dist - behind.dist) * ahead.lap_target > SWAP_MAX_GAP_S:
            return f"They're too far apart to swap (more than {SWAP_MAX_GAP_S:.0f}s)"
        behind.last_order_lap = ahead.last_order_lap = behind.lap
        self._event("radio", f"Team to {ahead.code}: let {behind.code} through", ahead.code)
        if self.inc_rng.random() < self.swap_chance(ahead):
            ahead.yield_to, ahead.yield_until = behind.code, self.t + ahead.lap_target * 1.2
            behind.passing = ahead.code
            self._event("radio", f"{ahead.code}: \"Copy, letting {behind.code} by.\"", ahead.code)
        else:
            reply = ("No way, I'm faster!" if ahead.driver.loyalty < 0.45 else
                     "Give me a lap, I can pull away." if ahead.driver.loyalty < 0.7 else
                     "Sorry, couldn't find a safe place.")
            self._event("radio", f"{ahead.code} ignores the order: \"{reply}\"", ahead.code)
        return None

    def swap_chance(self, ahead: Car) -> float:
        """Chance a driver obeys an order to let their team-mate by: loyal drivers usually do,
        and nobody likes handing over a podium or the lead."""
        pos = self.order().index(ahead) + 1
        p = 0.25 + 0.7 * ahead.driver.loyalty - (0.15 if pos <= 3 else 0.0) - (0.15 if pos == 1 else 0.0)
        return max(0.05, min(0.97, p))

    # ----------------------------------------------------------- simulation
    def step(self, dt: float) -> None:
        while dt > 1e-9 and not self.finished:
            h = min(dt, MAX_SUBSTEP_S)
            self._substep(h)
            dt -= h
            if self.t >= self._next_frame or self.finished:
                self._record_frame()

    def _record_frame(self) -> None:
        """One replay frame: race time, each car's distance in laps (lap + distance fraction,
        in grid order), a status letter per car (r running, p in the pit box, f finished, x out)
        and the race flag: g green, s Safety Car, v Virtual Safety Car."""
        if not self.cars:
            return
        dists, status = [], []
        for c in self.cars:
            whole = math.floor(c.frac)
            dists.append(round(c.lap + whole + self.track.dist_frac(c.frac - whole), 3))
            status.append("f" if c.finished else "x" if c.retired else "p" if c.pit_timer > 0 else "r")
        flag = "s" if self.sc else "v" if self.vsc else "g"
        self.frames.append([round(self.t, 2), dists, "".join(status), flag])
        self._next_frame = self.t + REPLAY_FRAME_S

    def replay(self) -> dict:
        """Everything the lap-by-lap replay and the race charts need (see sim/replay.py)."""
        from .replay import build_replay
        if self.finished and self._replay is not None:
            return self._replay
        out = build_replay(self)
        if self.finished:
            self._replay = out
        return out

    @property
    def finished(self) -> bool:
        return all(not c.running for c in self.cars)

    def _substep(self, dt: float) -> None:
        self.t += dt
        self.weather.step(self.t, dt)
        self._weather_messages()
        slow = VSC_SLOW if self.vsc else 1.0
        sc_rates = self._sc_rates() if self.sc else {}
        for car in self.cars:
            if not car.running:
                continue
            if car.pit_timer > 0:
                car.pit_timer -= dt
                if car.pit_timer <= 0:
                    car.pit_timer = 0
                    self._event("pit-out", f"{car.code} rejoins on {COMPOUNDS[car.tyre].name}s", car.code)
                continue
            rate = sc_rates[car.code] if self.sc else 1.0 / (car.lap_target * slow)
            if car.yield_to:
                mate = self.car(car.yield_to)
                if (mate is None or not mate.running or self.t > car.yield_until
                        or mate.dist > car.dist + 2 * MIN_GAP_S / car.lap_target):
                    car.yield_to = None
                else:
                    rate /= YIELD_SLOW
            car.frac += dt * rate
        self._interactions(dt)
        for car in self.cars:
            if car.running and car.pit_timer <= 0:
                self._sectors_and_line(car)

    def _sc_rates(self) -> dict[str, float]:
        """Laps per second for each car behind the Safety Car: the leader follows the SC and
        everyone else closes up to the car ahead, then holds station."""
        sc_lap = (self.track.base_lap_s + 6.0 * self.weather.wetness) * SC_SLOW
        order = sorted((c for c in self.cars if c.running and c.pit_timer <= 0), key=lambda c: -c.dist)
        rates, prev = {}, None
        for car in order:
            rate = 1.0 / sc_lap
            if prev is not None and (prev.dist - car.dist) * sc_lap > SC_GAP_S:
                rate = max(rate, 1.0 / (car.lap_target * SC_CATCH))
            rates[car.code] = rate
            prev = car
        return rates

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
            neutral = self.neutral is not None
            drs = (not neutral and self.t >= self.drs_after_t and car.lap >= 2 and self.track.in_drs(car.frac))
            rate = 0.0 if neutral else max(0.0, 0.01 + 0.22 * delta + (0.35 if drs else 0.0))
            if car.yield_to == ahead.code:
                rate = 0.0                       # let the team-mate go, don't re-pass
            rate *= 0.6 + 0.8 * car.driver.racecraft - 0.4 * ahead.driver.racecraft
            rate *= self.track.overtake_mult * (1 + 0.4 * self.weather.wetness)
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
        car.wear += (wear_mult * self.track.wear_mult * (0.4 if self.neutral else 1.0)
                     * wear_factor(car.tyre, self.weather.wetness)
                     * (self.cfg.team_wear or {}).get(car.driver.team, 1.0))
        car.fuel = max(0.0, car.fuel - FUEL_PER_LAP_KG)
        record = {"code": car.code, "lap": car.lap, "t": round(t_cross, 3), "time": round(lap_time, 3), "tyre": car.tyre,
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
            if self._needs_two(car.used):
                car.penalty_s = 10.0
                self._event("penalty", f"{car.code} +10s: only one compound used", car.code)
            return

        if self.rng.random() < self.cfg.dnf_chance_per_lap * (self.cfg.team_dnf or {}).get(car.driver.team, 1.0):
            car.retired = True
            self._event("dnf", f"{car.code} retires with a power unit failure", car.code)
            if self.inc_rng.random() < 0.4:      # stopped somewhere awkward
                self._incident("stopped", f"{car.code} has stopped on track")
            return

        crash = (self.cfg.crash_chance_per_lap * (1 + 3 * self.weather.wetness)
                 * (1.6 if car.mode == "push" else 1.0) * (0.2 if self.neutral else 1.0))
        if self.inc_rng.random() < crash:
            car.retired = True
            self._event("crash", f"{car.code} crashes out!", car.code)
            self._incident("crash", f"{car.code} has crashed")
            return

        self._race_control(car)
        if not car.controlled:
            self._ai_radio(car)
        self._maybe_pit(car)
        car.lap_target = self._lap_time(car)

    def _race_control(self, car: Car) -> None:
        if car is not self.leader() or car.lap <= self.leader_lap_seen:
            return
        self.leader_lap_seen = car.lap
        laps_left = self.cfg.laps - car.lap
        if self.sc:
            if car.lap >= self.sc_end_lap:
                self.sc, self.sc_in = False, False
                self.drs_after_t = self.t + self.track.base_lap_s
                self._event("green", "Green flag! The Safety Car is in and racing resumes")
            elif car.lap == self.sc_end_lap - 1 and not self.sc_in:
                self.sc_in = True
                self._event("sc", "Safety Car in this lap")
            return
        if (3 <= car.lap and laps_left > 3
                and self.inc_rng.random() < self.cfg.vsc_chance_per_lap * (1 + 3 * self.weather.rain)):
            self._incident("debris", "debris on track")

    def _incident(self, kind: str, reason: str) -> None:
        """Something on track: bring out a Safety Car or a VSC, if one isn't out already."""
        lead = self.leader()
        if (self.neutral or self.chequered or self.vsc_count + self.sc_count >= MAX_NEUTRAL
                or self.cfg.laps - lead.lap <= 2):
            return
        if self.inc_rng.random() < SC_SHARE[kind]:
            self.sc, self.sc_in = True, False
            self.sc_end_lap = lead.lap + self.inc_rng.randint(3, 5)
            self.sc_count += 1
            self._event("sc", f"Safety Car deployed: {reason}. The field will bunch up; pit stops are cheap")
        else:
            self.vsc_until = self.t + self.track.base_lap_s * self.inc_rng.uniform(1.5, 2.5)
            self.vsc_count += 1
            self._event("vsc", f"Virtual Safety Car deployed: {reason}. Gaps frozen; pit stops a bit cheaper")
        for c in self.cars:
            c.advice = self.box_advice(c) if c.running else None

    def box_advice(self, car: Car) -> dict:
        """Should this car take a cheap stop at the end of this lap? Compares the best plan
        staying out with the best plan after a stop now at today's reduced cost."""
        n = self.cfg.laps - car.lap - 1          # laps left after a stop at the end of this lap
        now, full = self.stop_cost(), self.track.pit_loss_s
        out = {"box": False, "compound": car.tyre, "saving_s": 0.0, "cost_s": round(now, 1),
               "usual_s": round(full, 1)}
        if n < 2 or car.pit_timer > 0:
            return out
        compound, gain = self._weather_gain(car, n)
        if compound and gain > 0.3:
            return {**out, "box": True, "compound": compound, "saving_s": round(float(gain * n - now), 1)}
        if car.tyre in WET:
            pct = COMPOUNDS[car.tyre].wear_pct(car.wear)
            return {**out, "box": pct > 50, "saving_s": round(max(0.0, (pct - 50) / 10), 1)}
        m = self.track.wear_mult
        stay = best_plan(n, full, car.tyre, car.wear + m, car.used, wear_mult=m,
                         require_two=self._needs_two(car.used)).cost_s
        best_c, best = car.tyre, math.inf
        for c in DRY:
            used = car.used | {c}
            cost = now + best_plan(n, full, c, 0.0, used, wear_mult=m,
                                   require_two=self._needs_two(used)).cost_s
            if cost < best:
                best_c, best = c, cost
        if not math.isfinite(stay):
            stay = best + full          # staying out breaks the two-compound rule
        saving = float(stay - best)
        return {**out, "box": bool(saving > 1.0), "compound": best_c, "saving_s": round(saving, 1)}

    def _ai_radio(self, car: Car) -> None:
        """AI pace calls: save tyres when a stint is running long, push in the closing laps,
        and ask a faster team-mate to be let through."""
        laps_left = self.cfg.laps - car.lap
        car.mode = "normal"
        if self.neutral or car.tyre not in DRY or ideal_type(self.weather.wetness) != "dry":
            return
        stops = car.plan.stops if car.plan else []
        stint_left = stops[0][0] - (car.lap - car.plan_base) if stops else laps_left
        projected = car.wear + max(0, stint_left) * self.track.wear_mult
        cliff = COMPOUNDS[car.tyre].cliff_lap
        if not stops and stint_left > 2 and projected > cliff + 3:
            car.mode = "conserve"
        elif laps_left <= 5 and not stops and projected < cliff - 2:
            car.mode = "push"
        mate = next((c for c in self.cars if c.driver.team == car.driver.team and c is not car), None)
        if (mate and mate.running and not mate.controlled and mate.pit_timer <= 0 and laps_left > 3
                and car.lap - car.last_order_lap > 5 and 0 < mate.dist - car.dist
                and (mate.dist - car.dist) * car.lap_target < 1.5
                and self._pace(car) < self._pace(mate) - 0.35 and self.inc_rng.random() < 0.5):
            self.team_order(car.driver.team)

    def _pace(self, car: Car) -> float:
        return car.driver.pace_s + car.form_s + COMPOUNDS[car.tyre].delta(car.wear) + PACE_MODES[car.mode][0]

    def _maybe_pit(self, car: Car) -> None:
        laps_left = self.cfg.laps - car.lap
        compound = None
        if car.controlled:
            compound, car.pit_request = car.pit_request, None
        else:
            compound = self._weather_call(car, laps_left)
            dry_now = ideal_type(self.weather.wetness) == "dry"
            due = car.plan.stops[0] if car.plan and car.plan.stops else None
            if compound is None and due and car.tyre in DRY and dry_now:
                n, c = due
                laps_to_stop = n - (car.lap - car.plan_base)
                if laps_to_stop <= 0:
                    compound = c
            if (compound is None and self.neutral and car.advice and car.advice["box"]
                    and car.advice["saving_s"] > self.inc_rng.uniform(0.0, 6.0)):
                compound = car.advice["compound"]        # take the cheap stop
            worn = car.wear > COMPOUNDS[car.tyre].cliff_lap + 2 and laps_left > 3
            if compound is None and worn and car.tyre in WET:
                compound = car.tyre                       # fresh set of the same wet tyre
            elif compound is None and dry_now and car.tyre in DRY and (
                    worn or (self._needs_two(car.used) and laps_left == 2)):
                plan = best_plan(laps_left, self.track.pit_loss_s, car.tyre, car.wear, car.used,
                                 wear_mult=self.track.wear_mult, require_two=self._needs_two(car.used))
                compound = plan.stops[0][1] if plan.stops else ("H" if car.tyre != "H" else "M")
        if compound is None or laps_left < 1:
            return
        loss = self.track.pit_loss_s * self._pit_mult()
        stationary = 2.3 + abs(self.rng.gauss(0, 0.3))
        stationary = max(1.6, stationary - (self.cfg.team_pit or {}).get(car.driver.team, 0.0))
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
        car.advice = None
        car.yield_to = None
        if not car.controlled:
            # Re-plan the rest of the race from here (dry tyres only; in the wet the weather decides).
            car.plan = (best_plan(laps_left, self.track.pit_loss_s, compound, 0.0, car.used,
                                  wear_mult=self.track.wear_mult, require_two=self._needs_two(car.used))
                        if compound in DRY else Plan(None, [], 0.0))
            car.plan_base = car.lap
        self._event("pit", f"{car.code} pits: {COMPOUNDS[old].name} -> {COMPOUNDS[compound].name}"
                    + (" (cheap stop under the Safety Car)" if self.sc else
                       " (cheap stop under VSC)" if self.vsc else ""), car.code)

    # -------------------------------------------------------------- weather
    def _dry_pick(self, car: Car, laps_left: int) -> str:
        eff = laps_left * self.track.wear_mult
        pick = "S" if eff <= 14 else "M" if eff <= 26 else "H"
        if self._needs_two(car.used | {pick}) and not car.used & set(WET):
            pick = next(c for c in ("M", "H", "S") if c not in car.used)
        return pick

    def _weather_options(self, car: Car, laps_left: int) -> dict[str, str]:
        """Best tyre of each type for this car: dry (which slick), 'I' and 'W'."""
        dry = car.tyre if car.tyre in DRY and car.wear < COMPOUNDS[car.tyre].cliff_lap else self._dry_pick(car, laps_left)
        return {"dry": dry, "I": "I", "W": "W"}

    def _weather_gain(self, car: Car, laps_left: int) -> tuple[str | None, float]:
        """(compound to switch to, seconds per lap it should gain) judged on the track now and
        a little ahead, as a team's weather radar would."""
        w_now = self.weather.wetness
        w_soon = self.weather.wetness_at(self.t, self.t + car.weather_look * car.lap_target)
        w = (w_now + w_soon) / 2
        opts = self._weather_options(car, laps_left)
        best_kind = min(opts, key=lambda k: condition_delta(opts[k], w))
        cur_kind = "dry" if car.tyre in DRY else car.tyre
        if best_kind == cur_kind:
            return None, 0.0
        gain = condition_delta(car.tyre, w) - condition_delta(opts[best_kind], w)
        return opts[best_kind], gain

    def _weather_call(self, car: Car, laps_left: int) -> str | None:
        compound, gain = self._weather_gain(car, laps_left)
        # Only switch when the time won back over the remaining laps beats most of the stop.
        stop_cost = self.stop_cost()
        if compound and gain > car.weather_threshold and gain * max(laps_left, 1) > 0.8 * stop_cost:
            return compound
        return None

    def _weather_messages(self) -> None:
        raining = self.weather.rain > (0.02 if self._rain_on else 0.05)
        if raining != self._rain_on:
            self._rain_on = raining
            self._event("weather", f"Rain is falling ({rain_label(self.weather.rain)})" if raining
                        else "The rain has stopped. The track will start to dry")
        w = self.weather.wetness
        ideal = self._ideal
        # small hysteresis so messages don't flicker around the crossover points
        if ideal == "dry" and w > INTER_FROM + 0.02:
            ideal = "W" if w >= WET_FROM else "I"
        elif ideal == "I" and (w < INTER_FROM - 0.02 or w > WET_FROM + 0.02):
            ideal = "dry" if w < INTER_FROM else "W"
        elif ideal == "W" and w < WET_FROM - 0.02:
            ideal = "I" if w >= INTER_FROM else "dry"
        if ideal != self._ideal:
            self._ideal = ideal
            self._event("weather", {"dry": "Track drying: slicks are now the fastest tyre",
                                    "I": "Track is wet: Intermediates are now the fastest tyre",
                                    "W": "Standing water: full Wets are now the fastest tyre"}[ideal])

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
                "penalty": c.penalty_s, "yielding": c.yield_to is not None,
                "advice": c.advice if (c.controlled and c.running and self.neutral) else None,
            })
            prev = c
        events, self.events = self.events, []
        laps, self.new_laps = self.new_laps, []
        return {
            "t": round(self.t, 2), "lap": min(self.cfg.laps, lead.lap + (0 if lead.finished else 1)),
            "total_laps": self.cfg.laps, "vsc": self.vsc, "chequered": self.chequered,
            "sc": self.sc, "sc_in": self.sc_in, "neutral": self.neutral,
            "neutral_id": self.vsc_count + self.sc_count,
            "finished": self.finished,
            "weather": {**self.weather.snapshot(self.t), "ideal": self._ideal},
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
        compound, gain = self._weather_gain(car, laps_left)
        if compound and gain > 0.8:
            return {"code": code, "weather_call": compound, "gain_s": round(gain, 1), "stops": [], "cost_s": 0.0}
        if car.tyre in WET:
            return {"code": code, "stops": [], "cost_s": 0.0, "stay_wet": True}
        plan = best_plan(laps_left, self.track.pit_loss_s, car.tyre, car.wear, car.used,
                         wear_mult=self.track.wear_mult, require_two=self._needs_two(car.used))
        return {"code": code, "stops": [{"in_laps": n, "lap": car.lap + n, "compound": c} for n, c in plan.stops],
                "cost_s": round(plan.cost_s, 1)}


def fmt_time(s: float) -> str:
    m, sec = divmod(s, 60)
    return f"{int(m)}:{sec:06.3f}"
