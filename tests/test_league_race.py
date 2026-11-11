"""Shared league races: the lobby, ready-up, one grid for every player, and who may command what."""
import json

import pytest
from sqlmodel import Session

from server import league_race, repo
from server.db import Season, engine
from sim.season import ROUNDS, track_for_round
from tests.conftest import ORIGIN, make_client
from tests.test_leagues import create, join


@pytest.fixture(autouse=True)
def no_rooms():
    league_race._rooms.clear()
    yield
    league_race._rooms.clear()


def recv(ws, kind, limit=200):
    """Read messages until one of the given type arrives."""
    for _ in range(limit):
        m = ws.receive_json()
        if m["type"] == kind:
            return m
        assert m["type"] in ("lobby", "state", "init"), m
    raise AssertionError(f"no {kind} message")


def recv_error(ws, limit=200):
    return recv(ws, "error", limit)["detail"]


@pytest.fixture()
def league(alice, bob):
    lg = create(alice)                                  # Ali runs aurora (MAR, OKA)
    b = join(bob, lg["code"]).json()                    # Bobby runs vantage (BRA, VEN)
    return lg, b


def open_lobby(c, league_id):
    ws = c.websocket_connect("/ws/race", headers=ORIGIN).__enter__()
    ws.send_text(json.dumps({"type": "lobby", "league": league_id}))
    return ws


def both_ready(alice, bob, lg):
    wa, wb = open_lobby(alice, lg["id"]), open_lobby(bob, lg["id"])
    recv(wa, "lobby"); recv(wb, "lobby")
    wa.send_text(json.dumps({"type": "ready", "ready": True, "tyres": {"MAR": "S"}}))
    wb.send_text(json.dumps({"type": "ready", "ready": True}))
    return wa, wb


def close(*wss):
    for ws in wss:
        ws.__exit__(None, None, None)


# ---------------------------------------------------------------- lobby
def test_lobby_shows_members_and_who_is_ready(league, alice, bob):
    lg, _ = league
    wa = open_lobby(alice, lg["id"])
    m = recv(wa, "lobby")
    assert m["round"] == 1 and m["track"] == track_for_round(1) and m["rounds"] == ROUNDS and m["host"]
    assert [(p["nickname"], p["team"], p["online"], p["ready"]) for p in m["players"]] == \
           [("Ali", "aurora", True, False), ("Bobby", "vantage", False, False)]
    wa.send_text(json.dumps({"type": "ready", "ready": True}))
    m = recv(wa, "lobby")
    assert m["players"][0]["ready"] and m["status"] == "waiting"      # still waiting for Bobby
    text = json.dumps(m)
    assert "example.com" not in text and "user_id" not in text
    close(wa)


def test_non_members_cannot_open_the_lobby(league, admin):
    lg, _ = league
    carol = make_client("carol@example.com")
    with carol.websocket_connect("/ws/race", headers=ORIGIN) as ws:
        ws.send_text(json.dumps({"type": "lobby", "league": lg["id"]}))
        assert ws.receive_json() == {"type": "error", "detail": "League not found"}
        ws.send_text(json.dumps({"type": "lobby", "league": lg["id"] + 99}))
        assert ws.receive_json() == {"type": "error", "detail": "League not found"}
        ws.send_text(json.dumps({"type": "ready", "ready": True}))
        assert ws.receive_json()["detail"] == "Open a league lobby first"
    assert league_race._rooms == {}


def test_ready_rejects_tyres_for_someone_elses_drivers(league, alice):
    lg, _ = league
    wa = open_lobby(alice, lg["id"])
    recv(wa, "lobby")
    wa.send_text(json.dumps({"type": "ready", "ready": True, "tyres": {"BRA": "S"}}))
    assert recv_error(wa) == "Starting tyres can only be set for your own drivers"
    close(wa)


def test_league_seasons_cant_be_raced_alone(league, alice):
    lg, _ = league
    with alice.websocket_connect("/ws/race", headers=ORIGIN) as ws:
        ws.send_text(json.dumps({"type": "start", "team": "aurora", "laps": 5, "season": lg["your_season_id"]}))
        assert "raced together" in recv_error(ws)


# ---------------------------------------------------------------- the shared race
def test_race_starts_when_everyone_is_ready_with_every_player_on_one_grid(league, alice, bob):
    lg, b = league
    wa, wb = both_ready(alice, bob, lg)
    ia, ib = recv(wa, "init"), recv(wb, "init")
    with Session(engine) as s:
        seed = repo.season_round_seed(s.get(Season, lg["your_season_id"]), 1)
    assert ia["seed"] == ib["seed"] == seed
    assert ia["team"] == "aurora" and ib["team"] == "vantage" and ia["host"] and not ib["host"]
    assert ia["season"]["league"] == lg["id"] and ib["season"]["id"] == b["your_season_id"]
    sa, sb = recv(wa, "state"), recv(wb, "state")
    # each player sees only their own cars as theirs, and the other player's cars by nickname
    assert {c["code"] for c in sa["cars"] if c["controlled"]} == {"MAR", "OKA"}
    assert {c["code"] for c in sb["cars"] if c["controlled"]} == {"BRA", "VEN"}
    assert {c["code"]: c["player"] for c in sa["cars"] if c["player"]} == \
           {"MAR": "Ali", "OKA": "Ali", "BRA": "Bobby", "VEN": "Bobby"}
    assert next(c for c in sa["cars"] if c["code"] == "MAR")["tyre"] == "S"     # Ali's tyre pick
    close(wa, wb)


