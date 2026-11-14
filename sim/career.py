"""Career mode: season after season as a team principal, building the team up over time.

Pure Python (no web or database code). A career's whole state is a plain dict so it can be
stored as JSON; every rule that changes it lives here and is tested on its own.

Money is in millions ($M). Development points (DP) are factory time.
  * Car development: upgrades cost DP. DP come from results (every round), a development
    allowance at the end of each season (bigger for teams lower down, so small teams can
    catch up), and from funding the factory with money.
  * Drivers: salaries are paid at the end of each season; contracts run out and must be renewed
    or replaced. Drivers get older: young ones improve, veterans fade and eventually retire,
    and a good or bad season against the team-mate moves their rating. Rookies join the market.
  * The board: each season it sets three objectives scaled to how strong the team is. Miss two
    (or one while already on a warning) and you are sacked; weaker teams may offer you a job,
    and if none does the career is over.
  * Rivals: every AI team develops its car each off-season, so standing still means falling back.
"""
from __future__ import annotations

import copy
import random
import statistics
from dataclasses import replace

from .grid import DRIVERS, TEAMS, Driver, default_grid
from .season import ROUNDS

# ---------------------------------------------------------------- car development
AREAS = {
    "power": "Engine: faster on the straights",
    "aero": "Aerodynamics: faster through the corners",
    "tyres": "Suspension: gentler on the tyres",
    "pit": "Pit crew: quicker pit stops",
    "reliability": "Reliability: fewer mechanical retirements",
}
PACE_PER_LEVEL_S = 0.03          # engine and aero, each level, seconds per lap
TYRE_WEAR_PER_LEVEL = 0.97       # wear multiplier per level (compounds)
PIT_PER_LEVEL_S = 0.1            # seconds off each pit stop per level
DNF_PER_LEVEL = 0.85             # breakdown chance multiplier per level
MAX_LEVEL = 15

DP_PER_ROUND_BASE = 1            # development points after every round ...
DP_PER_ROUND_POINTS = 10         # ... plus one for every 10 points the team scored
FACTORY_DP = 5                   # funding the factory buys this many DP ...
FACTORY_COST = 10.0              # ... for this much money


def upgrade_cost(level: int) -> int:
    """Development points to go from `level` to `level + 1`."""
    return 3 + 2 * level


def season_allowance(team_position: int) -> int:
    """Development points at the end of a season: lower teams get more factory time."""
    return 4 + 2 * (max(1, min(10, team_position)) - 1)


# ---------------------------------------------------------------- money
START_BUDGET_BASE = 40.0
ROUND_PRIZE_BASE = 1.0
ROUND_PRIZE_PER_POINT = 0.4
SEASON_PRIZE = (45, 40, 36, 32, 28, 25, 22, 20, 18, 16)     # by constructors' position
BUYOUT_SHARE = 0.5               # share of the remaining contract paid to release a driver early
MAX_YEARS = 3
BOARD_BONUS_MONEY = 10.0         # every objective met
BOARD_BONUS_DP = 10

# ---------------------------------------------------------------- drivers
# Reserve drivers who start the career without a seat (all made up).
# (code, name, pace delta, consistency, racecraft, age)
RESERVES = [
    ("QUI", "Leo Quinlan", -0.08, 0.21, 0.80, 31),
    ("WAR", "Nadia Ward", -0.04, 0.23, 0.72, 27),
    ("ZAN", "Matteo Zanetti", 0.02, 0.24, 0.66, 24),
    ("OYE", "Femi Oyelaran", 0.06, 0.22, 0.58, 22),
    ("HOL", "Freya Holm", 0.10, 0.25, 0.55, 20),
    ("BAS", "Ruben Bastos", 0.16, 0.30, 0.45, 19),
]
# Ages of the drivers on the default grid at the start of a career.
AGES = {"MAR": 29, "OKA": 23, "BRA": 32, "VEN": 26, "TAK": 30, "ILV": 24, "CAS": 34, "NOV": 21,
        "ROS": 28, "AMA": 25, "LIN": 33, "FER": 22, "HAL": 36, "DUV": 27, "PET": 31, "SAN": 20,
        "GRA": 35, "MOR": 23, "KOV": 37, "ELL": 19}
