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
}

# Pace modes: (lap time delta in s, tyre wear multiplier)
PACE_MODES: dict[str, tuple[float, float]] = {
    "push": (-0.35, 1.40),
    "normal": (0.0, 1.0),
    "conserve": (0.45, 0.65),
}
