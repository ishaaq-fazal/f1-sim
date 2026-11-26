"""Career mode, part two: development points, the driver market over time, and the board."""
import copy
import json
import random

import pytest
from sqlmodel import Session

from server import repo
from server.db import Career, engine
from sim import RaceConfig, RaceEngine
from sim import career as cr
from sim.grid import TEAMS
from sim.season import ROUNDS
from tests.conftest import ORIGIN
from tests.test_career import new_career, race_round

ORDER = [t[0] for t in TEAMS]


def positions(first: str) -> dict[str, int]:
    """Teams' standings with `first` in P1 and everyone else in default order."""
    rest = [t for t in ORDER if t != first]
    return {t: i for i, t in enumerate([first] + rest, 1)}


def last_place(team: str) -> dict[str, int]:
    rest = [t for t in ORDER if t != team]
    return {t: i for i, t in enumerate(rest + [team], 1)}


# ---------------------------------------------------------------- development points
def test_results_earn_development_points_and_small_teams_get_more_factory_time():
    st = cr.new_career("solace", 1)
    dp = st["dev_points"]
    # A season's worth of development is spread over its rounds: the share left over from one
    # round carries into the next, so a full season of scoreless rounds still earns 10 DP.
    for _ in range(cr.ROUNDS):
        cr.credit_round(st, 0)
    assert st["dev_points"] == dp + cr.ECONOMY_ROUNDS * cr.DP_PER_ROUND_BASE
    before = st["dev_points"]
    for _ in range(cr.ROUNDS):
        cr.credit_round(st, 43)
    assert st["dev_points"] == before + int(cr.ECONOMY_ROUNDS * (1 + 4.3) + 1e-6)
    assert cr.season_allowance(10) > cr.season_allowance(1)
    assert cr.new_career("kestrel", 1)["dev_points"] > cr.new_career("aurora", 1)["dev_points"]


def test_funding_the_factory_turns_money_into_development_points():
    st = cr.new_career("aurora", 1)
    st["budget"], st["dev_points"] = 15.0, 0
    cr.fund_factory(st)
    assert st["budget"] == 5.0 and st["dev_points"] == cr.FACTORY_DP
    with pytest.raises(cr.CareerError, match="Not enough budget"):
        cr.fund_factory(st)


def test_pit_crew_upgrades_make_stops_quicker():
    st = cr.new_career("halcyon", 1)
    assert cr.team_pit(st) == {"halcyon": 0.0}
    st["upgrades"]["pit"] = 8

    def avg_stop(pit):
        losses = []
        for seed in range(6):
            e = RaceEngine(RaceConfig(laps=12, seed=seed, dnf_chance_per_lap=0, team_pit=pit),
                           drivers=cr.race_grid(st))
            seen = {}
            while not e.finished:
                e.step(0.5)
                for c in e.cars:
                    if c.driver.team == "halcyon" and c.pits > seen.get(c.code, 0):
                        seen[c.code] = c.pits
                        losses.append(c.pit_timer)
        assert losses
        return sum(losses) / len(losses)

    assert avg_stop(cr.team_pit(st)) < avg_stop(None) - 0.4


# ---------------------------------------------------------------- drivers over time
def test_young_drivers_improve_and_veterans_fade():
    rng = random.Random(3)
    young = [cr._driver_entry("AAA", "A", 0.15, 0.28, 0.5, age=19) for _ in range(30)]
    old = [cr._driver_entry("BBB", "B", 0.0, 0.24, 0.6, age=36) for _ in range(30)]
    for d in young + old:
        cr.develop_driver(d, rng)
    assert sum(d["change"] for d in young) / 30 > 2
    assert sum(d["change"] for d in old) / 30 < -2
    assert young[0]["age"] == 20


def test_beating_your_team_mate_lifts_your_rating():
    rng_a, rng_b = random.Random(1), random.Random(1)
    a = cr._driver_entry("AAA", "A", 0.05, 0.24, 0.6, age=30)
    b = copy.deepcopy(a)
    cr.develop_driver(a, rng_a, form=1.0)
    cr.develop_driver(b, rng_b, form=-1.0)
    assert a["rating"] > b["rating"]


