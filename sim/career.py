"""Career mode: season after season with one team, a budget, car upgrades and driver contracts.

Pure Python (no web or database code). A career's whole state is a plain dict so it can be
stored as JSON; every rule that changes it lives here and is tested on its own.

Money is in millions ($M).
  * Income: prize money after every round (base + per point) and a bigger payout at the end of
    each season by constructors' position.
  * Spending: car upgrades (any time between races), driver salaries (paid at the end of each
    season), and buying out a driver's contract early.
  * Rivals: every AI team develops its car each off-season, so standing still means falling back.
"""
from __future__ import annotations

import random
from dataclasses import replace

from .grid import DRIVERS, TEAMS, Driver, default_grid

# ---------------------------------------------------------------- car upgrades
AREAS = {
    "power": "Power unit: faster on the straights",
    "aero": "Aerodynamics: faster through the corners",
    "tyres": "Suspension: gentler on the tyres",
    "reliability": "Reliability: fewer mechanical retirements",
}
PACE_PER_LEVEL_S = 0.03          # power and aero, each level, seconds per lap
TYRE_WEAR_PER_LEVEL = 0.97       # wear multiplier per level (compounds)
DNF_PER_LEVEL = 0.85             # breakdown chance multiplier per level
MAX_LEVEL = 15


def upgrade_cost(level: int) -> float:
    """Price of going from `level` to `level + 1`."""
    return 6.0 + 4.0 * level


# ---------------------------------------------------------------- money
START_BUDGET_BASE = 40.0
ROUND_PRIZE_BASE = 1.0
ROUND_PRIZE_PER_POINT = 0.4
SEASON_PRIZE = (45, 40, 36, 32, 28, 25, 22, 20, 18, 16)     # by constructors' position
BUYOUT_SHARE = 0.5               # share of the remaining contract paid to release a driver early
MAX_YEARS = 3

# ---------------------------------------------------------------- drivers
# Reserve drivers who start the career without a seat (all made up).
# (code, name, pace delta, consistency, racecraft)
RESERVES = [
    ("QUI", "Leo Quinlan", -0.08, 0.21, 0.80),
    ("WAR", "Nadia Ward", -0.04, 0.23, 0.72),
    ("ZAN", "Matteo Zanetti", 0.02, 0.24, 0.66),
    ("OYE", "Femi Oyelaran", 0.06, 0.22, 0.58),
    ("HOL", "Freya Holm", 0.10, 0.25, 0.55),
    ("BAS", "Ruben Bastos", 0.16, 0.30, 0.45),
]

_TEAM = {t[0]: t for t in TEAMS}


def salary(delta: float, consistency: float, racecraft: float) -> float:
    """Season salary a driver asks for: better drivers cost more."""
    s = 8.0 - 30.0 * delta + 6.0 * (racecraft - 0.5) - 10.0 * (consistency - 0.25)
    return round(max(1.0, s), 1)


def rating(delta: float, consistency: float, racecraft: float) -> int:
    """0-100 score shown in the game."""
    r = 75 - 120 * delta + 25 * (racecraft - 0.6) - 60 * (consistency - 0.24)
    return int(max(1, min(99, round(r))))


def _driver_entry(code, name, delta, consistency, racecraft, years=0) -> dict:
    return {"code": code, "name": name, "delta": delta, "consistency": consistency,
            "racecraft": racecraft, "salary": salary(delta, consistency, racecraft),
            "rating": rating(delta, consistency, racecraft), "years": years}


class CareerError(Exception):
    """A career action that isn't allowed right now (not enough money, wrong time, ...)."""


# ---------------------------------------------------------------- creating a career
def new_career(team: str, seed: int) -> dict:
    if team not in _TEAM:
        raise CareerError("Unknown team")
    rng = random.Random(seed)
    order = [t[0] for t in TEAMS]                     # fastest car first
    budget = START_BUDGET_BASE + 4.0 * order.index(team)   # slower teams start with more money
    drivers = [_driver_entry(c, n, d, cons, craft, years=rng.choice((1, 2)))
               for c, n, tm, d, cons, craft in DRIVERS if tm == team]
    market = [_driver_entry(*r) for r in RESERVES]
    return {
        "team": team, "season_no": 1, "budget": round(budget, 1),
        "upgrades": {a: 0 for a in AREAS},
        "drivers": drivers, "market": market,
        "ai_dev": {t: 0.0 for t in order if t != team},   # s/lap gained by each rival since season 1
        "history": [], "seed": seed, "spent": {"upgrades": 0.0, "salaries": 0.0, "buyouts": 0.0},
        "income": 0.0,
    }


# ---------------------------------------------------------------- what the car and drivers do in a race
def race_grid(state: dict) -> list[Driver]:
    """The 20 drivers for the next race, with this career's upgrades and rivals' development."""
    team = state["team"]
    out = []
    for d in default_grid():
        if d.team == team:
            continue
        out.append(replace(d, pace_s=round(d.pace_s - state["ai_dev"].get(d.team, 0.0), 3)))
    _, _, color, car = _TEAM[team]
    up = state["upgrades"]
    car_pace = car - PACE_PER_LEVEL_S * (up["power"] + up["aero"])
    for e in state["drivers"]:
        out.append(Driver(e["code"], e["name"], team, color, round(car_pace + e["delta"], 3),
                          e["consistency"], e["racecraft"]))
    return out


