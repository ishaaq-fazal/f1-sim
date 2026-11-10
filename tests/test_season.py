"""Season mode: schedule, F1 points, standings, and that seasons are private to their owner."""
import json
from types import SimpleNamespace as Row

import pytest
from sqlmodel import Session, select

from server import repo
from server.db import Season, SeasonResult, engine
from server.main import RaceSession
from sim import RaceConfig, RaceEngine
from sim.season import POINTS, ROUNDS, SCHEDULE, points_for, standings, track_for_round
from tests.conftest import ORIGIN, PASSWORD


# ---------------------------------------------------------------- rules
def test_eight_rounds_two_at_each_circuit():
    assert ROUNDS == 8
    assert all(SCHEDULE.count(t) == 2 for t in set(SCHEDULE)) and len(set(SCHEDULE)) == 4
    assert track_for_round(1) == track_for_round(5)
    with pytest.raises(ValueError):
        track_for_round(9)


def test_f1_points_for_the_top_ten_only():
    assert POINTS == (25, 18, 15, 12, 10, 8, 6, 4, 2, 1)
    assert [points_for(p) for p in (1, 2, 3, 10, 11, 20)] == [25, 18, 15, 1, 0, 0]
    assert points_for(1, retired=True) == 0


def test_standings_add_points_and_break_ties_on_countback():
    rows = [Row(driver="AAA", team="x", position=1, retired=False, points=25),
            Row(driver="BBB", team="y", position=2, retired=False, points=18),
            Row(driver="BBB", team="y", position=1, retired=False, points=25),
            Row(driver="AAA", team="x", position=2, retired=False, points=18),
            Row(driver="CCC", team="x", position=3, retired=False, points=15),
            Row(driver="CCC", team="x", position=15, retired=True, points=0)]
    drivers, teams = standings(rows)
    assert [(d.key, d.points) for d in drivers] == [("AAA", 43), ("BBB", 43), ("CCC", 15)]
    assert drivers[0].wins == 1 and drivers[2].podiums == 1
    assert [(t.key, t.points) for t in teams] == [("x", 58), ("y", 43)]


# ---------------------------------------------------------------- API
def new_season(c, team="aurora", laps=5, weather="dry"):
    r = c.post("/api/seasons", json={"team": team, "laps": laps, "weather": weather}, headers=ORIGIN)
    assert r.status_code == 201, r.text
    return r.json()


def finish_round(email: str, season_id: int):
    """Run the season's next round to the flag on the server side (fast) and save it."""
    with Session(engine) as s:
        user = repo.user_by_email(s, email)
        season = repo.get_season(s, user, season_id)
        s.expunge(user)
        round_no = season.rounds_done + 1
    sess = RaceSession(ws=None, user=user)
    sess.team, sess.seed, sess.season_round = season.team, round_no, (season_id, round_no)
    sess.engine = RaceEngine(RaceConfig(laps=5, seed=round_no, controlled_team=season.team,
                                        track_id=track_for_round(round_no)))
    while not sess.engine.finished:
        sess.engine.step(5.0)
    sess._save_result()


def test_create_season_and_empty_standings(alice):
    s = new_season(alice)
    assert s["rounds_done"] == 0 and not s["finished"] and set(s) == {
        "id", "team", "laps", "weather", "rounds_done", "finished", "created_at"}   # no owner id leaks
    d = alice.get(f"/api/seasons/{s['id']}").json()
    assert [r["track"] for r in d["rounds"]] == list(SCHEDULE)
    assert d["drivers"] == [] and d["points_system"][0] == 25


def test_season_rejects_bad_input(alice):
    for body in ({"team": "nobody", "laps": 5}, {"team": "aurora", "laps": 500},
                 {"team": "aurora", "laps": 5, "weather": "snow"}, {"team": "aurora", "laps": 5, "owner_id": 1}):
        assert alice.post("/api/seasons", json=body, headers=ORIGIN).status_code in (400, 422)


def test_full_season_scores_f1_points_and_finishes(alice):
    sid = new_season(alice)["id"]
    for _ in range(ROUNDS):
        finish_round("alice@example.com", sid)
    d = alice.get(f"/api/seasons/{sid}").json()
    assert d["season"]["finished"] and d["season"]["rounds_done"] == 8
    assert all(r["done"] and r["winner"] for r in d["rounds"])
    total = sum(x["points"] for x in d["drivers"])
    with Session(engine) as s:
        rows = s.exec(select(SeasonResult).where(SeasonResult.season_id == sid)).all()
    assert len(rows) == ROUNDS * 20 and total <= ROUNDS * sum(POINTS)
    assert total == sum(points_for(r.position, r.retired) for r in rows)
    assert sum(t["points"] for t in d["teams"]) == total
    assert d["drivers"][0]["points"] >= d["drivers"][-1]["points"]
    # a finished season can't take a ninth round
    with Session(engine) as s:
        user = repo.user_by_email(s, "alice@example.com")
        with pytest.raises(repo.SeasonError):
            repo.record_round(s, user, sid, 9, "harbourline", 1, [])