def test_off_season_brings_rookies_retires_veterans_and_renewals_follow_the_rating():
    st = cr.new_career("kestrel", 4)            # KOV is 37
    st["drivers"][0]["years"] = st["drivers"][1]["years"] = 1
    market_before = {d["code"] for d in st["market"]}
    target = next(o["target"] for o in st["objectives"]["items"] if o["kind"] == "points")
    cr.end_season(st, 9, target, "MAR", seed=2, team_positions=last_place("kestrel"),       # warning, not sacked
                  driver_points={"KOV": 6, "ELL": 4})
    kov = next(d for d in st["drivers"] if d["code"] == "KOV")
    assert kov["age"] == 38 and kov["retiring"] and kov["years"] == 0
    with pytest.raises(cr.CareerError, match="retiring"):
        cr.renew_driver(st, "KOV", 1, rounds_done=0)
    rookies = [d for d in st["market"] if d["code"] not in market_before]
    assert len(rookies) == cr.ROOKIES_PER_SEASON and all(d["age"] <= 21 for d in rookies)
    cr.sign_driver(st, rookies[0]["code"], "KOV", 2, rounds_done=0)
    assert all(d["code"] != "KOV" for d in st["market"])          # retired, gone for good
    ell = next(d for d in st["drivers"] if d["code"] == "ELL")
    cr.renew_driver(st, "ELL", 2, rounds_done=0)
    assert ell["salary"] == round(cr.salary(ell["delta"], ell["consistency"], ell["racecraft"]) * 1.1, 1)
    cr.ready_to_race(st)
    codes = [d.code for d in cr.race_grid(st)]
    assert len(codes) == 20 == len(set(codes))


# ---------------------------------------------------------------- the board
def test_objectives_scale_with_the_teams_strength():
    top, bottom = cr.new_career("aurora", 1)["objectives"], cr.new_career("kestrel", 1)["objectives"]
    assert top["expected"] < bottom["expected"]
    pos = lambda o: next(i["target"] for i in o["items"] if i["kind"] == "position")
    pts = lambda o: next(i["target"] for i in o["items"] if i["kind"] == "points")
    assert pos(top) < pos(bottom) and pts(top) > pts(bottom)
    rival = next(i["target"] for i in bottom["items"] if i["kind"] == "rival")
    assert rival != "kestrel"


def test_upgrading_the_car_raises_the_boards_expectations():
    st = cr.new_career("ironforge", 1)
    before = cr.set_objectives(st)["expected"]
    st["upgrades"].update(power=10, aero=10)
    assert cr.set_objectives(st)["expected"] < before


def test_meeting_every_objective_earns_a_bonus():
    st = cr.new_career("polaris", 1)
    dp = st["dev_points"]
    s = cr.end_season(st, 1, 300, "LIN", seed=1, team_positions=positions("polaris"))
    assert s["verdict"] == "bonus" and all(o["met"] for o in s["objectives"])
    assert st["dev_points"] == dp + cr.season_allowance(1) + cr.BOARD_BONUS_DP
    assert st["status"] == "active" and st["objectives"]["season"] == 2


def test_missing_one_objective_is_a_warning_then_missing_again_gets_you_sacked():
    st = cr.new_career("polaris", 1)
    target = next(i["target"] for i in st["objectives"]["items"] if i["kind"] == "points")
    s = cr.end_season(st, 1, target - 5, "LIN", seed=1, team_positions=positions("polaris"))
    assert s["verdict"] == "warning" and st["warning"] and st["status"] == "active"
    target = next(i["target"] for i in st["objectives"]["items"] if i["kind"] == "points")
    s = cr.end_season(st, 1, target - 5, "LIN", seed=2, team_positions=positions("polaris"))
    assert s["verdict"] == "sacked" and st["status"] in ("sacked", "over")


def test_sacked_principals_get_offers_from_weaker_teams_only():
    st = cr.new_career("verdant", 1)
    s = cr.end_season(st, 10, 0, "MAR", seed=1, team_positions=last_place("verdant"))
    assert s["verdict"] == "sacked" and st["status"] == "sacked"
    order = cr.team_strengths(st)
    assert 1 <= len(st["offers"]) <= 2
    assert all(order.index(t) > order.index("verdant") for t in st["offers"])
    with pytest.raises(cr.CareerError, match="sacked"):
        cr.ready_to_race(st)
    for act in (lambda: cr.buy_upgrade(st, "aero"), lambda: cr.fund_factory(st),
                lambda: cr.end_season(st, 1, 1, "MAR", seed=1)):
        with pytest.raises(cr.CareerError):
            act()
    with pytest.raises(cr.CareerError, match="offered"):
        cr.accept_offer(st, "aurora", seed=1)


