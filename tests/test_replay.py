"""Replay and race-chart data: frames for the track map, per-lap gaps and positions, and the
replay message sent once at the flag (solo and shared league races)."""
import json
from types import SimpleNamespace

import pytest

from server import league_race
from server import main as server_main
from sim.engine import REPLAY_FRAME_S, RaceConfig, RaceEngine
from sim.replay import lap_table
from tests.conftest import ORIGIN
from tests.test_league_race import both_ready, close, recv
from tests.test_server import start


def run(laps=8, seed=3, **kw) -> RaceEngine:
    e = RaceEngine(RaceConfig(laps=laps, seed=seed, controlled_team="aurora", **kw))
    while not e.finished:
        e.step(1.0)
    return e


@pytest.fixture(scope="module")
def race():
    e = run()
    return e, e.replay()


def test_frames_start_on_the_grid_and_end_at_the_flag(race):
    e, r = race
    frames = r["frames"]
    assert frames[0][0] == 0.0 and all(d <= 0 for d in frames[0][1])   # everyone behind the line
    assert set(frames[0][2]) == {"r"}
    assert set(frames[-1][2]) <= {"f", "x"} and frames[-1][0] == pytest.approx(e.t, abs=0.01)
    times = [f[0] for f in frames]
    assert times == sorted(times)
    steps = [b - a for a, b in zip(times, times[1:-1])]
    assert all(REPLAY_FRAME_S - 0.01 <= s <= REPLAY_FRAME_S + 0.3 for s in steps)
    assert r["frame_s"] == REPLAY_FRAME_S
    assert all(len(f[1]) == len(f[2]) == len(r["cars"]) == 20 and f[3] in "gsv" for f in frames)


def test_frames_carry_the_safety_car_flag():
    e = RaceEngine(RaceConfig(laps=12, seed=7, controlled_team="aurora", vsc_chance_per_lap=0.5))
    while not e.finished:
        e.step(1.0)
    flags = [f[3] for f in e.frames]
    assert flags[0] == "g" and {"s", "v"} & set(flags)
    # the first neutral frame comes after the first Safety Car / VSC call
    first = next(f[0] for f in e.frames if f[3] != "g")
    called = min(ev["t"] for ev in e.event_log if ev["kind"] in ("sc", "vsc"))
    assert called <= first <= called + 2.5


def test_cars_only_move_forward(race):
    _, r = race
    for i in range(len(r["cars"])):
        d = [f[1][i] for f in r["frames"]]
        assert all(b >= a - 1e-3 for a, b in zip(d, d[1:]))
    winner = r["frames"][-1][1]
    assert max(winner) == pytest.approx(r["total_laps"], abs=0.2)


def test_pit_stops_show_in_frames(race):
    _, r = race
    stopped = {r["cars"][i]["code"] for f in r["frames"] for i, s in enumerate(f[2]) if s == "p"}
    pitted = {code for code, laps in r["laps"].items() if any(l[4] for l in laps)}
    assert stopped and stopped == pitted


def test_gap_to_leader_per_lap(race):
    e, r = race
    assert len(r["lap_t"]) == e.cfg.laps and r["lap_t"] == sorted(r["lap_t"])
    for n in range(e.cfg.laps):
        row = [g[n] for g in r["gaps"].values() if g[n] is not None]
        assert min(row) == 0.0 and all(g >= 0 for g in row)
    # the gap matches the cars' own line-crossing times
    for c in e.cars:
        for rec in c.laps:
            assert r["gaps"][c.code][rec["lap"] - 1] == pytest.approx(rec["t"] - r["lap_t"][rec["lap"] - 1], abs=1e-3)


def test_positions_per_lap(race):
    e, r = race
    pos = r["positions"]
    assert sorted(p[0] for p in pos.values()) == list(range(1, 21))     # the grid
    for n in range(1, e.cfg.laps + 1):
        row = sorted(p[n] for p in pos.values() if p[n] is not None)
        assert row == list(range(1, len(row) + 1))
    # whoever crossed the line first on the final lap won
    winner = next(code for code, p in pos.items() if p[-1] == 1)
    assert winner == e.order()[0].code


def test_lap_records_carry_race_time(race):
    e, r = race
    for c in e.cars:
        for (lap, t, time_, tyre, pit), rec in zip(r["laps"][c.code], c.laps):
            assert (lap, t, time_, tyre, pit) == (rec["lap"], rec["t"], rec["time"], rec["tyre"], rec["pit"])
        ts = [rec["t"] for rec in c.laps]
        assert ts == sorted(ts)


def test_replay_is_deterministic_and_json_safe():
    a, b = run(laps=5, seed=11).replay(), run(laps=5, seed=11).replay()
    assert a == b
    text = json.dumps(a)
    assert "advice" not in text and len(text) < 400_000
    assert set(a) == {"type", "track", "total_laps", "frame_s", "cars", "frames", "lap_t", "gaps", "positions",
                      "laps", "events", "commentary", "highlights", "finished"}
    assert a["events"] and all({"t", "lap", "kind", "text", "code"} == set(ev) for ev in a["events"])


def test_lap_table_handles_retirements_and_lapped_cars():
    def car(code, grid, ts):
        return SimpleNamespace(code=code, grid=grid, laps=[{"lap": i + 1, "t": t} for i, t in enumerate(ts)])
    cars = [car("AAA", 2, [90, 180, 270]), car("BBB", 1, [91, 185]), car("CCC", 3, [95, 200, 300])]
    lap_t, gaps, pos = lap_table(cars)
    assert lap_t == [90, 180, 270]
    assert gaps == {"AAA": [0, 0, 0], "BBB": [1, 5, None], "CCC": [5, 20, 30]}
    assert pos == {"AAA": [2, 1, 1, 1], "BBB": [1, 2, 2, None], "CCC": [3, 3, 3, 2]}
    assert lap_table([]) == ([], {}, {})


def test_replay_is_cached_once_finished(race):
    e, r = race
    assert e.replay() is e.replay()


def test_ws_sends_replay_once_after_the_flag(client, monkeypatch):
    fast = RaceEngine.step
    monkeypatch.setattr(server_main.RaceEngine, "step", lambda self, dt: fast(self, dt * 200))
    with client.websocket_connect("/ws/race", headers=ORIGIN) as ws:
        start(ws, laps=5, seed=4)
        seen = []
        for _ in range(400):
            m = ws.receive_json()
            seen.append(m["type"])
            if m["type"] == "replay":
                break
        assert "replay" in seen, seen[-5:]
        assert m["finished"] and len(m["lap_t"]) == 5 and len(m["cars"]) == 20
        assert seen[-2] == "state"                  # sent right after the final state


def test_league_players_both_get_the_replay(league, alice, bob):
    lg, _ = league
    wa, wb = both_ready(alice, bob, lg)
    recv(wa, "init"); recv(wb, "init")
    room = league_race._rooms[lg["id"]]
    with room.lock:
        while not room.engine.finished:
            room.engine.step(5.0)
        room.save_result()
        room._take_snapshot()
    ra, rb = recv(wa, "replay"), recv(wb, "replay")
    assert ra == rb and ra["finished"] and "advice" not in json.dumps(ra)
    close(wa, wb)


from tests.test_league_race import league, no_rooms  # noqa: E402,F401  (fixtures)
