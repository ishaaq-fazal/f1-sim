"""Sprint weekends: which rounds have one, sprint points (8-7-6-5-4-3-2-1), the order a weekend is
raced in, and that sprint results stay private to their owner like every other season result."""
import json
from types import SimpleNamespace as Row

import pytest
from sqlmodel import Session, select

from server import league_race, repo
from server.db import Season, SprintResult, engine
from sim import RaceConfig, RaceEngine
from sim import career as cr
from sim.season import (ROUNDS, SCHEDULE, SPRINT_POINTS, SPRINT_TRACKS, is_sprint_round, points_for,
                        round_forecast, sprint_laps, sprint_rounds, sprint_seed, standings, track_for_round)
from sim.track import TRACK_SPECS
from tests.conftest import ORIGIN
from tests.test_career import new_career, race_round
from tests.test_league_race import both_ready, close, recv
from tests.test_leagues import create, join
from tests.test_season import finish_round, finish_session, new_season

FIRST = sprint_rounds()[0]          # round 3


def results(n=20):
    from sim.grid import default_grid
    grid = default_grid()
    return [(d.code, d.team, i, False) for i, d in enumerate(grid[:n], 1)]


def to_round(email, sid, round_no):
    """Finish whole rounds until the season's next round is round_no."""
    for _ in range(round_no - 1):
        finish_round(email, sid)


# ---------------------------------------------------------------- rules
def test_three_sprint_weekends_on_original_circuits_spread_through_the_year():
    assert sprint_rounds() == [3, 6, 10, 14, 18, 21]
    assert set(SPRINT_TRACKS) <= set(TRACK_SPECS) and len(SPRINT_TRACKS) == 6
    assert [SCHEDULE[n - 1] for n in sprint_rounds()] == list(SPRINT_TRACKS)
    assert not is_sprint_round(1) and not is_sprint_round(0) and not is_sprint_round(ROUNDS + 1)


def test_sprint_points_are_eight_down_to_one_for_the_top_eight():
    assert SPRINT_POINTS == (8, 7, 6, 5, 4, 3, 2, 1)
    assert [points_for(p, sprint=True) for p in range(1, 11)] == [8, 7, 6, 5, 4, 3, 2, 1, 0, 0]
    assert points_for(1, retired=True, sprint=True) == 0
    assert points_for(1) == 25                      # the Grand Prix table is unchanged


def test_a_sprint_is_about_a_third_of_the_race():
    assert sprint_laps(30) == 10 and sprint_laps(57) == 19
    assert sprint_laps(5) == 4                      # never shorter than 4 laps


def test_sprint_points_count_but_wins_and_countback_use_grand_prix_results_only():
    rows = [Row(driver="AAA", team="x", position=1, retired=False, points=25),
            Row(driver="BBB", team="y", position=2, retired=False, points=18),
            Row(driver="BBB", team="y", position=1, retired=False, points=8, sprint=True),
            Row(driver="AAA", team="x", position=8, retired=False, points=1, sprint=True)]
    drivers, teams = standings(rows)
    assert [(d.key, d.points) for d in drivers] == [("AAA", 26), ("BBB", 26)]   # tie broken on GP wins
    assert drivers[0].wins == 1 and drivers[1].wins == 0 and drivers[1].podiums == 1


def test_sprint_seed_and_forecast_are_fixed_but_differ_from_the_grand_prix():
    assert sprint_seed(1234) == sprint_seed(1234) != 1234
    track = track_for_round(FIRST)
    assert round_forecast(sprint_seed(99), track, 10) == round_forecast(sprint_seed(99), track, 10)


# ---------------------------------------------------------------- engine
def test_sprint_has_no_two_compound_rule_and_most_cars_dont_stop():
    stops = penalties = 0
    for seed in range(6):
        e = RaceEngine(RaceConfig(laps=10, seed=seed, track_id="riverside", sprint=True))
        while not e.finished:
            e.step(5.0)
        stops += sum(c.pits for c in e.cars)
        penalties += sum(1 for c in e.cars if c.penalty_s)
        assert any("Sprint" in ev["text"] for ev in e.events if ev["kind"] == "start")
    assert penalties == 0
    assert stops < 6 * 20 * 0.25                    # a sprint is mostly flat out on one set


