import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server.main import app
from tests.conftest import ORIGIN


def start(ws, team="aurora", laps=5, seed=1):
    ws.send_text(json.dumps({"type": "start", "team": team, "laps": laps, "seed": seed}))
    init = ws.receive_json()
    assert init["type"] == "init"
    state = ws.receive_json()
    assert state["type"] == "state"
    return state


def next_of(ws, kind, limit=50):
    for _ in range(limit):
        m = ws.receive_json()
        if m["type"] == kind:
            return m
    raise AssertionError(f"no {kind} message")


def test_security_headers(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"


def test_setup_lists_teams(client):
    data = client.get("/api/setup").json()
    assert len(data["teams"]) == 10 and set(data) == {"teams", "tracks", "laps_default", "speeds"}


def test_source_and_dotfiles_not_served(client):
    for path in ("/server/main.py", "/.env", "/../server/config.py", "/sim/engine.py"):
        assert client.get(path).status_code == 404


def test_api_docs_hidden_outside_dev(monkeypatch):
    # docs are only enabled when APP_ENV=dev; the error handler never leaks details
    r = TestClient(app, raise_server_exceptions=False).get("/api/nope")
    assert r.status_code == 404


def test_ws_rejects_foreign_origin(client):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws/race", headers={"origin": "https://evil.example"}) as ws:
            ws.receive_text()
    assert exc.value.code == 1008


def test_ws_rejects_missing_origin(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/race") as ws:
            ws.receive_text()


@pytest.mark.parametrize("payload", [
    "not json",
    '{"type": "start", "team": "aurora", "laps": 5, "admin": true}',              # extra field
    '{"type": "start", "team": "aurora\'; DROP TABLE users;--", "laps": 5}',        # injection-style text
    '{"type": "start", "team": "<script>alert(1)</script>", "laps": 5}',
    '{"type": "start", "team": "aurora", "laps": 100000}',                         # out of range
    '{"type": "speed", "value": 1000}',
    '{"type": "teleport"}',
])
def test_ws_rejects_invalid_messages(client, payload):
    with client.websocket_connect("/ws/race", headers=ORIGIN) as ws:
        ws.send_text(payload)
        msg = ws.receive_json()
        assert msg == {"type": "error", "detail": "Invalid message"}


def test_ws_unknown_team(client):
    with client.websocket_connect("/ws/race", headers=ORIGIN) as ws:
        ws.send_text(json.dumps({"type": "start", "team": "nobody", "laps": 5}))
        assert ws.receive_json()["detail"] == "Unknown team"


def test_ws_runs_race_and_accepts_orders_for_own_team(client):
    with client.websocket_connect("/ws/race", headers=ORIGIN) as ws:
        state = start(ws)
        mine = [c for c in state["cars"] if c["controlled"]]
        assert len(mine) == 2 and all(c["team"] == "aurora" for c in mine)
        ws.send_text(json.dumps({"type": "pit", "driver": mine[0]["code"], "compound": "H"}))
        ws.send_text(json.dumps({"type": "mode", "driver": mine[1]["code"], "mode": "push"}))
        st = next_of(ws, "state")
        for _ in range(5):
            cars = {c["code"]: c for c in st["cars"]}
            if cars[mine[0]["code"]]["pit_request"] == "H" and cars[mine[1]["code"]]["mode"] == "push":
                break
            st = next_of(ws, "state")
        assert cars[mine[0]["code"]]["pit_request"] == "H"
        assert cars[mine[1]["code"]]["mode"] == "push"
        assert st["predictions"] and all(p["code"] in {m["code"] for m in mine} for p in st["predictions"])


def test_ws_refuses_orders_for_other_teams(client):
    with client.websocket_connect("/ws/race", headers=ORIGIN) as ws:
        state = start(ws, team="aurora")
        rival = next(c for c in state["cars"] if not c["controlled"])
        ws.send_text(json.dumps({"type": "pit", "driver": rival["code"], "compound": "S"}))
        assert next_of(ws, "error")["detail"] == "Not your driver"


def test_ws_orders_need_a_race(client):
    with client.websocket_connect("/ws/race", headers=ORIGIN) as ws:
        ws.send_text(json.dumps({"type": "pit", "driver": "MAR", "compound": "S"}))
        assert ws.receive_json()["detail"] == "Start a race first"


def test_ws_message_rate_limit(client):
    with client.websocket_connect("/ws/race", headers=ORIGIN) as ws:
        for _ in range(15):
            ws.send_text(json.dumps({"type": "speed", "value": 2}))
        replies = [ws.receive_json() for _ in range(15)]
        assert {"type": "error", "detail": "Slow down"} in replies


def test_ws_oversized_message_closes(client):
    with client.websocket_connect("/ws/race", headers=ORIGIN) as ws:
        ws.send_text("x" * 5000)
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
        assert exc.value.code == 1009


def test_setup_lists_tracks_and_team_drivers(client):
    data = client.get("/api/setup").json()
    assert {t["id"] for t in data["tracks"]} == {"harbourline", "monteverde", "sandridge", "alderwood"}
    assert all(len(t["outline"]) > 50 for t in data["tracks"])
    aurora = next(t for t in data["teams"] if t["id"] == "aurora")
    assert [d["code"] for d in aurora["drivers"]] == ["MAR", "OKA"]


def test_ws_start_on_chosen_track_and_tyres(client):
    with client.websocket_connect("/ws/race", headers=ORIGIN) as ws:
        ws.send_text(json.dumps({"type": "start", "team": "aurora", "laps": 5, "seed": 1,
                                 "track": "monteverde", "tyres": {"MAR": "H", "OKA": "S"}}))
        init = ws.receive_json()
        assert init["track"]["id"] == "monteverde"
        cars = {c["code"]: c for c in ws.receive_json()["cars"]}
        assert cars["MAR"]["tyre"] == "H" and cars["OKA"]["tyre"] == "S"


@pytest.mark.parametrize("extra,detail", [
    ({"track": "nowhere"}, "Unknown track"),
    ({"tyres": {"BRA": "S"}}, "Starting tyres can only be set for your own drivers"),
    ({"tyres": {"MAR": "X"}}, "Invalid message"),
    ({"tyres": {"MAR": "S", "OKA": "S", "BRA": "S"}}, "Invalid message"),
    ({"track": "../etc"}, "Invalid message"),
])
def test_ws_rejects_bad_track_or_tyres(client, extra, detail):
    with client.websocket_connect("/ws/race", headers=ORIGIN) as ws:
        ws.send_text(json.dumps({"type": "start", "team": "aurora", "laps": 5, **extra}))
        assert ws.receive_json()["detail"] == detail
