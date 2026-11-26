"""Race commentary (sim/commentary.py) and the key moments after the flag (sim/highlights.py)."""
import json
from collections import Counter

import pytest

from server import league_race
from sim.commentary import LINES, Commentator
from sim.engine import RaceConfig, RaceEngine
from sim.highlights import MAX_HIGHLIGHTS, build_highlights
from tests.test_league_race import both_ready, close, recv


def run(laps=20, seed=3, team="aurora", **kw) -> RaceEngine:
    e = RaceEngine(RaceConfig(laps=laps, seed=seed, controlled_team=team, **kw))
    while not e.finished:
        e.step(1.0)
    return e


def lines(e: RaceEngine) -> list[dict]:
    return [ev for ev in e.event_log if ev["kind"] == "comm"]


@pytest.fixture(scope="module")
def race():
    return run(laps=25, seed=3)


@pytest.fixture(scope="module")
def wet():
    return run(laps=25, seed=7, weather="changeable")


# ---------------------------------------------------------------- commentary
def test_commentary_is_deterministic_and_never_changes_the_race(monkeypatch):
    a, b = run(seed=5), run(seed=5)
    assert [x["text"] for x in lines(a)] == [x["text"] for x in lines(b)]
    # Same race with the commentator switched off: identical laps and classification.
    for name in ("on_event", "on_leader_lap", "on_finish"):
        monkeypatch.setattr(Commentator, name, lambda self, *a, **k: None)
    c = run(seed=5)
    assert not lines(c)
    assert [x.code for x in a.order()] == [x.code for x in c.order()]
    assert [r["time"] for x in a.cars for r in x.laps] == [r["time"] for x in c.cars for r in x.laps]


def test_feed_covers_the_race(race, wet):
    topics = Counter(x["topic"] for x in lines(race)) + Counter(x["topic"] for x in lines(wet))
    for t in ("start", "lap-one", "pass", "pit", "fastest", "battle", "ten-to-go", "five-to-go",
              "final-lap", "win", "second", "third", "sc", "rain"):
        assert any(k.startswith(t) for k in topics), t
    assert lines(race)[0]["topic"] == "start"


def test_feed_is_paced_like_a_broadcast(race):
    n = len(lines(race))
    overtakes = sum(1 for ev in race.event_log if ev["kind"] == "overtake")
    assert 40 <= n <= 6 * race.cfg.laps          # a few lines a lap, not every pass in the field
    assert n < overtakes + 60


def test_wording_varies(race, wet):
    for e in (race, wet):
        by_topic: dict[str, list[str]] = {}
        for x in lines(e):
            by_topic.setdefault(x["topic"], []).append(x["text"])
        passes = [t for k, ts in by_topic.items() if k.startswith("pass") for t in ts]
        assert len(passes) > 5
        # The same phrasing never comes up twice in a row for a topic.
        for topic, texts in by_topic.items():
            if len(LINES[topic]) > 1:
                templates = [_template_of(topic, t) for t in texts]
                assert all(a != b for a, b in zip(templates, templates[1:])), topic


def _template_of(topic: str, text: str) -> int:
    """Which phrasing a line used: the one whose fixed words it contains."""
    def words(tpl):
        import re
        return [w for w in re.split(r"\{[a-z]+\}", tpl) if w.strip()]
    for i, tpl in enumerate(LINES[topic]):
        if all(w in text for w in words(tpl)):
            return i
    raise AssertionError((topic, text))


def test_deck_never_repeats_across_reshuffles():
    e = RaceEngine(RaceConfig(laps=5, seed=1))
    c = e.commentary
    picks = [c.pick("pass") for _ in range(60)]
    assert all(a != b for a, b in zip(picks, picks[1:]))
    assert set(picks) == set(LINES["pass"])