def test_grand_prix_still_enforces_two_compounds():
    e = RaceEngine(RaceConfig(laps=10, seed=1, track_id="riverside"))
    assert e._needs_two({"M"}) and not e._needs_two({"M", "H"})
    assert not RaceEngine(RaceConfig(laps=10, seed=1, sprint=True))._needs_two({"M"})


# ---------------------------------------------------------------- the weekend's order
def test_a_sprint_weekend_races_the_sprint_then_the_grand_prix(alice):
    sid = new_season(alice)["id"]
    to_round("alice@example.com", sid, FIRST)
    n = alice.get(f"/api/seasons/{sid}/next").json()
    assert n["round"] == FIRST and n["session"] == "sprint" and n["sprint_weekend"]
    assert n["laps"] == sprint_laps(5)
    assert alice.get(f"/api/seasons/{sid}").json()["next_session"] == "sprint"

    assert finish_session("alice@example.com", sid) == "sprint"
    d = alice.get(f"/api/seasons/{sid}").json()
    r3 = d["rounds"][FIRST - 1]
    assert d["season"]["rounds_done"] == FIRST - 1          # the round isn't over until the Grand Prix
    assert r3["sprint"] and r3["sprint_done"] and r3["sprint_winner"] and not r3["done"]
    assert d["next_session"] == "race" and d["sprint_points_system"] == list(SPRINT_POINTS)
    n = alice.get(f"/api/seasons/{sid}/next").json()
    assert n["session"] == "race" and n["laps"] == 5 and n["sprint_weekend"]

    assert finish_session("alice@example.com", sid) == "race"
    d = alice.get(f"/api/seasons/{sid}").json()
    assert d["season"]["rounds_done"] == FIRST and d["rounds"][FIRST - 1]["done"]
    assert alice.get(f"/api/seasons/{sid}/next").json()["session"] == "race"       # round 4: no sprint
    # the drivers' table includes the sprint points
    with Session(engine) as s:
        sprints = s.exec(select(SprintResult).where(SprintResult.season_id == sid)).all()
    assert len(sprints) == 20
    assert sum(x["points"] for x in d["drivers"]) == sum(r.points for r in sprints) + _gp_points(sid)
    assert sum(r.points for r in sprints) == sum(SPRINT_POINTS) - sum(
        points_for(r.position, sprint=True) for r in sprints if r.retired)


def _gp_points(sid):
    from server.db import SeasonResult
    with Session(engine) as s:
        return sum(r.points for r in s.exec(select(SeasonResult).where(SeasonResult.season_id == sid)))


def test_grand_prix_of_a_sprint_weekend_cant_be_saved_before_its_sprint(alice):
    sid = new_season(alice)["id"]
    to_round("alice@example.com", sid, FIRST)
    with Session(engine) as s:
        user = repo.user_by_email(s, "alice@example.com")
        track = track_for_round(FIRST)
        with pytest.raises(repo.SeasonError, match="sprint first"):
            repo.record_round(s, user, sid, FIRST, track, 1, results())
        with pytest.raises(repo.SeasonError):
            repo.record_sprint(s, user, sid, FIRST + 1, track_for_round(FIRST + 1), 1, results())   # wrong round
        with pytest.raises(repo.SeasonError):
            repo.record_sprint(s, user, sid, FIRST, "harbourline", 1, results())                  # wrong circuit
        repo.record_sprint(s, user, sid, FIRST, track, 1, results())
        with pytest.raises(repo.SeasonError):
            repo.record_sprint(s, user, sid, FIRST, track, 1, results())                          # twice
        repo.record_round(s, user, sid, FIRST, track, 1, results())
        assert repo.get_season(s, user, sid).rounds_done == FIRST


