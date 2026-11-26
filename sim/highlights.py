"""Key moments: the 5 to 8 highlights shown above the results after the flag.

Built from what the engine recorded during the race (its event log, every pit stop and the
lap-by-lap positions), so it needs no extra simulation and works the same for every kind of race.
Each highlight carries the race time to jump the replay to ("t", a few seconds before the moment)
and the drivers and teams it's about, so the player's own moments can be marked.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .commentary import TEAM_NAMES, surname
from .replay import lap_table
from .tyres import COMPOUNDS

if TYPE_CHECKING:
    from .engine import Car, RaceEngine

MAX_HIGHLIGHTS = 8
LEAD_IN_S = 5.0           # the replay starts this long before the moment


def _name(car: "Car") -> str:
    return surname(car.driver.name)


def _team(car: "Car") -> str:
    return TEAM_NAMES.get(car.driver.team, car.driver.team.title())


def _h(kind: str, title: str, text: str, lap: int, t: float, cars: list["Car"]) -> dict:
    return {"kind": kind, "title": title, "text": text, "lap": int(lap), "t": round(max(0.0, t - LEAD_IN_S), 1),
            "codes": [c.code for c in cars], "teams": sorted({c.driver.team for c in cars})}


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


class _Race:
    """The race as the highlights see it: final order, positions per lap and lap start times."""

    def __init__(self, e: "RaceEngine"):
        self.e = e
        self.lap_t, _, self.positions = lap_table(e.cars)
        self.order = e.order()
        self.finish = {c.code: i for i, c in enumerate(self.order, 1)}
        self.overtakes = [ev for ev in e.event_log if ev["kind"] == "overtake" and ev.get("pos")]

    def lap_start(self, lap: int) -> float:
        """Race time the leader started lap n."""
        if lap <= 1 or not self.lap_t:
            return 0.0
        return self.lap_t[min(lap, len(self.lap_t)) - 2]

    def pos_at(self, code: str, lap: int) -> int | None:
        row = self.positions.get(code, [])
        return row[lap] if 0 <= lap < len(row) else None

    def best_gain_lap(self, code: str) -> int:
        """The lap on which this car gained the most places (1 if it never did)."""
        row = self.positions.get(code, [])
        best, lap = 0, 1
        for n in range(1, len(row)):
            if row[n] is None or row[n - 1] is None:
                continue
            if row[n - 1] - row[n] > best:
                best, lap = row[n - 1] - row[n], n
        return lap

    def worst_loss_lap(self, code: str) -> int:
        row = self.positions.get(code, [])
        worst, lap = 0, 1
        for n in range(1, len(row)):
            if row[n] is None or row[n - 1] is None:
                continue
            if row[n] - row[n - 1] > worst:
                worst, lap = row[n] - row[n - 1], n
        return lap


def move_of_the_race(r: _Race) -> dict | None:
    """The overtake that mattered most: for the lead or a podium place, late in the race."""
    if not r.overtakes:
        return None
    total = r.e.cfg.laps

    def undone(ev) -> bool:
        """Was the place taken straight back, or lost again before the line?"""
        at_line = r.pos_at(ev["code"], ev["lap"])
        return ((at_line is not None and at_line > ev["pos"])
                or any(o["code"] == ev["other"] and o["other"] == ev["code"] and ev["t"] < o["t"] <= ev["t"] + 200
                       for o in r.overtakes))

    def score(ev):
        p = ev["pos"]
        return ((21 - p) + (20 if p == 1 else 8 if p <= 3 else 0) + 6 * ev["lap"] / total
                + (2 if not ev.get("drs") else 0) - (30 if undone(ev) else 0))
    ev = max(r.overtakes, key=score)
    a, b = r.e.car(ev["code"]), r.e.car(ev["other"])
    what = "the lead" if ev["pos"] == 1 else f"P{ev['pos']}"
    text = f"{_name(a)} passed {_name(b)} for {what} on lap {ev['lap']}" + (" with DRS." if ev.get("drs") else ".")
    return _h("overtake", "Move of the race", text, ev["lap"], ev["t"], [a, b])


def _stop_net(r: _Race, stop: dict) -> tuple[int, int, int] | None:
    """(places before the stop, places once the stops around it have played out, net gain)."""
    code, lap = stop["code"], stop["lap"]
    before = r.pos_at(code, lap)
    row = r.positions.get(code, [])
    last = max((n for n, p in enumerate(row) if p is not None), default=0)
    if before is None or last <= lap:
        return None
    # Wait until the cars that were running near this one have made their own stops.
    order = stop["order"]
    i = order.index(code) if code in order else 0
    near = set(order[max(0, i - 3):i + 4]) - {code}
    their = [p["lap"] for p in r.e.pit_log if p["code"] in near and lap - 8 <= p["lap"] <= lap + 12]
    end = min(last, max([lap + 3] + [n + 2 for n in their]))
    after = r.pos_at(code, end)
    if after is None:
        return None
    return before, after, before - after


def pit_calls(r: _Race) -> list[dict]:
    rated = []
    for stop in r.e.pit_log:
        net = _stop_net(r, stop)
        if net is not None:
            rated.append((stop, *net))
    out = []
    if not rated:
        return out
    best = max(rated, key=lambda x: (x[3] + (0.5 if x[0]["neutral"] else 0), -x[0]["lap"]))
    if best[3] >= 1:
        stop, before, after, net = best
        car = r.e.car(stop["code"])
        when = " under the Safety Car" if stop["neutral"] == "sc" else " under the VSC" if stop["neutral"] == "vsc" else ""
        text = (f"{_team(car)} boxed {_name(car)} on lap {stop['race_lap']}{when} for {COMPOUNDS[stop['new']].name}s: "
                f"P{before} before the stop, P{after} once the stops played out ({_plural(net, 'place')} up).")
        out.append(_h("pit-best", "Best pit call", text, stop["race_lap"], stop["t"], [car]))
    rest = [x for x in rated if x is not best and x[0]["code"] != best[0]["code"]]
    if rest:
        worst = min(rest, key=lambda x: (x[3] - (x[0]["stationary"] - 2.5) * 0.5, x[0]["lap"]))
        stop, before, after, net = worst
        if net <= -1 or stop["slow"]:
            car = r.e.car(stop["code"])
            slow = f", a slow {stop['stationary']:.1f}s stop" if stop["slow"] else ""
            text = (f"{_name(car)} pitted on lap {stop['race_lap']}{slow}: P{before} before, P{after} after"
                    + (f" ({_plural(-net, 'place')} lost)." if net < 0 else "."))
            out.append(_h("pit-worst", "Worst pit call", text, stop["race_lap"], stop["t"], [car]))
    return out


def winning_strategy(r: _Race) -> dict | None:
    win = r.order[0]
    if not win.laps or win.retired:
        return None
    stints = [[win.laps[0]["tyre"], 1, 1]]
    for rec in win.laps[1:]:
        if rec["pit"]:
            stints.append([rec["tyre"], rec["lap"], rec["lap"]])
        stints[-1][2] = rec["lap"]
    stops = len(stints) - 1
    plan = ", ".join(f"{COMPOUNDS[c].name} (laps {a}-{b})" if a != b else f"{COMPOUNDS[c].name} (lap {a})"
                     for c, a, b in stints)
    kind = "No stop" if stops == 0 else "One stop" if stops == 1 else f"{stops} stops"
    text = f"{_name(win)} won from P{win.grid}. {kind}: {plan}."
    first = next((p for p in r.e.pit_log if p["code"] == win.code), None)
    lap, t = (first["race_lap"], first["t"]) if first else (1, 0.0)
    return _h("strategy", "Winning strategy", text, lap, t, [win])


def climbers(r: _Race) -> list[dict]:
    """Most places gained, driver of the day and the toughest afternoon."""
    out = []
    done = [c for c in r.order if c.finished]
    gained = sorted(((c.grid - r.finish[c.code], c) for c in done), key=lambda x: (-x[0], r.finish[x[1].code]))
    climber = None
    if gained and gained[0][0] >= 2:
        n, climber = gained[0]
        lap = r.best_gain_lap(climber.code)
        out.append(_h("gained", "Most places gained",
                      f"{_name(climber)}: P{climber.grid} on the grid to P{r.finish[climber.code]} at the flag, "
                      f"{_plural(n, 'place')} up.", lap, r.lap_start(lap), [climber]))
    passes = {}
    for ev in r.overtakes:
        passes[ev["code"]] = passes.get(ev["code"], 0) + 1

    def dotd(c):
        f = r.finish[c.code]
        return (c.grid - f) + max(0, 11 - f) * 0.6 + passes.get(c.code, 0) * 0.3 + (2 if f == 1 else 0)
    pool = [c for c in done if c is not climber] or done
    if pool:
        star = max(pool, key=dotd)
        f = r.finish[star.code]
        lap = r.best_gain_lap(star.code)
        n = passes.get(star.code, 0)
        text = (f"{_name(star)} ({_team(star)}): P{star.grid} to P{f}" +
                (f" with {_plural(n, 'overtake')}." if n else "."))
        out.append(_h("dotd", "Driver of the day", text, lap, r.lap_start(lap), [star]))
    lost = sorted(((r.finish[c.code] - c.grid, c) for c in done), key=lambda x: (-x[0], x[1].grid))
    if lost and lost[0][0] >= 2:
        n, c = lost[0]
        lap = r.worst_loss_lap(c.code)
        out.append(_h("lost", "Most places lost",
                      f"{_name(c)}: P{c.grid} on the grid, P{r.finish[c.code]} at the flag, "
                      f"{_plural(n, 'place')} down.", lap, r.lap_start(lap), [c]))
    outs = [ev for ev in r.e.event_log if ev["kind"] in ("crash", "dnf") and ev.get("pos")]
    if outs:
        ev = min(outs, key=lambda x: x["pos"])
        if ev["pos"] <= 10:
            c = r.e.car(ev["code"])
            why = "crashed out" if ev["kind"] == "crash" else "retired with a power unit failure"
            out.append(_h("retired", "Heartbreak", f"{_name(c)} {why} from P{ev['pos']} on lap {ev['lap']}.",
                          ev["lap"], ev["t"], [c]))
    return out


def neutralisation(r: _Race) -> dict | None:
    calls = [ev for ev in r.e.event_log if ev["kind"] in ("sc", "vsc") and not ev.get("sc_in")]
    if not calls:
        return None
    def stops_under(i: int) -> int:
        start, end = calls[i]["t"], calls[i + 1]["t"] if i + 1 < len(calls) else float("inf")
        return sum(1 for p in r.e.pit_log if p["neutral"] and start <= p["t"] < end)
    # The one that changed most: the most cars taking a cheap stop under it (the first, on a tie).
    i = max(range(len(calls)), key=lambda k: (stops_under(k), -k))
    ev, stops = calls[i], stops_under(i)
    if stops == 0 and ev["kind"] == "vsc":
        return None                      # a quiet VSC that changed nothing isn't a key moment
    flag = "Safety Car" if ev["kind"] == "sc" else "Virtual Safety Car"
    who = r.e.car(ev["who"]) if ev.get("who") else None
    why = ("for debris" if ev.get("cause") == "debris" else
           f"after {_name(who)} crashed" if who and ev.get("cause") == "crash" else
           f"after {_name(who)} stopped on track" if who else "")
    others = len(calls) - 1
    more = f" One of {len(calls)} neutralisations in this race." if others else ""
    cheap = f" {_plural(stops, 'car')} took a cheap stop under it." if stops else " Nobody took the chance to pit."
    text = f"{flag} on lap {ev['lap']} {why}.".replace(" .", ".") + cheap + more
    return _h("neutral", "Safety Car" if ev["kind"] == "sc" else "Virtual Safety Car", text, ev["lap"], ev["t"],
              [who] if who else [])


def fastest_lap(r: _Race) -> dict | None:
    f = r.e.fastest
    if not f:
        return None
    from .engine import fmt_time
    car = r.e.car(f["code"])
    lap = f["lap"]
    return _h("fastest", "Fastest lap", f"{_name(car)}: {fmt_time(f['time'])} on lap {lap}.", lap,
              r.lap_start(lap), [car])


# Shown in this order; when there are more than MAX_HIGHLIGHTS, the last kinds here go first.
ORDER = ["overtake", "pit-best", "pit-worst", "strategy", "dotd", "gained", "lost", "neutral", "retired", "fastest"]
DROP_FIRST = ["fastest", "retired", "lost", "neutral", "pit-worst"]


def build_highlights(engine: "RaceEngine") -> list[dict]:
    """The key moments of a finished race (empty while it's still running)."""
    if not engine.finished or not engine.cars:
        return []
    r = _Race(engine)
    found = [h for h in (move_of_the_race(r), *pit_calls(r), winning_strategy(r), *climbers(r),
                         neutralisation(r), fastest_lap(r)) if h]
    for kind in DROP_FIRST:
        if len(found) <= MAX_HIGHLIGHTS:
            break
        found = [h for h in found if h["kind"] != kind]
    return sorted(found, key=lambda h: ORDER.index(h["kind"]))