# Rookies who arrive on the market one season at a time (made-up names).
ROOKIES = [
    ("ABE", "Jonah Abeyta"), ("SVE", "Astrid Svensson"), ("MBA", "Tendai Mbeki"), ("COR", "Lucia Cortes"),
    ("HAY", "Finn Hayward"), ("YAM", "Haru Yamada"), ("DEL", "Marco Delacroix"), ("KAR", "Selin Karaca"),
    ("BOW", "Ella Bowen"), ("RIV", "Diego Rivas"), ("NIE", "Lars Nielsen"), ("PAT", "Asha Patel"),
    ("VOG", "Emil Vogt"), ("OKO", "Grace Okoye"), ("LAN", "Remy Lanvin"), ("SZA", "Bence Szabo"),
    ("MCK", "Rory McKenna"), ("ALV", "Ines Alvarez"), ("TAN", "Wei Tan"), ("DUP", "Hugo Dupont"),
]
ROOKIES_PER_SEASON = 2
RETIRE_AGE = 38
MARKET_SIZE = 12

_TEAM = {t[0]: t for t in TEAMS}
_ORDER = [t[0] for t in TEAMS]                       # fastest car first
_TEAM_DRIVERS = {t: [d for d in DRIVERS if d[2] == t] for t in _ORDER}

# Constructors' points a team of each strength (1 = strongest) scores in a season, measured
# from AI races on the default grid; the board's points target is a share of this.
EXPECTED_POINTS = (230, 210, 170, 110, 90, 70, 50, 35, 22, 15)
POINTS_TARGET_SHARE = 0.75


def salary(delta: float, consistency: float, racecraft: float) -> float:
    """Season salary a driver asks for: better drivers cost more."""
    s = 8.0 - 30.0 * delta + 6.0 * (racecraft - 0.5) - 10.0 * (consistency - 0.25)
    return round(max(1.0, s), 1)


def rating(delta: float, consistency: float, racecraft: float) -> int:
    """0-100 score shown in the game."""
    r = 75 - 120 * delta + 25 * (racecraft - 0.6) - 60 * (consistency - 0.24)
    return int(max(1, min(99, round(r))))


def _driver_entry(code, name, delta, consistency, racecraft, age=25, years=0, growth=1.0) -> dict:
    return {"code": code, "name": name, "delta": delta, "consistency": consistency,
            "racecraft": racecraft, "salary": salary(delta, consistency, racecraft),
            "rating": rating(delta, consistency, racecraft), "years": years,
            "age": age, "growth": growth, "change": 0, "retiring": False}


def _team_name(team: str) -> str:
    return _TEAM[team][1]


class CareerError(Exception):
    """A career action that isn't allowed right now (not enough money, wrong time, ...)."""


# ---------------------------------------------------------------- creating a career
def new_career(team: str, seed: int) -> dict:
    if team not in _TEAM:
        raise CareerError("Unknown team")
    rng = random.Random(seed)
    budget = START_BUDGET_BASE + 4.0 * _ORDER.index(team)   # slower teams start with more money
    drivers = [_driver_entry(c, n, d, cons, craft, age=AGES[c], years=rng.choice((1, 2)))
               for c, n, tm, d, cons, craft in DRIVERS if tm == team]
    market = [_driver_entry(*r, growth=round(rng.uniform(0.6, 1.4), 2)) for r in RESERVES]
    state = {
        "team": team, "season_no": 1, "budget": round(budget, 1),
        "dev_points": season_allowance(_ORDER.index(team) + 1),
        "upgrades": {a: 0 for a in AREAS},
        "inherited_dev": 0.0,              # s/lap the car had already gained when you joined the team
        "drivers": drivers, "market": market, "rookies_used": 0,
        "ai_dev": {t: 0.0 for t in _ORDER if t != team},   # s/lap gained by each rival since season 1
        "history": [], "seed": seed, "spent": {"upgrades": 0.0, "salaries": 0.0, "buyouts": 0.0},
        "income": 0.0, "status": "active", "warning": False, "offers": [], "teams": [team],
    }
    set_objectives(state)
    return state


