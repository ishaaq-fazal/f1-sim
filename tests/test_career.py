"""Career mode: budget, upgrades, contracts, season after season, and that careers stay private."""
import copy
import json
import statistics

import pytest
from sqlmodel import Session, select

from server import repo
from server.db import Career, Season, SeasonResult, engine
from sim import RaceConfig, RaceEngine
from sim import career as cr
from sim.season import ROUNDS, track_for_round
from tests.conftest import ORIGIN, PASSWORD


# ---------------------------------------------------------------- rules (no database)
def test_new_career_starts_with_the_teams_drivers_and_more_money_for_slower_teams():
    fast, slow = cr.new_career("aurora", 1), cr.new_career("kestrel", 1)
    assert [d["code"] for d in fast["drivers"]] == ["MAR", "OKA"]
    assert all(d["years"] >= 1 for d in fast["drivers"])
    assert slow["budget"] > fast["budget"]
    assert "aurora" not in fast["ai_dev"] and len(fast["ai_dev"]) == 9
    assert all(v == 0 for v in fast["upgrades"].values())
    with pytest.raises(cr.CareerError):
        cr.new_career("nobody", 1)


def test_race_grid_has_20_unique_drivers_and_upgrades_make_your_car_faster():
    st = cr.new_career("solace", 1)
    grid = cr.race_grid(st)
    assert len(grid) == 20 and len({d.code for d in grid}) == 20
    before = {d.code: d.pace_s for d in grid if d.team == "solace"}
    st["upgrades"]["power"], st["upgrades"]["aero"] = 3, 2
    after = {d.code: d.pace_s for d in cr.race_grid(st) if d.team == "solace"}
    assert all(after[c] == pytest.approx(before[c] - 5 * cr.PACE_PER_LEVEL_S) for c in before)
    st["upgrades"]["tyres"], st["upgrades"]["reliability"] = 2, 2
    assert cr.team_wear(st)["solace"] < 1 and cr.team_dnf(st)["solace"] < 1


def test_upgrades_cost_more_each_level_and_need_the_money():
    st = cr.new_career("aurora", 1)
    st["budget"] = 20.0
    assert cr.buy_upgrade(st, "power") == 6.0 and st["budget"] == 14.0
    assert cr.buy_upgrade(st, "power") == 10.0 and st["budget"] == 4.0
    with pytest.raises(cr.CareerError, match="Not enough budget"):
        cr.buy_upgrade(st, "power")
    with pytest.raises(cr.CareerError):
        cr.buy_upgrade(st, "warp-drive")
    assert st["upgrades"]["power"] == 2 and st["spent"]["upgrades"] == 16.0


def test_signing_a_driver_buys_out_the_old_contract_and_swaps_seats():
    st = cr.new_career("aurora", 1)
    oka = next(d for d in st["drivers"] if d["code"] == "OKA")
    cost = cr.sign_driver(st, "QUI", "OKA", years=2, rounds_done=0)
    assert cost == pytest.approx(0.5 * oka["salary"] * oka["years"], abs=0.1)
    assert {d["code"] for d in st["drivers"]} == {"MAR", "QUI"}
    assert any(d["code"] == "OKA" and d["years"] == 0 for d in st["market"])
    assert next(d for d in st["drivers"] if d["code"] == "QUI")["years"] == 2
    with pytest.raises(cr.CareerError, match="before the first race"):
        cr.sign_driver(st, "WAR", "MAR", years=1, rounds_done=3)
    with pytest.raises(cr.CareerError):
        cr.sign_driver(st, "WAR", "MAR", years=5, rounds_done=0)
    with pytest.raises(cr.CareerError):
        cr.sign_driver(st, "TAK", "MAR", years=1, rounds_done=0)    # TAK is under contract elsewhere


def test_end_of_season_pays_prizes_and_wages_ticks_contracts_and_rivals_improve():
    st = cr.new_career("verdant", 1)
    budget, wages = st["budget"], sum(d["salary"] for d in st["drivers"])
    years = [d["years"] for d in st["drivers"]]
    summary = cr.end_season(st, team_position=3, team_points=120, champion="MAR", seed=5)
    assert st["budget"] == pytest.approx(budget + cr.SEASON_PRIZE[2] - wages, abs=0.1)
    assert [d["years"] for d in st["drivers"]] == [y - 1 for y in years]
    assert all(v > 0 for v in st["ai_dev"].values())
    assert st["season_no"] == 2 and st["history"] == [summary] and summary["team_position"] == 3


def test_expired_contracts_must_be_renewed_before_racing():
    st = cr.new_career("aurora", 1)
    for d in st["drivers"]:
        d["years"] = 0
    with pytest.raises(cr.CareerError, match="run out"):
        cr.ready_to_race(st)
    old_salary = st["drivers"][0]["salary"]
    cr.renew_driver(st, st["drivers"][0]["code"], 2, rounds_done=0)
    assert st["drivers"][0]["years"] == 2 and st["drivers"][0]["salary"] > old_salary
    with pytest.raises(cr.CareerError, match="hasn't run out"):
        cr.renew_driver(st, st["drivers"][0]["code"], 2, rounds_done=0)
    cr.renew_driver(st, st["drivers"][1]["code"], 1, rounds_done=0)
    cr.ready_to_race(st)