def team_wear(state: dict) -> dict[str, float]:
    return {state["team"]: TYRE_WEAR_PER_LEVEL ** state["upgrades"]["tyres"]}


def team_dnf(state: dict) -> dict[str, float]:
    return {state["team"]: DNF_PER_LEVEL ** state["upgrades"]["reliability"]}


def ready_to_race(state: dict) -> None:
    """Raise unless both seats have a driver under contract."""
    if len(state["drivers"]) != 2:
        raise CareerError("You need two drivers under contract")
    if any(d["years"] <= 0 for d in state["drivers"]):
        raise CareerError("A driver's contract has run out: renew it or sign someone else")


# ---------------------------------------------------------------- actions
def buy_upgrade(state: dict, area: str) -> float:
    if area not in AREAS:
        raise CareerError("Unknown upgrade")
    level = state["upgrades"][area]
    if level >= MAX_LEVEL:
        raise CareerError("That part is fully developed")
    cost = upgrade_cost(level)
    if state["budget"] < cost:
        raise CareerError("Not enough budget")
    state["budget"] = round(state["budget"] - cost, 1)
    state["spent"]["upgrades"] = round(state["spent"]["upgrades"] + cost, 1)
    state["upgrades"][area] = level + 1
    return cost


def _pre_season(rounds_done: int) -> None:
    if rounds_done != 0:
        raise CareerError("Drivers can only be signed before the first race of a season")


def sign_driver(state: dict, code: str, replace_code: str, years: int, rounds_done: int) -> float:
    """Sign a driver from the market into `replace_code`'s seat. Returns what it cost now
    (a buyout if the replaced driver still had years left)."""
    _pre_season(rounds_done)
    if not 1 <= years <= MAX_YEARS:
        raise CareerError("Contracts are 1 to 3 seasons")
    new = next((d for d in state["market"] if d["code"] == code), None)
    old = next((d for d in state["drivers"] if d["code"] == replace_code), None)
    if new is None or old is None:
        raise CareerError("Driver not found")
    buyout = round(BUYOUT_SHARE * old["salary"] * max(0, old["years"]), 1)
    if state["budget"] < buyout:
        raise CareerError("Not enough budget to buy out that contract")
    state["budget"] = round(state["budget"] - buyout, 1)
    state["spent"]["buyouts"] = round(state["spent"]["buyouts"] + buyout, 1)
    state["market"].remove(new)
    state["drivers"].remove(old)
    old = {**old, "years": 0}
    state["market"].append(old)
    state["drivers"].append({**new, "years": years})
    return buyout


def renew_driver(state: dict, code: str, years: int, rounds_done: int) -> None:
    _pre_season(rounds_done)
    if not 1 <= years <= MAX_YEARS:
        raise CareerError("Contracts are 1 to 3 seasons")
    d = next((d for d in state["drivers"] if d["code"] == code), None)
    if d is None:
        raise CareerError("Driver not found")
    if d["years"] > 0:
        raise CareerError("That contract hasn't run out yet")
    d["salary"] = round(d["salary"] * 1.1, 1)         # a renewal costs a bit more
    d["years"] = years


def round_prize(team_points: int) -> float:
    return round(ROUND_PRIZE_BASE + ROUND_PRIZE_PER_POINT * team_points, 1)


def credit_round(state: dict, team_points: int) -> float:
    prize = round_prize(team_points)
    state["budget"] = round(state["budget"] + prize, 1)
    state["income"] = round(state["income"] + prize, 1)
    return prize


def end_season(state: dict, team_position: int, team_points: int, champion: str, seed: int) -> dict:
    """Close a finished season: prize money, salaries, contracts tick down, rivals develop.
    Returns a summary for the history list."""
    prize = float(SEASON_PRIZE[team_position - 1]) if 1 <= team_position <= len(SEASON_PRIZE) else 10.0
    wages = round(sum(d["salary"] for d in state["drivers"]), 1)
    state["budget"] = round(state["budget"] + prize - wages, 1)
    state["income"] = round(state["income"] + prize, 1)
    state["spent"]["salaries"] = round(state["spent"]["salaries"] + wages, 1)
    for d in state["drivers"]:
        d["years"] -= 1
    rng = random.Random(seed)
    for t in state["ai_dev"]:
        # Rivals develop 0.05-0.18 s/lap a season; teams lower down find a little more.
        rank = [x[0] for x in TEAMS].index(t)
        state["ai_dev"][t] = round(state["ai_dev"][t] + rng.uniform(0.05, 0.15) + 0.003 * rank, 3)
    # Unsigned drivers' form drifts a little between seasons.
    for d in state["market"]:
        d["delta"] = round(max(-0.2, min(0.3, d["delta"] + rng.gauss(0, 0.03))), 3)
        d["salary"] = salary(d["delta"], d["consistency"], d["racecraft"])
        d["rating"] = rating(d["delta"], d["consistency"], d["racecraft"])
    summary = {"season": state["season_no"], "team_position": team_position, "points": team_points,
               "champion": champion, "prize": prize, "salaries": wages}
    state["history"].append(summary)
    state["season_no"] += 1
    return summary
