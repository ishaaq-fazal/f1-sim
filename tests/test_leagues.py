"""Online leagues: friends each run their own season and compare points on a shared table.
Most of these tests are about who can see and change what."""
import json

import pytest
from sqlmodel import Session, select

from server import repo
from server.db import League, LeagueMember, Season, engine
from sim.season import ROUNDS, SCHEDULE
from tests.conftest import ORIGIN, PASSWORD, make_client
from tests.test_season import finish_round


@pytest.fixture()
def carol(admin):
    return make_client("carol@example.com")


def create(c, name="Friday Club", laps=5, team="aurora", nickname="Ali"):
    r = c.post("/api/leagues", json={"name": name, "laps": laps, "team": team, "nickname": nickname},
               headers=ORIGIN)
    assert r.status_code == 201, r.text
    return r.json()


def join(c, code, team="vantage", nickname="Bobby"):
    return c.post("/api/leagues/join", json={"code": code, "team": team, "nickname": nickname}, headers=ORIGIN)


# ---------------------------------------------------------------- basics
def test_create_league_makes_a_season_and_an_invite_code(alice):
    lg = create(alice)
    assert len(lg["code"]) == 8 and lg["is_owner"] and lg["members"] == 1 and lg["your_nickname"] == "Ali"
    season = alice.get(f"/api/seasons/{lg['your_season_id']}").json()["season"]
    assert season["team"] == "aurora" and season["laps"] == 5 and season["rounds_done"] == 0
    assert [x["id"] for x in alice.get("/api/leagues").json()] == [lg["id"]]


def test_friends_join_and_the_table_compares_their_points(alice, bob):
    lg = create(alice)
    r = join(bob, lg["code"].lower())                       # codes aren't case sensitive
    assert r.status_code == 201, r.text
    bob_lg = r.json()
    assert bob_lg["id"] == lg["id"] and not bob_lg["is_owner"] and bob_lg["members"] == 2
    assert bob.get(f"/api/seasons/{bob_lg['your_season_id']}").json()["season"]["laps"] == 5   # league's laps

    finish_round("alice@example.com", lg["your_season_id"])
    finish_round("bob@example.com", bob_lg["your_season_id"])
    finish_round("bob@example.com", bob_lg["your_season_id"])

    d = alice.get(f"/api/leagues/{lg['id']}").json()
    assert d["rounds"] == ROUNDS and d["schedule"] == list(SCHEDULE)
    rows = {r["nickname"]: r for r in d["table"]}
    assert rows["Ali"]["you"] and rows["Ali"]["owner"] and not rows["Bobby"]["you"]
    assert rows["Ali"]["rounds_done"] == 1 and rows["Bobby"]["rounds_done"] == 2
    assert rows["Bobby"]["team"] == "vantage" and rows["Bobby"]["team_name"]
    # points are each player's own team's points from their own season
    for email, row, sid in (("alice@example.com", rows["Ali"], lg["your_season_id"]),
                            ("bob@example.com", rows["Bobby"], bob_lg["your_season_id"])):
        with Session(engine) as s:
            u = repo.user_by_email(s, email)
            mine = [x for x in repo.season_results(s, u, sid) if x.team == row["team"]]
        assert row["points"] == sum(x.points for x in mine)
    assert [r["pos"] for r in d["table"]] == [1, 2]
    assert d["table"][0]["points"] >= d["table"][1]["points"]
    # both see the same table
    assert [(r["nickname"], r["points"]) for r in bob.get(f"/api/leagues/{lg['id']}").json()["table"]] == \
           [(r["nickname"], r["points"]) for r in d["table"]]


def test_league_rounds_share_one_seed_so_everyone_races_the_same_conditions(alice, bob, carol):
    lg = create(alice)
    b = join(bob, lg["code"]).json()
    other = create(carol, name="Other League", nickname="Caz")
    with Session(engine) as s:
        sa, sb, sc = (s.get(Season, x) for x in (lg["your_season_id"], b["your_season_id"], other["your_season_id"]))
        solo = Season(owner_id=sa.owner_id, team="aurora", laps=5)
        s.add(solo)
        s.flush()
        assert repo.season_round_seed(sa, 1) == repo.season_round_seed(sb, 1)
        assert repo.season_round_seed(sa, 1) != repo.season_round_seed(sa, 2)
        assert repo.season_round_seed(sa, 1) != repo.season_round_seed(sc, 1)
        assert repo.season_round_seed(solo, 1) == repo.round_seed(solo.id, 1)
        s.rollback()
    fa = alice.get(f"/api/seasons/{lg['your_season_id']}/next").json()["forecast"]
    fb = bob.get(f"/api/seasons/{b['your_season_id']}/next").json()["forecast"]
    assert fa == fb