def _avg_finish(state, seeds=range(8)):
    places = []
    for seed in seeds:
        e = RaceEngine(RaceConfig(laps=10, seed=seed, dnf_chance_per_lap=0, team_wear=cr.team_wear(state)),
                       drivers=cr.race_grid(state))
        while not e.finished:
            e.step(5.0)
        places += [i for i, c in enumerate(e.order(), 1) if c.driver.team == state["team"]]
    return statistics.mean(places)


def test_upgraded_car_finishes_higher_in_real_races():
    st = cr.new_career("ironforge", 1)
    base = _avg_finish(st)
    st["upgrades"].update(power=8, aero=8, tyres=4)
    assert _avg_finish(st) < base - 3


# ---------------------------------------------------------------- API helpers
def new_career(c, team="aurora", laps=5):
    r = c.post("/api/careers", json={"team": team, "laps": laps}, headers=ORIGIN)
    assert r.status_code == 201, r.text
    return r.json()


def race_round(email, career_id):
    """Race the career's next round on the server side and record it, like a finished WS race."""
    with Session(engine) as s:
        user = repo.user_by_email(s, email)
        career = repo.get_career(s, user, career_id)
        season = repo.career_season(s, user, career)
        n = season.rounds_done + 1
        e = RaceEngine(RaceConfig(laps=5, seed=n, controlled_team=career.team, track_id=track_for_round(n)),
                       drivers=cr.race_grid(career.state))
        while not e.finished:
            e.step(5.0)
        repo.record_round(s, user, season.id, n, track_for_round(n), n,
                          [(c.code, c.driver.team, i, c.retired) for i, c in enumerate(e.order(), 1)])


# ---------------------------------------------------------------- API
def test_create_career_and_its_season_is_separate_from_season_mode(alice):
    c = new_career(alice)
    assert c["season_no"] == 1 and c["rounds_done"] == 0 and c["ready"] and len(c["drivers"]) == 2
    assert [u["next_cost"] for u in c["upgrades"]] == [6.0] * 4
    assert alice.get("/api/seasons").json() == []                   # not listed with plain seasons
    assert alice.get(f"/api/seasons/{c['season_id']}").status_code == 200   # but its standings work
    assert alice.delete(f"/api/seasons/{c['season_id']}", headers=ORIGIN).status_code == 404


def test_buying_upgrades_through_the_api(alice):
    c = new_career(alice)
    r = alice.post(f"/api/careers/{c['id']}/upgrade", json={"area": "aero"}, headers=ORIGIN)
    assert r.status_code == 200
    after = r.json()
    assert after["budget"] == pytest.approx(c["budget"] - 6.0)
    assert next(u for u in after["upgrades"] if u["area"] == "aero") == {
        "area": "aero", "label": cr.AREAS["aero"], "level": 1, "next_cost": 10.0}
    for _ in range(20):
        r = alice.post(f"/api/careers/{c['id']}/upgrade", json={"area": "power"}, headers=ORIGIN)
        if r.status_code == 400:
            break
    assert r.json()["detail"] == "Not enough budget"
    assert alice.post(f"/api/careers/{c['id']}/upgrade", json={"area": "nitro"},
                      headers=ORIGIN).status_code == 422


def test_whole_career_season_then_the_next_one(alice):
    c = new_career(alice, team="polaris")
    cid = c["id"]
    assert alice.post(f"/api/careers/{cid}/next-season", headers=ORIGIN).status_code == 400   # not finished
    race_round("alice@example.com", cid)
    after1 = alice.get(f"/api/careers/{cid}").json()
    assert after1["budget"] > c["budget"]                         # prize money after a round
    r = alice.post(f"/api/careers/{cid}/sign", json={"code": "QUI", "replace": "LIN", "years": 1},
                   headers=ORIGIN)
    assert r.status_code == 400 and "before the first race" in r.json()["detail"]
    for _ in range(ROUNDS - 1):
        race_round("alice@example.com", cid)
    done = alice.get(f"/api/careers/{cid}").json()
    assert done["season_finished"] and not done["ready"]
    nxt = alice.post(f"/api/careers/{cid}/next-season", headers=ORIGIN)
    assert nxt.status_code == 200, nxt.text
    s2 = nxt.json()
    assert s2["season_no"] == 2 and s2["rounds_done"] == 0 and s2["season_id"] != c["season_id"]
    assert len(s2["history"]) == 1 and 1 <= s2["history"][0]["team_position"] <= 10
    assert alice.post(f"/api/careers/{cid}/next-season", headers=ORIGIN).status_code == 400   # only once
    # pre-season again: now drivers can be signed
    r = alice.post(f"/api/careers/{cid}/sign", json={"code": "QUI", "replace": "FER", "years": 2}, headers=ORIGIN)
    assert r.status_code == 200 and {d["code"] for d in r.json()["drivers"]} >= {"QUI"}