def test_no_sprint_on_a_normal_weekend(alice):
    sid = new_season(alice)["id"]
    with Session(engine) as s:
        user = repo.user_by_email(s, "alice@example.com")
        with pytest.raises(repo.SeasonError):
            repo.record_sprint(s, user, sid, 1, track_for_round(1), 1, results())


def test_ws_season_start_on_a_sprint_weekend_runs_the_sprint(alice):
    sid = new_season(alice, team="vantage", laps=12)["id"]
    to_round("alice@example.com", sid, FIRST)
    with alice.websocket_connect("/ws/race", headers=ORIGIN) as ws:
        ws.send_text(json.dumps({"type": "start", "team": "aurora", "laps": 5, "season": sid}))
        init = ws.receive_json()
        assert init["type"] == "init" and init["laps"] == sprint_laps(12) == 4
        assert init["season"] == {"id": sid, "round": FIRST, "rounds": ROUNDS, "session": "sprint"}
        assert init["track"]["id"] == track_for_round(FIRST)
        with Session(engine) as s:
            seed = repo.season_round_seed(s.get(Season, sid), FIRST)
        assert init["seed"] == sprint_seed(seed)     # fixed in advance, like the forecast


# ---------------------------------------------------------------- privacy
def test_sprint_results_are_private_to_their_owner(alice, bob):
    sid = new_season(alice)["id"]
    to_round("alice@example.com", sid, FIRST)
    finish_session("alice@example.com", sid)
    with Session(engine) as s:
        b = repo.user_by_email(s, "bob@example.com")
        assert repo.sprint_results(s, b, sid) == []
        assert repo.all_results(s, b, sid) == []
        season = s.get(Season, sid)
        assert repo.next_session(s, b, season) is None
        with pytest.raises(repo.SeasonError):
            repo.record_sprint(s, b, sid, FIRST, track_for_round(FIRST), 1, results())
        with pytest.raises(repo.SeasonError):
            repo.record_round(s, b, sid, FIRST, track_for_round(FIRST), 1, results())
    assert bob.get(f"/api/seasons/{sid}").status_code == 404
    assert bob.get(f"/api/seasons/{sid}/next").status_code == 404


def test_deleting_a_season_deletes_its_sprint_results(alice):
    sid = new_season(alice)["id"]
    to_round("alice@example.com", sid, FIRST)
    finish_session("alice@example.com", sid)
    assert alice.delete(f"/api/seasons/{sid}", headers=ORIGIN).status_code == 204
    with Session(engine) as s:
        assert s.exec(select(SprintResult).where(SprintResult.season_id == sid)).all() == []


# ---------------------------------------------------------------- career
def test_career_sprint_points_pay_prize_money_and_count_for_the_board(alice):
    c = new_career(alice)
    for _ in range(FIRST - 1):
        race_round("alice@example.com", c["id"])
    before = alice.get(f"/api/careers/{c['id']}").json()
    assert before["next_session"] == "sprint"
    with Session(engine) as s:
        user = repo.user_by_email(s, "alice@example.com")
        career = repo.get_career(s, user, c["id"])
        season = repo.career_season(s, user, career)
        rows = [(d.code, d.team, 0, False) for d in cr.race_grid(career.state)]
        mine = [r for r in rows if r[1] == career.team]
        others = [r for r in rows if r[1] != career.team]
        order = mine + others                                  # your two cars finish 1-2
        repo.record_sprint(s, user, season.id, FIRST, track_for_round(FIRST), 1,
                           [(code, team, i, False) for i, (code, team, _, _) in enumerate(order, 1)])
    after = alice.get(f"/api/careers/{c['id']}").json()
    assert after["budget"] == pytest.approx(before["budget"] + cr.ROUND_PRIZE_PER_POINT * 15 * cr.ROUND_SHARE, abs=0.1)
    assert after["dev_points"] == before["dev_points"]          # no development points for a sprint
    assert after["next_session"] == "race"
    table = alice.get(f"/api/seasons/{c['season_id']}").json()["teams"]
    assert any(t["team"] == c["team"] and t["points"] >= 15 for t in table)