def migrate(state: dict) -> dict:
    """Bring a career saved before development points, the driver market and the board up to
    date. Returns the same dict when nothing is missing, otherwise an updated copy."""
    if "status" in state and "dev_points" in state and "objectives" in state:
        return state
    st = copy.deepcopy(state)
    st.setdefault("dev_points", 10)
    st.setdefault("inherited_dev", 0.0)
    st.setdefault("status", "active")
    st.setdefault("warning", False)
    st.setdefault("offers", [])
    st.setdefault("rookies_used", 0)
    st.setdefault("teams", [st["team"]])
    for a in AREAS:
        st["upgrades"].setdefault(a, 0)
    reserves = {r[0]: r[5] for r in RESERVES}
    for d in st["drivers"] + st["market"]:
        d.setdefault("age", AGES.get(d["code"], reserves.get(d["code"], 26)) + st["season_no"] - 1)
        d.setdefault("growth", 1.0)
        d.setdefault("change", 0)
        d.setdefault("retiring", False)
    if "objectives" not in st:
        set_objectives(st)
    return st


# ---------------------------------------------------------------- what the car and drivers do in a race
def own_car_pace(state: dict) -> float:
    up = state["upgrades"]
    return _TEAM[state["team"]][3] - state.get("inherited_dev", 0.0) - PACE_PER_LEVEL_S * (up["power"] + up["aero"])


def race_grid(state: dict) -> list[Driver]:
    """The 20 drivers for the next race, with this career's upgrades and rivals' development."""
    team = state["team"]
    out = []
    for d in default_grid():
        if d.team == team:
            continue
        out.append(replace(d, pace_s=round(d.pace_s - state["ai_dev"].get(d.team, 0.0), 3)))
    color = _TEAM[team][2]
    car_pace = own_car_pace(state)
    for e in state["drivers"]:
        out.append(Driver(e["code"], e["name"], team, color, round(car_pace + e["delta"], 3),
                          e["consistency"], e["racecraft"]))
    return out


def team_wear(state: dict) -> dict[str, float]:
    return {state["team"]: TYRE_WEAR_PER_LEVEL ** state["upgrades"]["tyres"]}


def team_dnf(state: dict) -> dict[str, float]:
    return {state["team"]: DNF_PER_LEVEL ** state["upgrades"]["reliability"]}


def team_pit(state: dict) -> dict[str, float]:
    """Seconds the pit crew saves on every stop."""
    return {state["team"]: round(PIT_PER_LEVEL_S * state["upgrades"].get("pit", 0), 2)}


def ready_to_race(state: dict) -> None:
    """Raise unless the team can race: still employed and two drivers under contract."""
    if state.get("status") == "sacked":
        raise CareerError("You've been sacked: pick a job offer or end the career")
    if state.get("status") == "over":
        raise CareerError("This career is over")
    if len(state["drivers"]) != 2:
        raise CareerError("You need two drivers under contract")
    if any(d["years"] <= 0 for d in state["drivers"]):
        raise CareerError("A driver's contract has run out: renew it or sign someone else")


def _active(state: dict) -> None:
    if state.get("status", "active") != "active":
        raise CareerError("You don't run a team right now")


# ---------------------------------------------------------------- team strength and the board
def team_strengths(state: dict) -> list[str]:
    """Teams from strongest to weakest: car pace (with development) plus the drivers' pace."""
    pace = {}
    for t in _ORDER:
        if t == state["team"]:
            pace[t] = own_car_pace(state) + statistics.mean(d["delta"] for d in state["drivers"])
        else:
            pace[t] = (_TEAM[t][3] - state["ai_dev"].get(t, 0.0)
                       + statistics.mean(d[3] for d in _TEAM_DRIVERS[t]))
    return sorted(_ORDER, key=lambda t: (pace[t], _ORDER.index(t)))


def set_objectives(state: dict) -> dict:
    """The board's three targets for the coming season, scaled to the team's strength."""
    order = team_strengths(state)
    rank = order.index(state["team"]) + 1
    target_pos = min(10, rank + 1)
    expected = EXPECTED_POINTS[rank - 1] * ROUNDS / 10
    target_pts = max(5, int(round(POINTS_TARGET_SHARE * expected / 5.0)) * 5)
    rival = order[rank - 2] if rank > 1 else order[1]
    rival_text = (f"Beat {_team_name(rival)} in the teams' championship" if rank > 1
                  else f"Stay ahead of {_team_name(rival)} in the teams' championship")
    state["objectives"] = {
        "season": state["season_no"], "expected": rank,
        "items": [
            {"kind": "position", "target": target_pos,
             "text": f"Finish P{target_pos} or higher in the teams' championship"},
            {"kind": "points", "target": target_pts, "text": f"Score at least {target_pts} points"},
            {"kind": "rival", "target": rival, "text": rival_text},
        ],
    }
    return state["objectives"]