def test_rounds_must_be_recorded_in_order_on_the_right_circuit(alice):
    sid = new_season(alice)["id"]
    with Session(engine) as s:
        user = repo.user_by_email(s, "alice@example.com")
        with pytest.raises(repo.SeasonError):
            repo.record_round(s, user, sid, 2, track_for_round(2), 1, [])       # skipping round 1
        with pytest.raises(repo.SeasonError):
            repo.record_round(s, user, sid, 1, "sandridge", 1, [])            # wrong circuit
        repo.record_round(s, user, sid, 1, track_for_round(1), 1, [("MAR", "aurora", 1, False)])
        with pytest.raises(repo.SeasonError):
            repo.record_round(s, user, sid, 1, track_for_round(1), 1, [])       # same round twice


# ---------------------------------------------------------------- authorisation
def test_users_only_see_their_own_seasons(alice, bob):
    sid = new_season(alice)["id"]
    finish_round("alice@example.com", sid)
    assert bob.get("/api/seasons").json() == []
    assert bob.get(f"/api/seasons/{sid}").status_code == 404
    assert bob.get(f"/api/seasons/{sid + 999}").status_code == 404     # same answer as a missing one
    assert len(alice.get("/api/seasons").json()) == 1


def test_users_cannot_delete_or_record_into_others_seasons(alice, bob):
    sid = new_season(alice)["id"]
    assert bob.delete(f"/api/seasons/{sid}", headers=ORIGIN).status_code == 404
    with Session(engine) as s:
        b = repo.user_by_email(s, "bob@example.com")
        with pytest.raises(repo.SeasonError):
            repo.record_round(s, b, sid, 1, track_for_round(1), 1, [("MAR", "aurora", 1, False)])
        assert repo.season_results(s, b, sid) == []
    assert alice.get(f"/api/seasons/{sid}").status_code == 200
    assert alice.delete(f"/api/seasons/{sid}", headers=ORIGIN).status_code == 204
    assert alice.get(f"/api/seasons/{sid}").status_code == 404
    with Session(engine) as s:
        assert s.exec(select(SeasonResult).where(SeasonResult.season_id == sid)).all() == []


def test_viewers_cannot_start_a_season(admin, bob):
    bob_id = bob.get("/api/me").json()["id"]
    admin.patch(f"/api/admin/users/{bob_id}/role", json={"role": "viewer"}, headers=ORIGIN)
    bob.post("/api/auth/login", json={"email": "bob@example.com", "password": PASSWORD})
    assert bob.post("/api/seasons", json={"team": "aurora", "laps": 5}, headers=ORIGIN).status_code == 403


def test_season_endpoints_need_sign_in_and_same_origin(alice):
    from fastapi.testclient import TestClient
    from server.main import app
    anon = TestClient(app)
    assert anon.get("/api/seasons").status_code == 401
    assert anon.post("/api/seasons", json={"team": "aurora", "laps": 5}, headers=ORIGIN).status_code == 401
    r = alice.post("/api/seasons", json={"team": "aurora", "laps": 5}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403


@pytest.mark.parametrize("path", ["/api/seasons/1%20OR%201=1", "/api/seasons/1;DROP%20TABLE%20season"])
def test_sql_injection_in_season_path_rejected(alice, path):
    assert alice.get(path).status_code == 422
    with Session(engine) as s:
        s.exec(select(Season)).all()          # table still there


# ---------------------------------------------------------------- over the WebSocket
def test_ws_season_round_uses_the_seasons_team_and_circuit(alice):
    sid = new_season(alice, team="vantage", laps=7)["id"]
    finish_round("alice@example.com", sid)            # round 1 done, round 2 is next
    with alice.websocket_connect("/ws/race", headers=ORIGIN) as ws:
        # team, track and laps in the message are ignored: the season decides
        ws.send_text(json.dumps({"type": "start", "team": "aurora", "laps": 5, "track": "harbourline",
                                 "season": sid}))
        init = ws.receive_json()
        assert init["type"] == "init" and init["team"] == "vantage" and init["laps"] == 7
        assert init["track"]["id"] == track_for_round(2)
        assert init["season"] == {"id": sid, "round": 2, "rounds": 8}


def test_ws_refuses_another_users_season(alice, bob):
    sid = new_season(alice)["id"]
    with bob.websocket_connect("/ws/race", headers=ORIGIN) as ws:
        ws.send_text(json.dumps({"type": "start", "team": "aurora", "laps": 5, "season": sid}))
        m = ws.receive_json()
        assert m == {"type": "error", "detail": "Season not found"}


def test_ws_refuses_a_finished_season(alice):
    sid = new_season(alice)["id"]
    for _ in range(ROUNDS):
        finish_round("alice@example.com", sid)
    with alice.websocket_connect("/ws/race", headers=ORIGIN) as ws:
        ws.send_text(json.dumps({"type": "start", "team": "aurora", "laps": 5, "season": sid}))
        assert ws.receive_json()["detail"] == "That season is over"