def test_join_rules(alice, bob, carol):
    lg = create(alice)
    assert join(bob, "ZZZZZZZZ").status_code == 400                       # no such code
    assert join(bob, lg["code"], nickname="ali").status_code == 400        # name taken (any case)
    assert join(bob, lg["code"]).status_code == 201
    assert join(bob, lg["code"], nickname="Again").status_code == 400      # already in
    assert join(alice, lg["code"], nickname="Twice").status_code == 400
    assert join(carol, lg["code"], team="nobody", nickname="Caz").status_code == 400
    r = join(carol, lg["code"], team="vantage", nickname="Caz")             # bob already runs Vantage
    assert r.status_code == 400 and "team is taken" in r.json()["detail"] and "Vantage" not in r.json()["detail"]
    with Session(engine) as s:
        assert len(s.exec(select(LeagueMember)).all()) == 2
        assert len(s.exec(select(Season)).all()) == 2                     # failed joins left no seasons


def test_no_new_players_once_a_league_has_raced(alice, bob):
    lg = create(alice)
    finish_round("alice@example.com", lg["your_season_id"])
    r = join(bob, lg["code"])
    assert r.status_code == 400 and "already started" in r.json()["detail"]


@pytest.mark.parametrize("body", [
    {"name": "x", "laps": 5, "team": "aurora", "nickname": "Ali"},                 # name too short
    {"name": "<script>", "laps": 5, "team": "aurora", "nickname": "Ali"},
    {"name": "Club", "laps": 500, "team": "aurora", "nickname": "Ali"},
    {"name": "Club", "laps": 5, "team": "aurora", "nickname": "a@b.com"},
    {"name": "Club", "laps": 5, "team": "aurora", "nickname": "Ali", "owner_id": 1},  # extra field
    {"name": "Club'; DROP TABLE league;--", "laps": 5, "team": "aurora", "nickname": "Ali"},
])
def test_create_rejects_bad_input(alice, body):
    assert alice.post("/api/leagues", json=body, headers=ORIGIN).status_code in (400, 422)


def test_join_attempts_are_rate_limited(alice, bob):
    from server import routes
    routes._league_join.hits.clear()
    codes = [join(bob, f"AAAAAA{i:02d}").status_code for i in range(12)]
    assert codes[:10] == [400] * 10 and codes[-1] == 429
    routes._league_join.hits.clear()


# ---------------------------------------------------------------- authorisation
def test_non_members_cannot_see_a_league(alice, bob):
    lg = create(alice)
    finish_round("alice@example.com", lg["your_season_id"])
    assert bob.get("/api/leagues").json() == []
    assert bob.get(f"/api/leagues/{lg['id']}").status_code == 404
    assert bob.get(f"/api/leagues/{lg['id'] + 999}").status_code == 404     # same answer as missing
    # joining a league doesn't open up members' private seasons or race history
    join(bob, lg["code"])
    assert bob.get(f"/api/seasons/{lg['your_season_id']}").status_code == 404
    assert bob.get(f"/api/seasons/{lg['your_season_id']}/next").status_code == 404
    assert bob.get("/api/races").json() == []


def test_league_table_never_shows_emails_or_user_ids(alice, bob):
    lg = create(alice)
    join(bob, lg["code"])
    body = bob.get(f"/api/leagues/{lg['id']}").text + bob.get("/api/leagues").text
    assert "alice@example.com" not in body and "owner_id" not in body and "user_id" not in body
    # only the owner gets member ids (to remove people), and never their own
    assert all(r["member_id"] is None for r in bob.get(f"/api/leagues/{lg['id']}").json()["table"])
    rows = alice.get(f"/api/leagues/{lg['id']}").json()["table"]
    assert [r["member_id"] is not None for r in sorted(rows, key=lambda r: r["nickname"])] == [False, True]