def objective_progress(state: dict, team_positions: dict[str, int], team_points: int,
                       rounds_done: int) -> list[dict]:
    """Where each objective stands. team_positions maps team id -> current teams' position."""
    mine = team_positions.get(state["team"], 10)
    out = []
    for o in state["objectives"]["items"]:
        if o["kind"] == "position":
            met, now = mine <= o["target"], f"P{mine} now" if rounds_done else "No races yet"
        elif o["kind"] == "points":
            projected = team_points * ROUNDS / rounds_done if rounds_done else 0
            met = team_points >= o["target"] if rounds_done >= ROUNDS else projected >= o["target"]
            now = f"{team_points} so far" if rounds_done else "No races yet"
        else:
            theirs = team_positions.get(o["target"], 10)
            met = mine < theirs
            now = f"You P{mine}, them P{theirs}" if rounds_done else "No races yet"
        out.append({"kind": o["kind"], "text": o["text"], "now": now,
                    "met": bool(met) if rounds_done else None})
    return out


def judge_season(state: dict, team_positions: dict[str, int], team_points: int) -> tuple[str, list[dict]]:
    """The board's verdict on a finished season: bonus, kept, warning or sacked."""
    items = objective_progress(state, team_positions, team_points, ROUNDS)
    missed = sum(1 for i in items if not i["met"])
    if missed == 0:
        verdict = "bonus"
    elif missed >= 2 or (missed == 1 and state.get("warning")):
        verdict = "sacked"
    else:
        verdict = "warning"
    return verdict, [{"text": i["text"], "met": i["met"]} for i in items]


def job_offers(state: dict, rng: random.Random) -> list[str]:
    """Up to two weaker teams willing to hire a principal who was just sacked."""
    order = team_strengths(state)
    weaker = order[order.index(state["team"]) + 1:]
    return sorted(rng.sample(weaker, min(2, len(weaker))), key=order.index)


# ---------------------------------------------------------------- actions
def buy_upgrade(state: dict, area: str) -> int:
    _active(state)
    if area not in AREAS:
        raise CareerError("Unknown upgrade")
    level = state["upgrades"][area]
    if level >= MAX_LEVEL:
        raise CareerError("That part is fully developed")
    cost = upgrade_cost(level)
    if state["dev_points"] < cost:
        raise CareerError("Not enough development points")
    state["dev_points"] -= cost
    state["upgrades"][area] = level + 1
    return cost


def fund_factory(state: dict) -> float:
    """Turn money into development points (money spent here can't pay drivers)."""
    _active(state)
    if state["budget"] < FACTORY_COST:
        raise CareerError("Not enough budget")
    state["budget"] = round(state["budget"] - FACTORY_COST, 1)
    state["spent"]["upgrades"] = round(state["spent"]["upgrades"] + FACTORY_COST, 1)
    state["dev_points"] += FACTORY_DP
    return FACTORY_COST


def _pre_season(rounds_done: int) -> None:
    if rounds_done != 0:
        raise CareerError("Drivers can only be signed before the first race of a season")


def sign_driver(state: dict, code: str, replace_code: str, years: int, rounds_done: int) -> float:
    """Sign a driver from the market into `replace_code`'s seat. Returns what it cost now
    (a buyout if the replaced driver still had years left)."""
    _active(state)
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
    if not old.get("retiring"):
        state["market"].append({**old, "years": 0})
    state["drivers"].append({**new, "years": years})
    return buyout


def renew_driver(state: dict, code: str, years: int, rounds_done: int) -> None:
    _active(state)
    _pre_season(rounds_done)
    if not 1 <= years <= MAX_YEARS:
        raise CareerError("Contracts are 1 to 3 seasons")
    d = next((d for d in state["drivers"] if d["code"] == code), None)
    if d is None:
        raise CareerError("Driver not found")
    if d.get("retiring"):
        raise CareerError(f"{d['name']} is retiring: sign someone else")
    if d["years"] > 0:
        raise CareerError("That contract hasn't run out yet")
    # A new deal is priced on the driver's current rating, plus a bit for staying.
    d["salary"] = round(salary(d["delta"], d["consistency"], d["racecraft"]) * 1.1, 1)
    d["years"] = years


