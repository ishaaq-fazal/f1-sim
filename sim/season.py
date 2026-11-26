"""Season mode: a 22-round championship, one race at each circuit, scored with F1 points.
Six rounds are sprint weekends: a short sprint race (top 8 score 8-7-6-5-4-3-2-1) before the
Grand Prix.

Pure Python (no web or database code), so the rules can be tested on their own.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

# One round at each circuit, ordered so street, fast and high-wear tracks are mixed through the year.
SCHEDULE: tuple[str, ...] = (
    "harbourline", "sandridge", "riverside", "monteverde", "falconridge", "redcliff",           # 1-6
    "vinehill", "alderwood", "lanternquay", "thornbury", "cedarvale", "frostholm",           # 7-12
    "stonebridge", "glacier", "pinecrest", "copperbasin", "sunreef", "saltmarsh",            # 13-18
    "highmoor", "neon", "ashcombe", "brightwater",                                          # 19-22
)
ROUNDS = len(SCHEDULE)

# Points for P1..P10, as in Formula 1. Retired cars score nothing.
POINTS: tuple[int, ...] = (25, 18, 15, 12, 10, 8, 6, 4, 2, 1)


# Sprint weekends: a short race on Saturday before the Grand Prix, at circuits where passing is
# possible (rounds 3, 6, 10, 14, 18 and 21, spread through the year).
SPRINT_TRACKS: tuple[str, ...] = ("riverside", "redcliff", "thornbury", "glacier", "saltmarsh", "ashcombe")
# Points for P1..P8 in a sprint, as in Formula 1.
SPRINT_POINTS: tuple[int, ...] = (8, 7, 6, 5, 4, 3, 2, 1)
SPRINT_LAP_SHARE = 1 / 3          # an F1 sprint is about a third of the Grand Prix distance
MIN_SPRINT_LAPS = 4


def points_for(position: int, retired: bool = False, sprint: bool = False) -> int:
    table = SPRINT_POINTS if sprint else POINTS
    if retired or position < 1 or position > len(table):
        return 0
    return table[position - 1]


def is_sprint_round(round_no: int) -> bool:
    return 1 <= round_no <= ROUNDS and SCHEDULE[round_no - 1] in SPRINT_TRACKS


def sprint_rounds() -> list[int]:
    return [n for n in range(1, ROUNDS + 1) if is_sprint_round(n)]


def sprint_laps(race_laps: int) -> int:
    """Sprint distance for a season raced over race_laps laps."""
    return max(MIN_SPRINT_LAPS, round(race_laps * SPRINT_LAP_SHARE))


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

    Each row needs .driver, .team, .position, .retired and .points attributes. Rows with a true
    .sprint attribute are sprint results: their points count, but wins and countback only use
    Grand Prix finishes (as in F1)."""
    drivers: dict[str, Standing] = {}
    teams: dict[str, Standing] = {}
    for r in rows:
        d = drivers.setdefault(r.driver, Standing(r.driver, r.team))
        t = teams.setdefault(r.team, Standing(r.team, r.team))
        d.points += r.points
        t.points += r.points
        if getattr(r, "sprint", False):
            continue
        if not r.retired:
            d.finishes.append(r.position)
            t.finishes.append(r.position)
    return (sorted(drivers.values(), key=Standing.sort_key),
            sorted(teams.values(), key=Standing.sort_key))


# ---------------------------------------------------------------- weather for each round
# Each round's weather is rolled from the round's seed, so it is fixed before the race (the
# forecast and the race always agree) but nobody can choose it.
# Roughly one round in three is fully dry; the rest bring rain in some form.
WEATHER_ODDS = (("dry", 0.32), ("changeable", 0.24), ("showers", 0.18), ("drying", 0.12), ("wet", 0.14))


def sprint_seed(seed: int) -> int:
    """Seed for a round's sprint, made from the round's seed: the sprint has its own weather and
    race, fixed in advance like the Grand Prix's."""
    return random.Random(seed ^ 0x5B_21A7).randrange(2**31)


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
    def around(t: float) -> tuple[int, int]:
        lap = max(1, min(laps, round(t / track.base_lap_s)))
        return max(1, lap - fuzz.randint(2, 4)), min(laps, lap + fuzz.randint(2, 4))

    if wet_start:
        chance = 100
        first = w.spells[0]
        later = [sp for sp in w.spells[1:] if sp.start < race_s]
        if first.end < race_s * 0.6 and not later:
            lo, hi = around(first.end)
            summary = f"Wet start, rain easing off around laps {lo}-{hi}: the track will dry"
        else:
            summary = "Raining at the start: wet track"
    elif in_race:
        sp = min(in_race, key=lambda s: s.start)
        chance = int(min(95, max(45, 72 + fuzz.gauss(0, 10))))
        lo, hi = around(sp.start)
        if len(in_race) >= 2:
            summary = f"Dry start, showers on and off from around laps {lo}-{hi}"
        else:
            summary = f"Dry start, rain likely around laps {lo}-{hi}"
    else:
        chance = int(max(0, min(30, 8 + fuzz.gauss(0, 6))))
        summary = "Dry and settled"
    return {"summary": summary, "rain_chance": chance, "wet_start": wet_start,
            "track_wetness": round(w.wetness, 2), "next_20_min": w.forecast(0.0)}