def test_overtake_lines_name_both_drivers_and_the_place(race):
    surnames = {x.code: x.driver.name.split()[-1] for x in race.cars}
    for x in lines(race):
        if x["topic"] in ("pass", "pass-drs", "lead-pass", "podium-pass"):
            a, b = x["codes"]
            assert surnames[a] in x["text"] and surnames[b] in x["text"]
            if x["topic"] in ("pass", "pass-drs"):
                assert "P" in x["text"]


def test_pit_lines_give_the_stop_time(race):
    pit = [x for x in lines(race) if x["topic"] in ("pit", "pit-quick", "pit-slow")]
    assert pit
    for x in pit:
        assert any(f"{p['stationary']:.1f}s" in x["text"] for p in race.pit_log if p["code"] == x["code"])


def test_safety_car_and_vsc_are_called():
    e = run(laps=15, seed=7, vsc_chance_per_lap=0.5)
    topics = [x["topic"] for x in lines(e)]
    assert {"sc", "vsc"} & set(topics)
    flags = [x for x in lines(e) if x["topic"] in ("sc", "vsc", "sc-in", "green")]
    assert all(x["tone"] == "flag" for x in flags)


def test_team_orders_are_called():
    e = RaceEngine(RaceConfig(laps=12, seed=2, controlled_team="aurora"))
    while e.leader().lap < 3:
        e.step(1.0)
    a, b = sorted((c for c in e.cars if c.driver.team == "aurora"), key=lambda c: -c.dist)
    b.frac = a.frac - 0.5 / a.lap_target + (a.lap - b.lap)     # half a second behind
    assert e.team_order("aurora") is None
    order = [x for x in lines(e) if x["topic"].startswith("order-")]
    assert order and set(order[-1]["codes"]) == {a.code, b.code} and order[-1]["teams"] == ["aurora"]


def test_winner_line_names_the_classified_winner():
    for seed in (3, 5, 9):
        e = run(laps=12, seed=seed)
        win = next(x for x in lines(e) if x["topic"].startswith("win"))
        finish = next(ev for ev in e.event_log if ev["kind"] == "finish")
        crossed = min((c for c in e.cars if c.finished), key=lambda c: c.finish_t)
        assert finish["code"] == crossed.code == win["codes"][0]


def test_player_lines_carry_the_team(race):
    mine = [x for x in lines(race) if "aurora" in x["teams"]]
    assert mine
    assert all(set(x["codes"]) & {"MAR", "OKA"} for x in mine)


def test_snapshot_sends_clean_events():
    e = RaceEngine(RaceConfig(laps=8, seed=3, controlled_team="aurora"))
    sent = []
    while not e.finished:
        e.step(2.0)
        sent += e.snapshot()["events"]
    assert any(ev["kind"] == "comm" for ev in sent)
    for ev in sent:
        if ev["kind"] == "comm":
            assert {"t", "lap", "kind", "text", "code", "teams", "codes", "tone", "topic"} == set(ev)
        else:
            assert {"t", "lap", "kind", "text", "code"} == set(ev)      # no internal stop data
    json.dumps(sent)


def test_sprint_has_commentary():
    e = run(laps=10, seed=4, sprint=True)
    assert lines(e) and build_highlights(e)


# ---------------------------------------------------------------- key moments
def test_highlights_only_after_the_flag():
    e = RaceEngine(RaceConfig(laps=8, seed=3))
    e.step(200.0)
    assert build_highlights(e) == []
    assert e.replay()["highlights"] == []


@pytest.mark.parametrize("seed", [3, 5, 7, 11])
def test_highlights_shape(seed):
    e = run(laps=25, seed=seed, weather="changeable" if seed == 7 else "dry")
    hs = e.replay()["highlights"]
    assert 5 <= len(hs) <= MAX_HIGHLIGHTS
    kinds = [h["kind"] for h in hs]
    assert len(set(kinds)) == len(kinds)
    assert {"overtake", "strategy", "dotd"} <= set(kinds)
    for h in hs:
        assert set(h) == {"kind", "title", "text", "lap", "t", "codes", "teams"}
        assert 1 <= h["lap"] <= e.cfg.laps and 0 <= h["t"] <= e.t
        assert h["title"] and h["text"].endswith(".")
        assert all(e.car(c) for c in h["codes"])
    json.dumps(hs)