def round_prize(team_points: int) -> float:
    return round(ROUND_PRIZE_BASE + ROUND_PRIZE_PER_POINT * team_points, 1)


def round_dev_points(team_points: int) -> int:
    return DP_PER_ROUND_BASE + team_points // DP_PER_ROUND_POINTS


def credit_round(state: dict, team_points: int) -> float:
    prize = round_prize(team_points)
    state["budget"] = round(state["budget"] + prize, 1)
    state["income"] = round(state["income"] + prize, 1)
    state["dev_points"] = state.get("dev_points", 0) + round_dev_points(team_points)
    return prize


# ---------------------------------------------------------------- drivers between seasons
def age_shift(age: int) -> float:
    """Pace change from a year of age (negative = faster)."""
    if age <= 22:
        return -0.035
    if age <= 25:
        return -0.02
    if age <= 28:
        return -0.008
    if age <= 31:
        return 0.0
    if age <= 34:
        return 0.015
    return 0.03


def develop_driver(d: dict, rng: random.Random, form: float = 0.0) -> None:
    """One off-season for one driver: a year older, pace moves with age, form and luck.
    form is -1 (crushed by the team-mate) to +1 (crushed the team-mate)."""
    before = d["rating"]
    shift = age_shift(d["age"])
    if shift < 0:
        shift *= d.get("growth", 1.0)                 # some young drivers have more to come
    shift -= 0.02 * max(-1.0, min(1.0, form))
    shift += rng.gauss(0, 0.012)
    d["delta"] = round(max(-0.25, min(0.35, d["delta"] + shift)), 3)
    step = -0.004 if d["age"] <= 25 else (0.004 if d["age"] >= 33 else 0.0)
    d["consistency"] = round(max(0.17, min(0.34, d["consistency"] + step)), 3)
    d["racecraft"] = round(max(0.3, min(0.9, d["racecraft"] + (0.01 if d["age"] <= 27 else 0.0))), 3)
    d["age"] += 1
    d["rating"] = rating(d["delta"], d["consistency"], d["racecraft"])
    d["change"] = d["rating"] - before


def _new_rookies(state: dict, rng: random.Random) -> list[dict]:
    out = []
    taken = {d["code"] for d in state["drivers"] + state["market"]} | {d[0] for d in DRIVERS}
    while len(out) < ROOKIES_PER_SEASON and state["rookies_used"] < len(ROOKIES):
        code, name = ROOKIES[state["rookies_used"]]
        state["rookies_used"] += 1
        if code in taken:
            continue
        out.append(_driver_entry(code, name, round(rng.uniform(0.10, 0.24), 3),
                                 round(rng.uniform(0.25, 0.31), 3), round(rng.uniform(0.40, 0.62), 3),
                                 age=rng.randint(18, 20), growth=round(rng.uniform(0.6, 1.6), 2)))
    return out


def _driver_form(state: dict, driver_points: dict[str, int]) -> dict[str, float]:
    if len(state["drivers"]) != 2:
        return {}
    a, b = state["drivers"]
    pa, pb = driver_points.get(a["code"], 0), driver_points.get(b["code"], 0)
    f = (pa - pb) / (pa + pb + 20)
    return {a["code"]: f, b["code"]: -f}


