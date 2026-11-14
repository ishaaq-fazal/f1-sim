"""Live race decisions: radio calls, team orders, Safety Cars and VSCs."""
import json

import pytest
from sqlmodel import Session

import sim.engine as eng
from server import league_race, repo
from server.db import engine as db_engine
from sim.engine import RaceConfig, RaceEngine
from sim.grid import LOYALTY, default_grid, loyalty_for
from tests.conftest import ORIGIN
from tests.test_league_race import both_ready, close, league, no_rooms, recv, recv_error  # noqa: F401
from tests.test_server import next_of, start


def quiet(**kw):
    """A race with no random incidents, so tests choose when the Safety Car comes out."""
    cfg = dict(laps=30, seed=7, controlled_team="aurora", vsc_chance_per_lap=0, crash_chance_per_lap=0,
               dnf_chance_per_lap=0, mistake_chance_per_lap=0)
    cfg.update(kw)
    return RaceEngine(RaceConfig(**cfg))


def run_laps(e, laps):
    target = e.leader().lap + laps
    while e.leader().lap < target and not e.finished:
        e.step(1.0)


def force(e, monkeypatch, kind):
    monkeypatch.setitem(eng.SC_SHARE, "debris", 1.0 if kind == "sc" else 0.0)
    e._incident("debris", "debris on track")


def spread_s(e):
    cars = [c for c in e.order() if c.running and c.pit_timer <= 0]
    return (cars[0].dist - cars[-1].dist) * e.track.base_lap_s


# --------------------------------------------------------------- safety car

def test_safety_car_bunches_the_field_then_goes_back_in(monkeypatch):
    e = quiet(controlled_team=None)
    run_laps(e, 8)
    gaps = lambda: [(a.dist - b.dist) * e.track.base_lap_s for a, b in zip(e.order(), e.order()[1:])]
    assert max(gaps()) > 1.5
    force(e, monkeypatch, "sc")
    for c in e.cars:
        c.plan.stops, c.advice = [], None       # nobody pits, so we see the queue form
    assert e.sc and e.neutral == "sc"
    end = e.sc_end_lap
    run_laps(e, end - e.leader().lap - 1)
    assert e.sc
    assert max(gaps()) < eng.SC_GAP_S           # everyone has closed up behind the SC
    run_laps(e, 2)
    assert not e.sc
    kinds = [ev["kind"] for ev in e.snapshot()["events"]]
    assert "green" in kinds


def test_no_overtaking_under_the_safety_car(monkeypatch):
    e = quiet(controlled_team=None)
    run_laps(e, 5)
    force(e, monkeypatch, "sc")
    e.snapshot()
    for c in e.cars:
        c.plan.stops = []                      # nobody pits, so the order can only change by passing
        c.advice = None
    run_laps(e, 1)
    order = [c.code for c in e.order()]
    run_laps(e, 1)
    assert [c.code for c in e.order()] == order
    assert not [ev for ev in e.snapshot()["events"] if ev["kind"] == "overtake"]


def test_vsc_freezes_gaps(monkeypatch):
    e = quiet(controlled_team=None)
    run_laps(e, 6)
    for c in e.cars:
        c.plan.stops = []
    force(e, monkeypatch, "vsc")
    assert e.neutral == "vsc"
    before = spread_s(e)
    e.step(30.0)
    assert e.vsc
    assert abs(spread_s(e) - before) < 2.0      # gaps hold instead of collapsing


def test_stops_are_about_half_price_under_sc_and_a_bit_cheaper_under_vsc(monkeypatch):
    e = quiet()
    full = e.stop_cost()
    assert full == pytest.approx(e.track.pit_loss_s)
    force(e, monkeypatch, "vsc")
    vsc = e.stop_cost()
    e.vsc_until = -1
    force(e, monkeypatch, "sc")
    sc = e.stop_cost()
    assert 0.4 * full < sc < 0.6 * full
    assert sc < vsc < 0.8 * full


def test_box_now_advice_for_worn_tyres_and_fresh_tyres(monkeypatch):
    e = quiet()
    run_laps(e, 14)
    force(e, monkeypatch, "sc")
    mar = e.car("MAR")
    mar.wear, mar.tyre, mar.used = 14.0, "S", {"S"}
    worn = e.box_advice(mar)
    assert worn["box"] and worn["compound"] in ("M", "H") and worn["saving_s"] > 1
    mar.wear, mar.used = 0.0, {"S", "M"}
    mar.tyre = "M"
    assert not e.box_advice(mar)["box"]
    snap = e.snapshot()
    mine = {c["code"]: c for c in snap["cars"]}
    assert snap["sc"] and snap["neutral"] == "sc" and snap["neutral_id"] == 1
    assert mine["MAR"]["advice"] is not None
    assert all(c["advice"] is None for c in snap["cars"] if not c["controlled"])   # AI calls stay hidden
    json.dumps(snap)                                                                # plain JSON types


def test_ai_teams_take_cheap_stops_under_the_safety_car(monkeypatch):
    e = quiet(controlled_team=None)
    run_laps(e, 13)
    force(e, monkeypatch, "sc")
    run_laps(e, 2)
    texts = [ev["text"] for ev in e.snapshot()["events"]]
    assert sum("under the Safety Car" in t for t in texts) >= 3