def test_move_of_the_race_is_a_real_pass_that_stuck(race):
    h = next(h for h in build_highlights(race) if h["kind"] == "overtake")
    a, b = h["codes"]
    ev = next(ev for ev in race.event_log if ev["kind"] == "overtake" and ev["code"] == a and ev["other"] == b
              and ev["lap"] == h["lap"])
    assert ev["t"] - 5.0 <= h["t"] + 0.1 <= ev["t"]          # the replay starts just before it


def test_winning_strategy_matches_the_winners_stints(race):
    h = next(h for h in build_highlights(race) if h["kind"] == "strategy")
    win = race.order()[0]
    assert h["codes"] == [win.code]
    stops = sum(1 for r in win.laps if r["pit"])
    assert ("No stop" if stops == 0 else "One stop" if stops == 1 else f"{stops} stops") in h["text"]
    assert f"P{win.grid}" in h["text"]


def test_places_gained_and_lost_are_the_extremes(race):
    hs = {h["kind"]: h for h in build_highlights(race)}
    pos = {c.code: i for i, c in enumerate(race.order(), 1)}
    done = [c for c in race.cars if c.finished]
    if "gained" in hs:
        best = max(c.grid - pos[c.code] for c in done)
        c = race.car(hs["gained"]["codes"][0])
        assert c.grid - pos[c.code] == best >= 2
        assert hs["dotd"]["codes"] != hs["gained"]["codes"]
    if "lost" in hs:
        worst = max(pos[c.code] - c.grid for c in done)
        c = race.car(hs["lost"]["codes"][0])
        assert pos[c.code] - c.grid == worst


def test_pit_calls_compare_places_before_and_after():
    e = run(laps=25, seed=7, weather="changeable")
    hs = {h["kind"]: h for h in build_highlights(e)}
    assert "pit-best" in hs
    best = hs["pit-best"]
    import re
    before, after, net = map(int, re.search(r"P(\d+) before the stop, P(\d+) .*\((\d+) place", best["text"]).groups())
    assert before - after == net >= 1
    if "pit-worst" in hs:
        assert hs["pit-worst"]["codes"] != best["codes"]


def test_safety_car_highlight():
    e = run(laps=20, seed=7, vsc_chance_per_lap=0.3)
    h = next((h for h in build_highlights(e) if h["kind"] == "neutral"), None)
    assert h is not None and ("Safety Car" in h["text"])


def test_player_team_marked_in_highlights(race):
    for h in build_highlights(race):
        assert h["teams"] == sorted({race.car(c).driver.team for c in h["codes"]})


# ---------------------------------------------------------------- league races
def test_league_players_get_the_same_commentary(league, alice, bob):
    lg, _ = league
    wa, wb = both_ready(alice, bob, lg)
    recv(wa, "init"); recv(wb, "init")
    room = league_race._rooms[lg["id"]]
    with room.lock:
        while not room.engine.finished:
            room.engine.step(5.0)
        room.save_result()
        room._take_snapshot()

    def feed(ws):
        out = []
        for _ in range(400):
            m = ws.receive_json()
            if m["type"] == "state":
                out += [ev["text"] for ev in m["events"] if ev["kind"] == "comm"]
            if m["type"] == "replay":
                return out, m
        raise AssertionError("no replay")
    fa, ra = feed(wa)
    fb, rb = feed(wb)
    assert fa and fa == fb
    assert ra["commentary"] == rb["commentary"] and [x["text"] for x in ra["commentary"]] == fa
    assert ra["highlights"] and ra["highlights"] == rb["highlights"]
    close(wa, wb)


from tests.test_league_race import league, no_rooms  # noqa: E402,F401  (fixtures)