def test_ws_career_race_uses_your_signed_drivers(alice):
    c = new_career(alice, team="kestrel")
    alice.post(f"/api/careers/{c['id']}/sign", json={"code": "QUI", "replace": "ELL", "years": 2}, headers=ORIGIN)
    with alice.websocket_connect("/ws/race", headers=ORIGIN) as ws:
        ws.send_text(json.dumps({"type": "start", "team": "aurora", "laps": 5, "season": c["season_id"],
                                 "tyres": {"QUI": "S"}}))
        init = ws.receive_json()
        assert init["type"] == "init" and init["team"] == "kestrel"
        assert init["season"]["career"] == {"id": c["id"], "season_no": 1}
        state = ws.receive_json()
        mine = {x["code"]: x for x in state["cars"] if x["controlled"]}
        assert set(mine) == {"KOV", "QUI"} and mine["QUI"]["tyre"] == "S"


def test_ws_refuses_a_career_race_without_two_contracted_drivers(alice):
    c = new_career(alice)
    with Session(engine) as s:
        row = s.get(Career, c["id"])
        st = copy.deepcopy(row.state)
        st["drivers"][0]["years"] = 0
        row.state = st
        s.add(row)
        s.commit()
    with alice.websocket_connect("/ws/race", headers=ORIGIN) as ws:
        ws.send_text(json.dumps({"type": "start", "team": "aurora", "laps": 5, "season": c["season_id"]}))
        assert "run out" in ws.receive_json()["detail"]


# ---------------------------------------------------------------- authorisation
def test_careers_are_private(alice, bob):
    c = new_career(alice)
    cid = c["id"]
    assert bob.get("/api/careers").json() == []
    assert bob.get(f"/api/careers/{cid}").status_code == 404
    assert bob.get(f"/api/seasons/{c['season_id']}").status_code == 404
    for path, body in ((f"/api/careers/{cid}/upgrade", {"area": "power"}),
                       (f"/api/careers/{cid}/sign", {"code": "QUI", "replace": "MAR", "years": 1}),
                       (f"/api/careers/{cid}/next-season", None)):
        assert bob.post(path, json=body, headers=ORIGIN).status_code == 404
    assert bob.delete(f"/api/careers/{cid}", headers=ORIGIN).status_code == 404
    assert alice.get(f"/api/careers/{cid}").json()["budget"] == c["budget"]      # nothing changed
    with bob.websocket_connect("/ws/race", headers=ORIGIN) as ws:
        ws.send_text(json.dumps({"type": "start", "team": "aurora", "laps": 5, "season": c["season_id"]}))
        assert ws.receive_json()["detail"] == "Season not found"


def test_career_responses_do_not_leak_internal_fields(alice):
    c = new_career(alice)
    assert "owner_id" not in c and "state" not in c and "seed" not in c and "ai_dev" not in c
    assert set(c["drivers"][0]) == {"code", "name", "rating", "salary", "years"}


def test_viewers_cannot_start_a_career(admin, bob):
    bob_id = bob.get("/api/me").json()["id"]
    admin.patch(f"/api/admin/users/{bob_id}/role", json={"role": "viewer"}, headers=ORIGIN)
    bob.post("/api/auth/login", json={"email": "bob@example.com", "password": PASSWORD})
    assert bob.post("/api/careers", json={"team": "aurora", "laps": 5}, headers=ORIGIN).status_code == 403


def test_career_input_is_validated(alice):
    for body in ({"team": "nobody", "laps": 5}, {"team": "aurora", "laps": 1},
                 {"team": "aurora", "laps": 5, "budget": 9999}):
        assert alice.post("/api/careers", json=body, headers=ORIGIN).status_code in (400, 422)
    c = new_career(alice)
    for body in ({"code": "qui", "replace": "MAR", "years": 1}, {"code": "QUI", "replace": "MAR", "years": 9},
                 {"code": "QUI", "replace": "MAR", "years": 1, "fee": 0}):
        assert alice.post(f"/api/careers/{c['id']}/sign", json=body, headers=ORIGIN).status_code == 422
    assert alice.get("/api/careers/1%20OR%201=1").status_code == 422


def test_deleting_a_career_removes_its_seasons_and_results(alice):
    c = new_career(alice)
    race_round("alice@example.com", c["id"])
    assert alice.delete(f"/api/careers/{c['id']}", headers=ORIGIN).status_code == 204
    assert alice.get(f"/api/careers/{c['id']}").status_code == 404
    with Session(engine) as s:
        assert s.exec(select(Season).where(Season.career_id == c["id"])).all() == []
        assert s.exec(select(SeasonResult).where(SeasonResult.season_id == c["season_id"])).all() == []