def test_taking_a_job_offer_moves_you_to_the_new_team():
    st = cr.new_career("verdant", 1)
    st["upgrades"]["aero"] = 4
    st["market"].append({**st["drivers"][1], "years": 0})          # NOV released onto the market
    st["drivers"][1] = {**next(d for d in st["market"] if d["code"] == "QUI"), "years": 2}
    st["market"] = [d for d in st["market"] if d["code"] != "QUI"]
    cr.end_season(st, 10, 0, "MAR", seed=1, team_positions=last_place("verdant"))
    new_team = st["offers"][0]
    cr.accept_offer(st, new_team, seed=1)
    assert st["team"] == new_team and st["status"] == "active" and st["teams"] == ["verdant", new_team]
    assert {d["code"] for d in st["drivers"]} == {d[0] for d in cr._TEAM_DRIVERS[new_team]}
    assert all(d["years"] >= 1 for d in st["drivers"]) and all(v == 0 for v in st["upgrades"].values())
    assert "verdant" in st["ai_dev"] and new_team not in st["ai_dev"]
    assert st["ai_dev"]["verdant"] >= 4 * cr.PACE_PER_LEVEL_S          # your old car keeps its upgrades
    market = {d["code"] for d in st["market"]}
    assert "QUI" in market and "NOV" not in market                     # NOV races for Verdant again
    codes = [d.code for d in cr.race_grid(st)]
    assert len(codes) == 20 == len(set(codes))
    cr.ready_to_race(st)


def test_sacked_from_the_weakest_team_means_career_over():
    st = cr.new_career("kestrel", 1)
    s = cr.end_season(st, 10, 0, "MAR", seed=1, team_positions=last_place("kestrel"))
    assert s["verdict"] == "sacked" and st["status"] == "over" and st["offers"] == []


def test_walking_away_ends_the_career():
    st = cr.new_career("verdant", 1)
    with pytest.raises(cr.CareerError):
        cr.end_career(st)                                # still employed
    cr.end_season(st, 10, 0, "MAR", seed=1, team_positions=last_place("verdant"))
    cr.end_career(st)
    assert st["status"] == "over"


def test_old_saved_careers_are_brought_up_to_date():
    st = cr.new_career("solace", 1)
    for k in ("dev_points", "status", "objectives", "warning", "offers", "inherited_dev", "teams", "rookies_used"):
        st.pop(k)
    st["upgrades"].pop("pit")
    for d in st["drivers"] + st["market"]:
        for k in ("age", "growth", "change", "retiring"):
            d.pop(k)
    new = cr.migrate(st)
    assert new is not st and new["status"] == "active" and new["upgrades"]["pit"] == 0
    assert new["objectives"]["items"] and all("age" in d for d in new["drivers"])
    assert cr.migrate(new) is new
    cr.ready_to_race(new)


# ---------------------------------------------------------------- through the API
def _rig_for_the_sack(career_id):
    """Put a career on a warning with objectives it can't meet, straight in the database."""
    with Session(engine) as s:
        row = s.get(Career, career_id)
        st = copy.deepcopy(row.state)
        st["warning"] = True
        for item in st["objectives"]["items"]:
            if item["kind"] == "points":
                item["target"] = 10_000
        row.state = st
        s.add(row)
        s.commit()


def test_api_board_objectives_show_progress(alice):
    c = new_career(alice, team="nimbus")
    assert c["status"] == "active" and not c["warning"] and len(c["objectives"]) == 3
    assert all(o["met"] is None for o in c["objectives"]) and c["offers"] == []
    race_round("alice@example.com", c["id"])
    after = alice.get(f"/api/careers/{c['id']}").json()
    assert all(isinstance(o["met"], bool) for o in after["objectives"])
    assert after["dev_points"] > c["dev_points"]