def test_players_only_command_their_own_cars_and_only_the_owner_sets_speed(league, alice, bob):
    lg, _ = league
    wa, wb = both_ready(alice, bob, lg)
    recv(wa, "init"); recv(wb, "init")
    wb.send_text(json.dumps({"type": "pit", "driver": "MAR", "compound": "H"}))
    assert recv_error(wb) == "Not your driver"
    wb.send_text(json.dumps({"type": "mode", "driver": "OKA", "mode": "push"}))
    assert recv_error(wb) == "Not your driver"
    wb.send_text(json.dumps({"type": "speed", "value": 50}))
    assert recv_error(wb) == "Only the league owner controls speed and pause"
    wb.send_text(json.dumps({"type": "pause", "paused": True}))
    assert recv_error(wb) == "Only the league owner controls speed and pause"
    room = league_race._rooms[lg["id"]]
    assert room.speed == 1 and not room.paused
    wa.send_text(json.dumps({"type": "speed", "value": 20}))
    wb.send_text(json.dumps({"type": "pit", "driver": "BRA", "compound": "H"}))     # his own car: fine
    for _ in range(30):
        st = recv(wb, "state")
        if st["speed"] == 20 and next(c for c in st["cars"] if c["code"] == "BRA")["pit_request"] == "H":
            break
    else:
        raise AssertionError("orders not applied")
    assert next(c for c in st["cars"] if c["code"] == "MAR")["pit_request"] is None
    close(wa, wb)


def test_finished_race_is_saved_into_every_players_season(league, alice, bob):
    lg, b = league
    wa, wb = both_ready(alice, bob, lg)
    recv(wa, "init"); recv(wb, "init")
    room = league_race._rooms[lg["id"]]
    with room.lock:
        while not room.engine.finished:
            room.engine.step(5.0)
        room.save_result()
        room._take_snapshot()
    while not recv(wa, "state")["finished"]:
        pass
    with Session(engine) as s:
        ua, ub = repo.user_by_email(s, "alice@example.com"), repo.user_by_email(s, "bob@example.com")
        ra = [(r.driver, r.position) for r in repo.season_results(s, ua, lg["your_season_id"])]
        rb = [(r.driver, r.position) for r in repo.season_results(s, ub, b["your_season_id"])]
        assert len(ra) == 20 and ra == rb                     # one race, the same result for both
        assert repo.get_season(s, ua, lg["your_season_id"]).rounds_done == 1
        assert repo.get_season(s, ub, b["your_season_id"]).rounds_done == 1
    assert len(alice.get("/api/races").json()) == 1 and len(bob.get("/api/races").json()) == 1
    table = {r["nickname"]: r for r in alice.get(f"/api/leagues/{lg['id']}").json()["table"]}
    assert table["Ali"]["rounds_done"] == table["Bobby"]["rounds_done"] == 1
    close(wa, wb)
    # back in the lobby, it's round 2
    wa = open_lobby(alice, lg["id"])
    assert recv(wa, "lobby")["round"] == 2
    close(wa)


def test_leaving_the_lobby_unreadies_and_everyone_leaving_abandons_the_race(league, alice, bob):
    lg, _ = league
    wa, wb = open_lobby(alice, lg["id"]), open_lobby(bob, lg["id"])
    recv(wa, "lobby"); recv(wb, "lobby")
    wb.send_text(json.dumps({"type": "ready", "ready": True}))
    recv(wb, "lobby")
    close(wb)
    room = league_race._rooms[lg["id"]]
    assert room.ready == {} and room.engine is None
    wb = open_lobby(bob, lg["id"])
    recv(wb, "lobby")
    wa.send_text(json.dumps({"type": "ready", "ready": True}))
    wb.send_text(json.dumps({"type": "ready", "ready": True}))
    recv(wa, "init")
    close(wa, wb)
    assert room.engine is None and lg["id"] not in league_race._rooms     # abandoned, nothing saved
    with Session(engine) as s:
        assert s.get(Season, lg["your_season_id"]).rounds_done == 0


def test_a_removed_member_is_dropped_from_the_lobby(league, alice, bob):
    lg, _ = league
    wb = open_lobby(bob, lg["id"])
    recv(wb, "lobby")
    bob.post(f"/api/leagues/{lg['id']}/leave", headers=ORIGIN)
    league_race._rooms[lg["id"]].version += 1             # any lobby change re-checks membership
    assert recv_error(wb) == "League not found"
    wb.send_text(json.dumps({"type": "ready", "ready": True}))
    assert recv_error(wb) == "Open a league lobby first"
    close(wb)
