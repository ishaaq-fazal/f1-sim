"""Circuit geometry and a speed profile, so cars move realistically round the map.

Positions use two fractions of a lap:
  * time fraction  - share of the lap time elapsed (what the engine advances)
  * dist fraction  - share of the lap distance covered (what the map draws)
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


def catmull_rom_closed(ctrl: np.ndarray, samples: int = 40) -> np.ndarray:
    P = np.asarray(ctrl, dtype=float)
    n = len(P)
    t = np.linspace(0, 1, samples, endpoint=False)[:, None]
    out = []
    for i in range(n):
        p0, p1, p2, p3 = P[i - 1], P[i], P[(i + 1) % n], P[(i + 2) % n]
        out.append(
            0.5 * ((2 * p1) + (-p0 + p2) * t + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t**2
                   + (-p0 + 3 * p1 - 3 * p2 + p3) * t**3)
        )
    return np.vstack(out)


@dataclass
class Track:
    id: str
    name: str
    points: np.ndarray          # (N, 2) metres, closed loop, index 0 = start/finish line
    length_m: float
    base_lap_s: float           # reference lap: medium tyre, empty tank, average driver
    pit_loss_s: float           # time lost by a stop vs staying out (lane + stationary)
    cum_dist: np.ndarray        # (N+1,) dist fraction at each point
    cum_time: np.ndarray        # (N+1,) time fraction at each point
    speed: np.ndarray           # (N,) relative speed profile, m/s at the reference pace
    sector_time: tuple[float, float]   # time fractions of the S1/S2 and S2/S3 lines
    drs_zones: list[tuple[float, float]]  # time-fraction ranges
    wear_mult: float = 1.0      # tyre wear per lap vs average (abrasive surface > 1)
    overtake_mult: float = 1.0  # how easy passing is vs average (tight street circuit < 1)
    blurb: str = ""

    def dist_frac(self, time_frac: float) -> float:
        return float(np.interp(time_frac % 1.0, self.cum_time, self.cum_dist))

    def time_frac(self, dist_frac: float) -> float:
        return float(np.interp(dist_frac % 1.0, self.cum_dist, self.cum_time))

    def in_drs(self, time_frac: float) -> bool:
        f = time_frac % 1.0
        return any(a <= f <= b for a, b in self.drs_zones)

    def to_json(self, max_points: int = 600) -> dict:
        step = max(1, len(self.points) // max_points)
        pts = self.points[::step]
        idx = np.arange(0, len(self.points), step)
        return {
            "id": self.id,
            "name": self.name,
            "length_m": round(self.length_m),
            "wear": self.wear_mult,
            "overtaking": self.overtake_mult,
            "points": np.round(pts, 1).tolist(),
            "dist": np.round(self.cum_dist[idx], 5).tolist(),
            "sectors": [round(self.dist_frac(s), 4) for s in self.sector_time],
            "drs": [[round(self.dist_frac(a), 4), round(self.dist_frac(b), 4)] for a, b in self.drs_zones],
        }


def build_track(id, name, ctrl, length_m, base_lap_s, pit_loss_s, start_near, sector_dist=(0.33, 0.68),
                wear_mult=1.0, overtake_mult=1.0, blurb=""):
    pts = catmull_rom_closed(np.asarray(ctrl, float))
    # Put the start/finish line at the point nearest start_near.
    start = int(np.argmin(np.hypot(*(pts - np.asarray(start_near)).T)))
    pts = np.roll(pts, -start, axis=0)
    seg = np.hypot(*np.diff(np.vstack([pts, pts[:1]]), axis=0).T)
    scale = length_m / seg.sum()
    pts = pts * scale
    pts -= pts.min(axis=0)
    seg = seg * scale
    n = len(pts)

    # Curvature from heading change between neighbouring segments, lightly smoothed.
    d = np.diff(np.vstack([pts, pts[:1]]), axis=0)
    head = np.arctan2(d[:, 1], d[:, 0])
    dh = np.angle(np.exp(1j * (np.roll(head, -1) - head)))
    kappa = np.abs(dh) / np.maximum(seg, 1e-6)
    kernel = np.ones(7) / 7
    kappa = np.convolve(np.concatenate([kappa[-3:], kappa, kappa[:3]]), kernel, "valid")

    v_top, a_lat, a_brake, a_accel = 92.0, 42.0, 48.0, 13.0
    v = np.minimum(v_top, np.sqrt(a_lat / np.maximum(kappa, 1e-6)))
    for _ in range(2):  # accel forwards, brake backwards, twice to close the loop
        for i in range(n):
            v[i] = min(v[i], np.sqrt(v[i - 1] ** 2 + 2 * a_accel * seg[i - 1]))
        for i in range(n - 1, -1, -1):
            j = (i + 1) % n
            v[i] = min(v[i], np.sqrt(v[j] ** 2 + 2 * a_brake * seg[i]))

    dt = seg / v
    cum_dist = np.concatenate([[0.0], np.cumsum(seg)]) / seg.sum()
    cum_time = np.concatenate([[0.0], np.cumsum(dt)]) / dt.sum()
    v = v * (dt.sum() / base_lap_s)  # rescale so the profile matches the reference lap

    # DRS: the two longest stretches near top speed.
    fast = v >= 0.93 * v.max()
    runs, i = [], 0
    while i < n:
        if fast[i]:
            j = i
            while j + 1 < n and fast[j + 1]:
                j += 1
            runs.append((cum_dist[j + 1] - cum_dist[i], i, j))
            i = j + 1
        else:
            i += 1
    runs.sort(reverse=True)
    drs = sorted((float(cum_time[i]), float(cum_time[j + 1])) for _, i, j in runs[:2])

    sector_time = tuple(float(np.interp(s, cum_dist, cum_time)) for s in sector_dist)
    return Track(id, name, pts, float(seg.sum()), base_lap_s, pit_loss_s, cum_dist, cum_time, v, sector_time, drs,
                 wear_mult, overtake_mult, blurb)


# Original circuits (made up, not real venues). Control points are in arbitrary units and are
# rescaled to length_m. Each track starts on its main straight.
HARBOURLINE = [
    (1500, 2000), (900, 2000), (520, 1930), (400, 1700), (400, 800), (600, 450), (1000, 400),
    (1300, 650), (1500, 1000), (1800, 1050), (2100, 800), (2300, 400), (2700, 350), (2900, 600),
    (2850, 1000), (2500, 1250), (2550, 1600), (2800, 1800), (2650, 2000), (2100, 2010),
]

# Tight, walled street circuit: lots of 90-degree corners, short straights.
MONTEVERDE = [
    (1200, 1800), (500, 1800), (420, 1720), (420, 1300), (500, 1220), (800, 1220), (880, 1140),
    (880, 800), (800, 720), (500, 720), (420, 640), (420, 300), (500, 220), (1300, 220),
    (1380, 300), (1380, 700), (1460, 780), (1800, 780), (1880, 860), (1880, 1720), (1800, 1800),
]

# Fast desert circuit: two very long straights, a hairpin and an abrasive surface.
SANDRIDGE = [
    (1600, 1500), (400, 1500), (250, 1420), (230, 1250), (350, 1150), (900, 1100), (1050, 1000),
    (1000, 850), (600, 700), (450, 550), (500, 400), (700, 330), (2400, 330), (2650, 420),
    (2720, 600), (2600, 760), (2350, 800), (2250, 900), (2550, 1100), (2700, 1300), (2600, 1450),
    (2300, 1500),
]

# Flowing parkland circuit: sweeping curves, medium speed.
ALDERWOOD = [
    (1400, 1700), (800, 1700), (450, 1550), (350, 1250), (550, 1000), (500, 750), (300, 550),
    (450, 300), (850, 280), (1100, 450), (1250, 750), (1550, 800), (1750, 550), (2050, 400),
    (2350, 550), (2400, 900), (2150, 1150), (2250, 1450), (2050, 1680),
]

# Fast river-bank ring: long straights joined by a few big corners and one chicane.
RIVERSIDE = [
    (1500, 1700), (600, 1700), (350, 1600), (250, 1350), (300, 1000), (250, 650), (350, 400),
    (600, 300), (1500, 300), (2400, 300), (2650, 400), (2750, 650), (2700, 900), (2500, 1000),
    (2700, 1100), (2750, 1350), (2650, 1600), (2400, 1700),
]

# Long mountain circuit with hairpins; cold air keeps tyre wear low.
GLACIER = [
    (1800, 1900), (600, 1900), (300, 1800), (250, 1500), (500, 1350), (900, 1350), (1000, 1200),
    (800, 1050), (350, 1000), (250, 750), (400, 500), (900, 450), (1300, 250), (1800, 200),
    (2300, 350), (2400, 650), (2100, 850), (2200, 1100), (2700, 1200), (2850, 1500), (2700, 1800),
    (2300, 1900),
]

# Night street circuit round city blocks: 90-degree corners and long boulevards.
NEON = [
    (1600, 1800), (700, 1800), (600, 1700), (600, 1400), (300, 1400), (200, 1300), (200, 500),
    (300, 400), (900, 400), (1000, 500), (1000, 800), (1100, 900), (1500, 900), (1600, 800),
    (1600, 300), (1700, 200), (2100, 200), (2500, 200), (2600, 300), (2600, 1000), (2600, 1700),
    (2500, 1800), (2050, 1800),
]

# High-speed moorland sweepers that punish the tyres.
HIGHMOOR = [
    (1500, 1800), (700, 1750), (350, 1550), (300, 1200), (550, 950), (1000, 900), (1200, 700),
    (1000, 450), (1200, 250), (1700, 250), (2100, 450), (2600, 350), (2900, 600), (2800, 1000),
    (2400, 1150), (2500, 1500), (2200, 1750),
]

# Short, twisty woodland track: no room to pass.
CEDARVALE = [
    (1100, 1500), (500, 1500), (300, 1350), (350, 1100), (650, 1000), (600, 800), (300, 700),
    (300, 400), (550, 250), (900, 350), (1050, 600), (1300, 700), (1500, 450), (1800, 350),
    (2000, 550), (1900, 850), (1650, 950), (1800, 1200), (1700, 1450),
]

# Windswept coastal circuit: two long straights, slow corners, a coarse sea-air surface.
SALTMARSH = [
    (1700, 1600), (400, 1600), (250, 1450), (300, 1250), (700, 1150), (800, 950), (500, 800),
    (300, 600), (400, 350), (700, 300), (2300, 300), (2550, 400), (2600, 650), (2350, 800),
    (2000, 850), (1900, 1000), (2300, 1150), (2600, 1300), (2550, 1550), (2300, 1600),
]

TRACK_SPECS = {
    "harbourline": dict(name="Harbourline Circuit", ctrl=HARBOURLINE, length_m=5120, base_lap_s=88.0,
                        pit_loss_s=21.5, start_near=(1650, 2002), wear_mult=1.0, overtake_mult=1.0,
                        blurb="Balanced harbour-side track with two DRS straights."),
    "monteverde": dict(name="Monteverde Street Circuit", ctrl=MONTEVERDE, length_m=3420, base_lap_s=74.0,
                       pit_loss_s=19.0, start_near=(950, 1800), wear_mult=0.75, overtake_mult=0.35,
                       blurb="Narrow streets and walls: gentle on tyres, very hard to pass. Track position is king."),
    "sandridge": dict(name="Sandridge Raceway", ctrl=SANDRIDGE, length_m=5610, base_lap_s=91.0,
                      pit_loss_s=23.0, start_near=(1100, 1500), wear_mult=1.45, overtake_mult=1.5,
                      blurb="Fast desert circuit with a rough surface: heavy tyre wear, lots of overtaking."),
    "alderwood": dict(name="Alderwood Park", ctrl=ALDERWOOD, length_m=4310, base_lap_s=79.0,
                      pit_loss_s=20.5, start_near=(1100, 1700), wear_mult=1.15, overtake_mult=0.8,
                      blurb="Flowing parkland sweeps that load the tyres; passing takes commitment."),
    "riverside": dict(name="Riverside Ring", ctrl=RIVERSIDE, length_m=4650, base_lap_s=76.0,
                      pit_loss_s=20.0, start_near=(1100, 1700), wear_mult=0.9, overtake_mult=1.35,
                      blurb="Long straights and slipstreams: easy to pass, kind to tyres."),
    "glacier": dict(name="Glacier Pass", ctrl=GLACIER, length_m=6250, base_lap_s=102.0,
                    pit_loss_s=24.0, start_near=(1200, 1900), wear_mult=0.7, overtake_mult=0.9,
                    blurb="The longest lap: mountain hairpins and cold air, so tyres last and one-stops rule."),
    "neon": dict(name="Neon Bay Street Circuit", ctrl=NEON, length_m=5050, base_lap_s=96.0,
                 pit_loss_s=22.0, start_near=(1150, 1800), wear_mult=0.85, overtake_mult=0.55,
                 blurb="Night race round city blocks: walls everywhere and few places to pass."),
    "highmoor": dict(name="Highmoor Circuit", ctrl=HIGHMOOR, length_m=5850, base_lap_s=86.0,
                     pit_loss_s=21.0, start_near=(1100, 1780), wear_mult=1.3, overtake_mult=1.1,
                     blurb="Flat-out moorland sweepers: very fast, hard on tyres, good racing."),
    "cedarvale": dict(name="Cedar Vale", ctrl=CEDARVALE, length_m=3850, base_lap_s=71.0,
                      pit_loss_s=18.5, start_near=(800, 1500), wear_mult=1.05, overtake_mult=0.45,
                      blurb="Short and twisty through the woods: qualifying and strategy decide it."),
    "saltmarsh": dict(name="Saltmarsh Coast", ctrl=SALTMARSH, length_m=5350, base_lap_s=90.0,
                      pit_loss_s=22.5, start_near=(1050, 1600), wear_mult=1.25, overtake_mult=1.25,
                      blurb="Windswept seaside track with two big straights and a coarse surface."),
}
DEFAULT_TRACK = "harbourline"
_cache: dict[str, Track] = {}


def get_track(track_id: str = DEFAULT_TRACK) -> Track:
    """Build (once) and return a circuit by id. Raises KeyError for unknown ids."""
    if track_id not in _cache:
        spec = dict(TRACK_SPECS[track_id])
        _cache[track_id] = build_track(track_id, **spec)
    return _cache[track_id]


def default_track() -> Track:
    return get_track(DEFAULT_TRACK)