def test_api_sacked_then_join_a_new_team(alice):
    c = new_career(alice, team="verdant")
    cid = c["id"]
    _rig_for_the_sack(cid)
    for _ in range(ROUNDS):
        race_round("alice@example.com", cid)
    r = alice.post(f"/api/careers/{cid}/next-season", headers=ORIGIN)
    assert r.status_code == 200, r.text
    sacked = r.json()
    assert sacked["status"] == "sacked" and sacked["offers"] and not sacked["ready"]
    assert sacked["history"][-1]["verdict"] == "sacked"
    assert sacked["season_id"] == c["season_id"]                      # no new season while unemployed
    assert alice.post(f"/api/careers/{cid}/next-season", headers=ORIGIN).status_code == 400
    assert alice.post(f"/api/careers/{cid}/upgrade", json={"area": "aero"}, headers=ORIGIN).status_code == 400
    with alice.websocket_connect("/ws/race", headers=ORIGIN) as ws:
        ws.send_text(json.dumps({"type": "start", "team": "verdant", "laps": 5, "season": c["season_id"]}))
        assert ws.receive_json()["type"] == "error"
    # an offer from a team that didn't make one is refused
    no_offer = next(t for t in ORDER if t not in [o["team"] for o in sacked["offers"]])
    assert alice.post(f"/api/careers/{cid}/accept", json={"team": no_offer}, headers=ORIGIN).status_code == 400
    team = sacked["offers"][0]["team"]
    r = alice.post(f"/api/careers/{cid}/accept", json={"team": team}, headers=ORIGIN)
    assert r.status_code == 200, r.text
    hired = r.json()
    assert hired["team"] == team and hired["status"] == "active" and hired["ready"]
    assert hired["season_id"] != c["season_id"] and hired["rounds_done"] == 0
    assert hired["teams_managed"] == ["verdant", team]
    with alice.websocket_connect("/ws/race", headers=ORIGIN) as ws:
        ws.send_text(json.dumps({"type": "start", "team": "aurora", "laps": 5, "season": hired["season_id"]}))
        init = ws.receive_json()
        assert init["type"] == "init" and init["team"] == team


def test_api_walk_away_ends_the_career(alice):
    c = new_career(alice, team="verdant")
    assert alice.post(f"/api/careers/{c['id']}/retire", headers=ORIGIN).status_code == 400
    _rig_for_the_sack(c["id"])
    for _ in range(ROUNDS):
        race_round("alice@example.com", c["id"])
    alice.post(f"/api/careers/{c['id']}/next-season", headers=ORIGIN)
    r = alice.post(f"/api/careers/{c['id']}/retire", headers=ORIGIN)
    assert r.status_code == 200 and r.json()["status"] == "over" and not r.json()["ready"]


def test_another_user_cannot_touch_a_sacked_career(alice, bob):
    c = new_career(alice, team="verdant")
    _rig_for_the_sack(c["id"])
    for _ in range(ROUNDS):
        race_round("alice@example.com", c["id"])
    sacked = alice.post(f"/api/careers/{c['id']}/next-season", headers=ORIGIN).json()
    team = sacked["offers"][0]["team"]
    assert bob.post(f"/api/careers/{c['id']}/accept", json={"team": team}, headers=ORIGIN).status_code == 404
    assert bob.post(f"/api/careers/{c['id']}/retire", headers=ORIGIN).status_code == 404
    assert bob.post(f"/api/careers/{c['id']}/factory", headers=ORIGIN).status_code == 404
    still = alice.get(f"/api/careers/{c['id']}").json()
    assert still["status"] == "sacked" and still["team"] == "verdant"
    # bob's own career is unaffected by alice's offers
    b = new_career(bob, team="aurora")
    assert bob.post(f"/api/careers/{b['id']}/accept", json={"team": team}, headers=ORIGIN).status_code == 400


def test_api_rejects_bad_offer_and_upgrade_input(alice):
    c = new_career(alice)
    for body in ({"team": "AURORA"}, {"team": "x" * 30}, {"team": "kestrel", "budget": 99}, {}):
        assert alice.post(f"/api/careers/{c['id']}/accept", json=body, headers=ORIGIN).status_code == 422
    assert alice.post(f"/api/careers/{c['id']}/upgrade", json={"area": "pit"}, headers=ORIGIN).status_code == 200


def test_old_career_rows_load_and_save_after_the_update(alice):
    c = new_career(alice)
    with Session(engine) as s:
        row = s.get(Career, c["id"])
        st = copy.deepcopy(row.state)
        for k in ("dev_points", "status", "objectives", "warning", "offers"):
            st.pop(k)
        row.state = st
        s.add(row)
        s.commit()
    got = alice.get(f"/api/careers/{c['id']}")
    assert got.status_code == 200 and got.json()["status"] == "active"
    assert alice.post(f"/api/careers/{c['id']}/upgrade", json={"area": "aero"}, headers=ORIGIN).status_code == 200
