"""Weather: rain that comes and goes, and a track that gets wetter or dries out.

rain      0..1 intensity right now (0 = none, ~0.3 light, ~0.6 moderate, 0.8+ heavy)
wetness   0..1 how much water is on the track. It follows the rain quickly when it rains
          and dries slowly (several laps) once it stops.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass

MODES = ("dry", "wet", "changeable", "showers", "drying")
RAMP_S = 90.0            # rain fades in/out over this long
WETTING_S = 100.0        # time for full rain to soak a dry track
DRYING_S = 600.0         # time for a soaked track to dry (about 6-7 laps)


@dataclass
class RainSpell:
    start: float
    end: float
    peak: float


def rain_label(rain: float) -> str:
    if rain < 0.03:
        return "none"
    return "light" if rain < 0.4 else "moderate" if rain < 0.7 else "heavy"


class Weather:
    def __init__(self, mode: str, rng: random.Random, race_s: float):
        if mode not in MODES:
            raise ValueError(mode)
        self.mode = mode
        self.spells: list[RainSpell] = []
        self.wetness = 0.0
        if mode == "wet":
            peak = rng.uniform(0.55, 0.95)
            # Usually rains most of the race; sometimes it stops and the track dries.
            end = race_s * rng.uniform(0.45, 1.4)
            self.spells.append(RainSpell(-3600.0, end, peak))
            self.wetness = min(1.0, peak * 1.1)
        elif mode == "changeable":
            starts = sorted(rng.uniform(0.12, 0.7) * race_s for _ in range(rng.choice((1, 1, 2))))
            for st in starts:
                if self.spells and st < self.spells[-1].end + 300:
                    continue
                self.spells.append(RainSpell(st, st + rng.uniform(360, 1200), rng.uniform(0.3, 0.9)))
        elif mode == "showers":
            # Several short showers that come and go: the track never quite settles.
            t = race_s * rng.uniform(0.05, 0.3)
            for _ in range(rng.randint(2, 4)):
                if t > race_s * 0.92:
                    break
                dur = rng.uniform(150, 420)
                self.spells.append(RainSpell(t, t + dur, rng.uniform(0.2, 0.6)))
                t += dur + rng.uniform(240, 0.3 * race_s)
        elif mode == "drying":
            # Wet at the start, but the rain stops early and the track dries out.
            peak = rng.uniform(0.4, 0.75)
            self.spells.append(RainSpell(-3600.0, race_s * rng.uniform(0.08, 0.3), peak))
            self.wetness = min(1.0, peak * 1.1)
        self._wobble = rng.uniform(0, 6.28)
        self.rain = self.intensity(0.0)

    # ---------------------------------------------------------------- model
    def intensity(self, t: float) -> float:
        r = 0.0
        for sp in self.spells:
            if sp.start <= t < sp.end:
                fade = min(1.0, (t - sp.start) / RAMP_S, (sp.end - t) / RAMP_S)
                wobble = 1.0 + 0.12 * math.sin(t / 97.0 + self._wobble)   # rain is never steady
                r = max(r, min(1.0, sp.peak * fade * wobble))
        return r

    @staticmethod
    def _advance(wet: float, rain: float, dt: float) -> float:
        target = min(1.0, rain * 1.1)
        if wet < target:
            return min(target, wet + dt * max(rain, 0.15) / WETTING_S)
        return max(target, wet - dt / DRYING_S)

    def step(self, t: float, dt: float) -> None:
        self.rain = self.intensity(t)
        self.wetness = self._advance(self.wetness, self.rain, dt)

    def wetness_at(self, t_now: float, t_future: float) -> float:
        """Predicted track wetness at a future time (what a team's weather radar tells it)."""
        w, t = self.wetness, t_now
        while t < t_future:
            dt = min(10.0, t_future - t)
            t += dt
            w = self._advance(w, self.intensity(t), dt)
        return w

    def forecast(self, t: float) -> list[dict]:
        """Chance of rain over the next 20 minutes, getting less certain further out."""
        out = []
        for minutes in (5, 10, 15, 20):
            ft = t + minutes * 60
            raining = self.intensity(ft) > 0.03
            near = any(abs(ft - sp.start) < 240 or abs(ft - sp.end) < 240 for sp in self.spells)
            if raining:
                chance = 95 - 2 * minutes - (25 if near else 0)
            else:
                chance = (45 if near else 3) + minutes // 2
            out.append({"in_min": minutes, "chance": max(0, min(100, chance))})
        return out

    def snapshot(self, t: float) -> dict:
        return {"mode": self.mode, "rain": round(self.rain, 2), "rain_label": rain_label(self.rain),
                "wetness": round(self.wetness, 2), "forecast": self.forecast(t)}