def test_only_the_owner_removes_members_or_deletes(alice, bob, carol):
    lg = create(alice)
    join(bob, lg["code"])
    join(carol, lg["code"], team="halcyon", nickname="Caz")
    with Session(engine) as s:
        ids = {m.nickname: m.id for m in s.exec(select(LeagueMember)).all()}
    # bob (a member, not the owner) can't remove carol or delete the league
    assert bob.delete(f"/api/leagues/{lg['id']}/members/{ids['Caz']}", headers=ORIGIN).status_code == 404
    assert bob.delete(f"/api/leagues/{lg['id']}", headers=ORIGIN).status_code == 404
    # a member id from one league can't be used through another league the attacker owns
    other = create(bob, name="Bob League", nickname="Bo")
    assert bob.delete(f"/api/leagues/{other['id']}/members/{ids['Caz']}", headers=ORIGIN).status_code == 404
    assert len(alice.get(f"/api/leagues/{lg['id']}").json()["table"]) == 3
    # the owner can
    assert alice.delete(f"/api/leagues/{lg['id']}/members/{ids['Caz']}", headers=ORIGIN).status_code == 204
    assert carol.get(f"/api/leagues/{lg['id']}").status_code == 404
    assert len(carol.get("/api/seasons").json()) == 1              # carol keeps her season
    assert alice.delete(f"/api/leagues/{lg['id']}", headers=ORIGIN).status_code == 204
    assert bob.get(f"/api/leagues/{lg['id']}").status_code == 404
    with Session(engine) as s:
        assert s.get(League, lg["id"]) is None
        assert all(x.league_id != lg["id"] for x in s.exec(select(Season)).all())


def test_leaving_hands_ownership_on_and_last_one_out_deletes(alice, bob):
    lg = create(alice)
    join(bob, lg["code"])
    assert alice.post(f"/api/leagues/{lg['id']}/leave", headers=ORIGIN).status_code == 204
    assert alice.get(f"/api/leagues/{lg['id']}").status_code == 404
    assert bob.get(f"/api/leagues/{lg['id']}").json()["league"]["is_owner"]
    assert alice.post(f"/api/leagues/{lg['id']}/leave", headers=ORIGIN).status_code == 404
    assert bob.post(f"/api/leagues/{lg['id']}/leave", headers=ORIGIN).status_code == 204
    with Session(engine) as s:
        assert s.get(League, lg["id"]) is None


def test_deleting_your_league_season_leaves_the_league(alice, bob):
    lg = create(alice)
    b = join(bob, lg["code"]).json()
    assert bob.delete(f"/api/seasons/{b['your_season_id']}", headers=ORIGIN).status_code == 204
    assert bob.get(f"/api/leagues/{lg['id']}").status_code == 404
    assert [r["nickname"] for r in alice.get(f"/api/leagues/{lg['id']}").json()["table"]] == ["Ali"]


def test_viewers_cannot_create_or_join(admin, alice, bob):
    lg = create(alice)
    bob_id = bob.get("/api/me").json()["id"]
    admin.patch(f"/api/admin/users/{bob_id}/role", json={"role": "viewer"}, headers=ORIGIN)
    bob.post("/api/auth/login", json={"email": "bob@example.com", "password": PASSWORD})
    assert join(bob, lg["code"]).status_code == 403
    assert bob.post("/api/leagues", json={"name": "Club", "laps": 5, "team": "aurora", "nickname": "Bo"},
                    headers=ORIGIN).status_code == 403


def test_league_endpoints_need_sign_in_and_same_origin(alice):
    from fastapi.testclient import TestClient
    from server.main import app
    lg = create(alice)
    anon = TestClient(app)
    assert anon.get("/api/leagues").status_code == 401
    assert anon.get(f"/api/leagues/{lg['id']}").status_code == 401
    assert anon.post("/api/leagues/join", json={"code": lg["code"], "team": "aurora", "nickname": "Anon"},
                     headers=ORIGIN).status_code == 401
    r = alice.post(f"/api/leagues/{lg['id']}/leave", headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    assert alice.get(f"/api/leagues/{lg['id']}").status_code == 200


@pytest.mark.parametrize("path", ["/api/leagues/1%20OR%201=1", "/api/leagues/1;DROP%20TABLE%20league"])
def test_sql_injection_in_league_path_rejected(alice, path):
    create(alice)
    assert alice.get(path).status_code == 422
    with Session(engine) as s:
        assert len(s.exec(select(League)).all()) == 1


def test_repo_refuses_non_members_directly(alice, bob):
    lg = create(alice)
    with Session(engine) as s:
        b = repo.user_by_email(s, "bob@example.com")
        assert repo.get_league(s, b, lg["id"]) is None
        assert repo.league_table(s, b, lg["id"]) is None
        assert repo.leave_league(s, b, lg["id"]) is False
        assert repo.delete_league(s, b, lg["id"]) is False