def test_crashes_bring_out_neutralisations_and_races_still_finish():
    sc = vsc = crashes = 0
    for seed in range(12):
        e = RaceEngine(RaceConfig(laps=25, seed=seed, crash_chance_per_lap=0.004))
        while not e.finished:
            e.step(2.0)
        sc, vsc = sc + e.sc_count, vsc + e.vsc_count
        crashes += sum(1 for c in e.cars if c.retired)
        assert e.sc_count + e.vsc_count <= eng.MAX_NEUTRAL
        assert any(c.finished for c in e.cars)
    assert crashes and sc and vsc


# --------------------------------------------------------------- radio calls

def test_player_pace_calls_go_out_over_the_radio():
    e = quiet()
    assert e.set_mode("MAR", "conserve")
    assert e.set_mode("OKA", "push")
    radio = [ev["text"] for ev in e.snapshot()["events"] if ev["kind"] == "radio"]
    assert any("MAR: save the tyres" in t for t in radio) and any("OKA: push" in t for t in radio)


def test_ai_saves_tyres_on_a_long_stint_and_pushes_at_the_end():
    e = quiet(controlled_team=None)
    run_laps(e, 3)
    car = e.car("ROS")
    car.tyre, car.wear, car.plan.stops = "S", 10.0, []
    e._ai_radio(car)
    assert car.mode == "conserve"
    car.lap, car.tyre, car.wear = e.cfg.laps - 3, "H", 5.0
    e._ai_radio(car)
    assert car.mode == "push"


def test_drivers_have_a_stable_team_player_temperament():
    grid = default_grid()
    assert all(d.loyalty == LOYALTY[d.code] for d in grid)
    assert loyalty_for("ZED") == loyalty_for("ZED") and 0.3 <= loyalty_for("ZED") <= 0.86


def test_loyal_drivers_obey_more_and_nobody_likes_giving_up_the_lead():
    e = quiet()
    oka, mar = e.car("OKA"), e.car("MAR")        # OKA is loyal, MAR isn't
    assert e.swap_chance(oka) > e.swap_chance(mar)
    leader = e.order()[0]
    back = e.order()[-1]
    if leader.driver.loyalty <= back.driver.loyalty:
        assert e.swap_chance(leader) < e.swap_chance(back)


def test_swap_positions_lets_the_team_mate_through(monkeypatch):
    e = quiet()
    run_laps(e, 3)
    mar, oka = e.car("MAR"), e.car("OKA")
    # Put the team-mates nose to tail, OKA ahead.
    oka.frac, oka.lap = mar.frac + 0.004, mar.lap
    monkeypatch.setattr(e, "swap_chance", lambda car: 1.0)
    assert e.team_order("aurora") is None
    assert oka.yield_to == "MAR"
    e.step(40.0)
    assert mar.dist > oka.dist
    radio = [ev["text"] for ev in e.snapshot()["events"] if ev["kind"] == "radio"]
    assert any("let MAR through" in t for t in radio)


def test_a_driver_can_ignore_team_orders(monkeypatch):
    e = quiet()
    run_laps(e, 3)
    mar, oka = e.car("MAR"), e.car("OKA")
    oka.frac, oka.lap = mar.frac + 0.004, mar.lap
    monkeypatch.setattr(e, "swap_chance", lambda car: 0.0)
    assert e.team_order("aurora") is None
    assert oka.yield_to is None
    assert any("ignores the order" in ev["text"] for ev in e.snapshot()["events"])


def test_swap_needs_the_team_mates_close_together():
    e = quiet()
    run_laps(e, 3)
    mar, oka = e.car("MAR"), e.car("OKA")
    oka.frac, oka.lap = mar.frac + 0.3, mar.lap
    assert "too far apart" in e.team_order("aurora")
    oka.retired = True
    assert "Both drivers" in e.team_order("aurora")


# --------------------------------------------------------------- server

def test_ws_team_orders_only_ever_apply_to_your_own_team(client, monkeypatch):
    seen = []
    monkeypatch.setattr(RaceEngine, "team_order", lambda self, team: seen.append(team) or "nope")
    with client.websocket_connect("/ws/race", headers=ORIGIN) as ws:
        start(ws, team="vantage")
        ws.send_text(json.dumps({"type": "order", "order": "swap"}))
        assert next_of(ws, "error")["detail"] == "nope"
    assert seen == ["vantage"]


def test_ws_rejects_unknown_team_orders(client):
    with client.websocket_connect("/ws/race", headers=ORIGIN) as ws:
        start(ws)
        ws.send_text(json.dumps({"type": "order", "order": "crash-my-rival"}))
        assert next_of(ws, "error")


def test_league_team_orders_and_box_advice_stay_with_their_owner(league, alice, bob, monkeypatch):
    lg, _ = league
    seen = []
    monkeypatch.setattr(RaceEngine, "team_order", lambda self, team: seen.append(team) or "nope")
    wa, wb = both_ready(alice, bob, lg)
    recv(wa, "init"); recv(wb, "init")
    wb.send_text(json.dumps({"type": "order", "order": "swap"}))
    assert recv_error(wb) == "nope"
    assert seen == ["vantage"]                         # Bobby's own team, never Ali's
    room = league_race._rooms[lg["id"]]
    with room.lock:
        room.snap["cars"] = [{**c, "advice": {"box": True}} for c in room.snap["cars"]]
    with Session(db_engine) as s:
        for uid, p in room.players.items():
            st = room.state_for(repo.user_by_id(s, uid), 0)
            with_advice = {c["team"] for c in st["cars"] if c["advice"]}
            assert with_advice == {p["team"]}
    close(wa, wb)