def test_credit_sprint_pays_only_for_points():
    st = {"budget": 10.0, "income": 0.0, "dev_points": 3}
    assert cr.credit_sprint(st, 0) == 0
    cr.credit_sprint(st, 10)
    assert st["budget"] == pytest.approx(10 + cr.ROUND_PRIZE_PER_POINT * 10 * cr.ROUND_SHARE, abs=0.05) and st["dev_points"] == 3


# ---------------------------------------------------------------- leagues
@pytest.fixture(autouse=True)
def no_rooms():
    league_race._rooms.clear()
    yield
    league_race._rooms.clear()


def _league_to_sprint(alice, bob):
    lg = create(alice)
    b = join(bob, lg["code"]).json()
    with Session(engine) as s:
        users = {"alice@example.com": lg["your_season_id"], "bob@example.com": b["your_season_id"]}
        grid = results()
        for n in range(1, FIRST):
            for email, sid in users.items():
                repo.record_round(s, repo.user_by_email(s, email), sid, n, track_for_round(n), n, grid)
    return lg, b


def test_league_shared_sprint_goes_into_every_players_season_and_the_table(alice, bob):
    lg, b = _league_to_sprint(alice, bob)
    wa, wb = both_ready(alice, bob, lg)
    ia, ib = recv(wa, "init"), recv(wb, "init")
    assert ia["season"]["session"] == ib["season"]["session"] == "sprint"
    assert ia["laps"] == sprint_laps(lg["laps"]) and ia["seed"] == ib["seed"]
    room = league_race._rooms[lg["id"]]
    assert room.engine.cfg.sprint
    with room.lock:
        while not room.engine.finished:
            room.engine.step(5.0)
        room.save_result()
        room._take_snapshot()
    while not recv(wa, "state")["finished"]:
        pass
    before = {r["nickname"]: r["points"] for r in alice.get(f"/api/leagues/{lg['id']}").json()["table"]}
    with Session(engine) as s:
        ua, ub = repo.user_by_email(s, "alice@example.com"), repo.user_by_email(s, "bob@example.com")
        ra = [(r.driver, r.position) for r in repo.sprint_results(s, ua, lg["your_season_id"])]
        rb = [(r.driver, r.position) for r in repo.sprint_results(s, ub, b["your_season_id"])]
        assert len(ra) == 20 and ra == rb
        assert repo.get_season(s, ua, lg["your_season_id"]).rounds_done == FIRST - 1
        sprint_pts = sum(r.points for r in repo.sprint_results(s, ua, lg["your_season_id"]) if r.team == "aurora")
        gp_pts = sum(r.points for r in repo.season_results(s, ua, lg["your_season_id"]) if r.team == "aurora")
    assert before["Ali"] == sprint_pts + gp_pts
    close(wa, wb)
    # back in the lobby, it's the same round's Grand Prix
    from tests.test_league_race import open_lobby
    wa = open_lobby(alice, lg["id"])
    m = recv(wa, "lobby")
    assert m["round"] == FIRST and m["session"] == "race" and m["race_laps"] == lg["laps"]
    close(wa)


def test_league_lobby_waits_when_players_are_on_different_sessions(alice, bob):
    lg, b = _league_to_sprint(alice, bob)
    with Session(engine) as s:          # only Ali has the sprint saved
        ua = repo.user_by_email(s, "alice@example.com")
        repo.record_sprint(s, ua, lg["your_season_id"], FIRST, track_for_round(FIRST), 1, results())
    from tests.test_league_race import open_lobby
    wa = open_lobby(alice, lg["id"])
    m = recv(wa, "lobby")
    assert m["blocker"] == "Players are on different rounds"
    close(wa)
