"""Season mode: a 10-round championship, one race at each circuit, scored with F1 points.

Pure Python (no web or database code), so the rules can be tested on their own.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

# One round at each circuit, ordered so street, fast and high-wear tracks are mixed through the year.
SCHEDULE: tuple[str, ...] = ("harbourline", "monteverde", "riverside", "sandridge", "cedarvale",
                             "glacier", "alderwood", "neon", "saltmarsh", "highmoor")
ROUNDS = len(SCHEDULE)

# Points for P1..P10, as in Formula 1. Retired cars score nothing.
POINTS: tuple[int, ...] = (25, 18, 15, 12, 10, 8, 6, 4, 2, 1)


def points_for(position: int, retired: bool = False) -> int:
    if retired or position < 1 or position > len(POINTS):
        return 0
    return POINTS[position - 1]


def track_for_round(round_no: int) -> str:
    """Circuit for a 1-based round number."""
    if not 1 <= round_no <= ROUNDS:
        raise ValueError("no such round")
    return SCHEDULE[round_no - 1]


@dataclass
class Standing:
    key: str                       # driver code or team id
    team: str
    points: int = 0
    finishes: list[int] = field(default_factory=list)   # positions of classified finishes

    @property
    def wins(self) -> int:
        return self.finishes.count(1)

    @property
    def podiums(self) -> int:
        return sum(1 for p in self.finishes if p <= 3)

    def sort_key(self):
        # Most points first; ties broken on countback (most wins, then most 2nds, ...), like F1.
        counts = [-self.finishes.count(p) for p in range(1, 21)]
        return (-self.points, *counts, self.key)


def standings(rows) -> tuple[list[Standing], list[Standing]]:
    """Drivers' and teams' standings from result rows.

    Each row needs .driver, .team, .position, .retired and .points attributes."""
    drivers: dict[str, Standing] = {}
    teams: dict[str, Standing] = {}
    for r in rows:
        d = drivers.setdefault(r.driver, Standing(r.driver, r.team))
        t = teams.setdefault(r.team, Standing(r.team, r.team))
        d.points += r.points
        t.points += r.points
        if not r.retired:
            d.finishes.append(r.position)
            t.finishes.append(r.position)
    return (sorted(drivers.values(), key=Standing.sort_key),
            sorted(teams.values(), key=Standing.sort_key))


# ---------------------------------------------------------------- weather for each round
# Each round's weather is rolled from the round's seed, so it is fixed before the race (the
# forecast and the race always agree) but nobody can choose it.
WEATHER_ODDS = (("dry", 0.6), ("changeable", 0.3), ("wet", 0.1))


def round_weather(seed: int) -> str:
    r = random.Random(seed ^ 0x5EA5_0E).random()
    for mode, p in WEATHER_ODDS:
        if r < p:
            return mode
        r -= p
    return WEATHER_ODDS[-1][0]


def round_forecast(seed: int, track_id: str, laps: int) -> dict:
    """The pre-race forecast for a round, built from the same weather the race will use."""
    from .track import get_track
    from .weather import Weather

    mode = round_weather(seed)
    track = get_track(track_id)
    race_s = laps * track.base_lap_s * 1.05
    # Same random draws as RaceEngine.__init__, so this is exactly the race's weather.
    w = Weather(mode, random.Random(random.Random(seed).random()), race_s)
    fuzz = random.Random(seed ^ 0xF0_CA57)
    wet_start = w.wetness > 0.3
    in_race = [sp for sp in w.spells if sp.end > 0 and sp.start < race_s]
    if wet_start:
        chance, summary = 100, "Raining at the start: wet track"
    elif in_race:
        sp = min(in_race, key=lambda s: s.start)
        chance = int(min(95, max(45, 72 + fuzz.gauss(0, 10))))
        lap = max(1, round(sp.start / track.base_lap_s))
        lo, hi = max(1, lap - fuzz.randint(2, 4)), min(laps, lap + fuzz.randint(2, 4))
        summary = f"Dry start, rain likely around laps {lo}-{hi}"
    else:
        chance = int(max(0, min(30, 8 + fuzz.gauss(0, 6))))
        summary = "Dry and settled"
    return {"summary": summary, "rain_chance": chance, "wet_start": wet_start,
            "track_wetness": round(w.wetness, 2), "next_20_min": w.forecast(0.0)}