# ---------------------------------------------------------------- end of a season
def end_season(state: dict, team_position: int, team_points: int, champion: str, seed: int,
               team_positions: dict[str, int] | None = None,
               driver_points: dict[str, int] | None = None) -> dict:
    """Close a finished season: prize money, wages, development allowance, the board's verdict,
    drivers age and change, contracts tick down, rivals develop. Returns a history summary."""
    _active(state)
    team_positions = dict(team_positions or {})
    team_positions.setdefault(state["team"], team_position)
    prize = float(SEASON_PRIZE[team_position - 1]) if 1 <= team_position <= len(SEASON_PRIZE) else 10.0
    wages = round(sum(d["salary"] for d in state["drivers"]), 1)
    allowance = season_allowance(team_position)
    verdict, objectives = judge_season(state, team_positions, team_points)
    bonus = BOARD_BONUS_MONEY if verdict == "bonus" else 0.0
    state["budget"] = round(state["budget"] + prize + bonus - wages, 1)
    state["income"] = round(state["income"] + prize + bonus, 1)
    state["spent"]["salaries"] = round(state["spent"]["salaries"] + wages, 1)
    state["dev_points"] += allowance + (BOARD_BONUS_DP if verdict == "bonus" else 0)

    rng = random.Random(seed)
    form = _driver_form(state, driver_points or {})
    for d in state["drivers"]:
        d["years"] -= 1
        develop_driver(d, rng, form.get(d["code"], 0.0))
        d["retiring"] = d["age"] >= RETIRE_AGE and d["years"] <= 0
    for d in state["market"]:
        develop_driver(d, rng)
        d["salary"] = salary(d["delta"], d["consistency"], d["racecraft"])
    state["market"] = [d for d in state["market"] if d["age"] < RETIRE_AGE]
    state["market"] += _new_rookies(state, rng)
    if len(state["market"]) > MARKET_SIZE:     # the least wanted drop out, rookies stay
        keep = sorted(state["market"], key=lambda d: (d["age"] > 21, -d["rating"]))[:MARKET_SIZE]
        state["market"] = [d for d in state["market"] if d in keep]

    for t in state["ai_dev"]:
        # Rivals develop 0.05-0.18 s/lap a season; teams lower down find a little more.
        rank = _ORDER.index(t)
        state["ai_dev"][t] = round(state["ai_dev"][t] + rng.uniform(0.05, 0.15) + 0.003 * rank, 3)

    summary = {"season": state["season_no"], "team": state["team"], "team_position": team_position,
               "points": team_points, "champion": champion, "prize": prize + bonus, "salaries": wages,
               "dev_points": allowance, "verdict": verdict, "objectives": objectives}
    state["history"].append(summary)
    state["season_no"] += 1
    if verdict == "sacked":
        state["offers"] = job_offers(state, rng)
        state["status"] = "sacked" if state["offers"] else "over"
        state["warning"] = False
    else:
        state["warning"] = verdict == "warning"
        set_objectives(state)
    return summary


def accept_offer(state: dict, team: str, seed: int) -> None:
    """Join a team that offered you a job after you were sacked. Your old team's car keeps the
    development you gave it; you take over the new team's car, money and drivers."""
    if state.get("status") != "sacked":
        raise CareerError("You haven't been sacked")
    if team not in state["offers"]:
        raise CareerError("That team hasn't offered you a job")
    rng = random.Random(seed)
    old = state["team"]
    up = state["upgrades"]
    state["ai_dev"][old] = round(state.get("inherited_dev", 0.0) + PACE_PER_LEVEL_S * (up["power"] + up["aero"]), 3)
    state["inherited_dev"] = state["ai_dev"].pop(team)
    state["upgrades"] = {a: 0 for a in AREAS}
    state["team"] = team
    state["budget"] = round(START_BUDGET_BASE + 4.0 * _ORDER.index(team), 1)
    state["dev_points"] = season_allowance(_ORDER.index(team) + 1)
    # The old team's own drivers stay there; anyone you signed goes back on the market.
    old_codes = {d[0] for d in _TEAM_DRIVERS[old]}
    new_codes = {d[0] for d in _TEAM_DRIVERS[team]}
    released = [{**d, "years": 0} for d in state["drivers"] if d["code"] not in old_codes and not d.get("retiring")]
    state["market"] = [d for d in state["market"] + released if d["code"] not in old_codes | new_codes]
    elapsed = state["season_no"] - 1
    state["drivers"] = [_driver_entry(c, n, d, cons, craft, age=AGES[c] + elapsed, years=rng.choice((1, 2)))
                        for c, n, tm, d, cons, craft in _TEAM_DRIVERS[team]]
    state["status"], state["offers"], state["warning"] = "active", [], False
    state["teams"] = state.get("teams", [old]) + [team]
    set_objectives(state)


def end_career(state: dict) -> None:
    """Turn down every offer and walk away."""
    if state.get("status") != "sacked":
        raise CareerError("You can only walk away after being sacked")
    state["status"], state["offers"] = "over", []
