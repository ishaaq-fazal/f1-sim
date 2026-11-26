"""Replay and race-chart data built from a race's recorded laps and frames.

The engine records a frame every REPLAY_FRAME_S race seconds (each car's distance in laps
and a status letter) and every lap a car completes (with the race time it crossed the line).
From those, this builds:

- lap_t: the race time the leader crossed the line at the end of each lap (index 0 = lap 1),
  which is what the replay scrubber steps through;
- gaps: per car, the seconds behind the leader as it crossed the line on each lap
  (None once it stops completing laps);
- positions: per car, its grid slot then its place crossing the line on each lap;
- laps: per car, [lap, race time, lap time, tyre, pitted] for every completed lap;
- events: the race-control log with race times, and the frames for the track map
  ([t, distance per car, status letter per car, race flag g/s/v]).
"""
from __future__ import annotations

from .engine import REPLAY_FRAME_S


def lap_table(cars) -> tuple[list[float], dict[str, list[float | None]], dict[str, list[int | None]]]:
    """Leader crossing times per lap, each car's gap to the leader per lap, and each car's
    position per lap (index 0 = grid). Cars are anything with .code, .grid and .laps."""
    crossings: dict[int, list[tuple[float, str]]] = {}
    for c in cars:
        for rec in c.laps:
            crossings.setdefault(rec["lap"], []).append((rec["t"], c.code))
    n_laps = max(crossings, default=0)
    lap_t: list[float] = []
    gaps: dict[str, list[float | None]] = {c.code: [] for c in cars}
    positions: dict[str, list[int | None]] = {c.code: [c.grid] for c in cars}
    for n in range(1, n_laps + 1):
        row = sorted(crossings.get(n, []))
        lead_t = row[0][0]
        lap_t.append(round(lead_t, 3))
        at = {code: (pos, t) for pos, (t, code) in enumerate(row, 1)}
        for c in cars:
            if c.code in at:
                pos, t = at[c.code]
                gaps[c.code].append(round(t - lead_t, 3))
                positions[c.code].append(pos)
            else:
                gaps[c.code].append(None)
                positions[c.code].append(None)
    return lap_t, gaps, positions


def build_replay(engine) -> dict:
    cars = engine.cars
    lap_t, gaps, positions = lap_table(cars)
    return {
        "type": "replay",
        "track": engine.track.id,
        "total_laps": engine.cfg.laps,
        "frame_s": REPLAY_FRAME_S,
        "cars": [{"code": c.code, "name": c.driver.name, "team": c.driver.team, "color": c.driver.color,
                  "grid": c.grid} for c in cars],
        "frames": engine.frames,
        "lap_t": lap_t,
        "gaps": gaps,
        "positions": positions,
        "laps": {c.code: [[r["lap"], r["t"], r["time"], r["tyre"], r["pit"]] for r in c.laps] for c in cars},
        "events": [{k: e[k] for k in ("t", "lap", "kind", "text", "code")} for e in engine.event_log],
        "finished": engine.finished,
    }
