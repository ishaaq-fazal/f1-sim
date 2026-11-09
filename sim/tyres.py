"""Tyre compound model: pace offset, degradation and the wear cliff."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Compound:
    code: str
    name: str
    color: str
    offset_s: float        # pace vs medium on a fresh set (negative = faster)
    deg_s: float           # linear loss per lap of wear
    deg_quad: float        # quadratic loss per lap^2 of wear
    cliff_lap: float       # wear (in laps) where grip falls away
    cliff_s: float         # extra loss per lap past the cliff (grows ^1.5)

    def delta(self, wear: float) -> float:
        """Lap time delta in seconds for this compound at a given wear."""
        d = self.offset_s + self.deg_s * wear + self.deg_quad * wear * wear
        if wear > self.cliff_lap:
            d += self.cliff_s * (wear - self.cliff_lap) ** 1.5
        return d

    def wear_pct(self, wear: float) -> float:
        return max(0.0, min(100.0, 100.0 * wear / self.cliff_lap))


COMPOUNDS: dict[str, Compound] = {
    "S": Compound("S", "Soft", "#ff3b3b", -0.75, 0.085, 0.0026, 16, 0.55),
    "M": Compound("M", "Medium", "#ffd23f", 0.0, 0.052, 0.0012, 27, 0.45),
    "H": Compound("H", "Hard", "#f2f2f2", 0.48, 0.030, 0.0006, 40, 0.40),
    # Wet-weather tyres: offset is 0 because their pace comes from condition_delta().
    "I": Compound("I", "Intermediate", "#3fbf4f", 0.0, 0.060, 0.0015, 24, 0.50),
    "W": Compound("W", "Wet", "#2f8cff", 0.0, 0.045, 0.0010, 30, 0.45),
}
DRY = ("S", "M", "H")
WET = ("I", "W")

# Track wetness 0..1 where the best tyre changes (roughly where the curves below cross).
INTER_FROM = 0.18
WET_FROM = 0.75


def condition_delta(code: str, wetness: float) -> float:
    """Extra lap time (s) for a compound on a track this wet, on top of the general
    slowdown every car gets in the rain. Each tyre is quickest in its own window:
    slicks when dry, Intermediates on a damp/wet track, full Wets in heavy water."""
    w = max(0.0, min(1.0, wetness))
    if code in DRY:
        return 30.0 * max(0.0, w - 0.08) ** 1.3          # no grip in standing water
    if code == "I":
        overheat = 4.0 * max(0.0, (0.15 - w) / 0.15)      # too soft for a dry track
        return 1.6 + overheat + 10.0 * max(0.0, w - 0.6) ** 1.2
    if code == "W":
        overheat = 8.0 * max(0.0, (0.5 - w) / 0.5)
        return 3.2 + overheat
    raise KeyError(code)


def wear_factor(code: str, wetness: float) -> float:
    """Wear multiplier: wet tyres overheat and shred on a drying track; slicks wear less in the wet."""
    w = max(0.0, min(1.0, wetness))
    if code in DRY:
        return 1.0 - 0.4 * w
    if code == "I":
        return 1.0 + 2.0 * max(0.0, (0.15 - w) / 0.15)
    return 1.0 + 2.5 * max(0.0, (0.45 - w) / 0.45)


def ideal_type(wetness: float) -> str:
    """'dry', 'I' or 'W': the kind of tyre that is fastest on this track."""
    return "W" if wetness >= WET_FROM else "I" if wetness >= INTER_FROM else "dry"


def needs_second_compound(used: set[str]) -> bool:
    """Two-compound rule: two different dry compounds, unless wet tyres were used."""
    return not (used & set(WET)) and len(used & set(DRY)) < 2

# Pace modes: (lap time delta in s, tyre wear multiplier)
PACE_MODES: dict[str, tuple[float, float]] = {
    "push": (-0.35, 1.40),
    "normal": (0.0, 1.0),
    "conserve": (0.45, 0.65),
}
