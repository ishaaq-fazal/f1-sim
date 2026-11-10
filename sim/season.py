"""Season mode: a 10-round championship, one race at each circuit, scored with F1 points.

Pure Python (no web or database code), so the rules can be tested on their own.
"""
from __future__ import annotations

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
