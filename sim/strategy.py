"""Pit strategy optimiser: picks stint lengths and compounds that minimise race time.

Used by AI teams for their plan, and to give the player a predicted best strategy.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product

import numpy as np

from .tyres import COMPOUNDS

MAX_OVER_CLIFF = 4  # never plan a stint more than this many laps past the cliff


@dataclass
class Plan:
    start_compound: str | None          # only set for a pre-race plan
    stops: list[tuple[int, str]] = field(default_factory=list)  # (pit after N more laps, compound)
    cost_s: float = 0.0

    def describe(self) -> str:
        parts = [self.start_compound] if self.start_compound else []
        parts += [f"L+{n}->{c}" for n, c in self.stops]
        return " ".join(parts)


def _stint_costs(code: str, wear0: float, max_laps: int, wear_mult: float = 1.0) -> np.ndarray:
    """cum[n] = total delta over the first n laps of a stint starting at wear0."""
    c = COMPOUNDS[code]
    per_lap = np.array([c.delta(wear0 + i * wear_mult) for i in range(max_laps)])
    return np.concatenate([[0.0], np.cumsum(per_lap)])


def _max_stint(code: str, wear0: float = 0.0, wear_mult: float = 1.0) -> int:
    return max(1, int((COMPOUNDS[code].cliff_lap + MAX_OVER_CLIFF - wear0) / wear_mult))


def best_plan(
    laps_left: int,
    pit_loss_s: float,
    current: str | None = None,
    wear: float = 0.0,
    used: set[str] | None = None,
    max_stops: int = 2,
    wear_mult: float = 1.0,
    starts: list[str] | None = None,
) -> Plan:
    """Brute-force the best 0/1/2-stop plan for the remaining laps.

    current=None means pre-race: the starting compound is also chosen.
    The plan must use at least two different dry compounds over the whole race.
    wear_mult scales tyre wear per lap for the circuit. starts limits the pre-race
    starting compound choices.
    """
    used = set(used or ())
    codes = list(COMPOUNDS)
    m = wear_mult
    fresh = {c: _stint_costs(c, 0.0, laps_left + 1, m) for c in codes}
    starts = [current] if current else (starts or codes)
    best = Plan(None, [], float("inf"))

    for first in starts:
        w0 = wear if current else 0.0
        first_cost = _stint_costs(first, w0, laps_left + 1, m) if current else fresh[first]
        first_max = _max_stint(first, w0, m)
        seen = used | {first}

        # 0 stops
        if laps_left <= first_max and len(seen) >= 2:
            cost = first_cost[laps_left]
            if cost < best.cost_s:
                best = Plan(None if current else first, [], cost)

        for n_stops in range(1, max_stops + 1):
            for seq in product(codes, repeat=n_stops):
                if len(seen | set(seq)) < 2:
                    continue
                cost = _best_split(laps_left, first_cost, first_max, seq, fresh, m)
                if cost is None:
                    continue
                total, split = cost
                total += n_stops * pit_loss_s
                if total < best.cost_s:
                    stops, acc = [], 0
                    for k, code in zip(split, seq):
                        acc += k
                        stops.append((acc, code))
                    best = Plan(None if current else first, stops, total)
    if best.cost_s == float("inf"):  # e.g. 1 lap left and rule unmet: stay out
        best = Plan(None if current else "M", [], 0.0)
    return best


def _best_split(laps_left, first_cost, first_max, seq, fresh, m=1.0):
    """Return (cost, [first stint length, next stint lengths except last]) or None."""
    if len(seq) == 1:
        best = None
        for k in range(1, laps_left):
            rest = laps_left - k
            if k > first_max or rest > _max_stint(seq[0], 0.0, m):
                continue
            cost = first_cost[k] + fresh[seq[0]][rest]
            if best is None or cost < best[0]:
                best = (cost, [k])
        return best
    best = None
    a, b = seq
    for k1 in range(1, min(first_max, laps_left - 2) + 1):
        for k2 in range(1, min(_max_stint(a, 0.0, m), laps_left - k1 - 1) + 1):
            rest = laps_left - k1 - k2
            if rest > _max_stint(b, 0.0, m):
                continue
            cost = first_cost[k1] + fresh[a][k2] + fresh[b][rest]
            if best is None or cost < best[0]:
                best = (cost, [k1, k2])
    return best
