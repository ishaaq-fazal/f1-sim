"""Team balance and qualifying: the car you pick matters, but it doesn't decide the race."""
from statistics import mean

from sim import TEAMS, RaceConfig, RaceEngine, default_grid
from sim.engine import QUALI_MODES


def run(e):
    while not e.finished:
        e.step(2.0)


def test_team_pace_gap_is_narrow():
    pace = [t[3] for t in TEAMS]
    assert max(pace) - min(pace) <= 0.5               # fastest to slowest car, s/lap
    assert pace == sorted(pace)                        # the pecking order is still there


def test_qualifying_sets_the_grid():
    e = RaceEngine(RaceConfig(laps=5, seed=3))
    q = e.qualifying
    assert [r["pos"] for r in q] == list(range(1, 21))
    assert {r["code"] for r in q} == {d.code for d in default_grid()}
    assert all(e.car(r["code"]).grid == r["pos"] for r in q)
    assert q[0]["gap"] == 0 and all(a["time"] <= b["time"] for a, b in zip(q, q[1:]))
    assert all(r["tyre"] == "S" for r in q)           # dry: everyone qualifies on Softs
    assert any(ev["kind"] == "quali" and "Pole position" in ev["text"] for ev in e.events)


def test_wet_qualifying_uses_wet_tyres():
    e = RaceEngine(RaceConfig(laps=5, seed=3, weather="wet"))
    assert all(r["tyre"] in ("I", "W") for r in e.qualifying)


def test_push_is_quicker_but_riskier_than_playing_safe():
    def laps(mode):
        times, mistakes = [], 0
        for seed in range(150):
            e = RaceEngine(RaceConfig(laps=5, seed=seed, controlled_team="solace",
                                      quali_modes={"HAL": mode, "DUV": mode}))
            for r in e.qualifying:
                if r["team"] == "solace":
                    assert r["mode"] == mode
                    mistakes += r["mistake"]
                    if not r["mistake"]:
                        times.append(r["time"])
        return mean(times), mistakes
    push_t, push_m = laps("push")
    safe_t, safe_m = laps("safe")
    assert push_t < safe_t and push_m > safe_m
    assert QUALI_MODES["push"][0] < 0 < QUALI_MODES["safe"][0]


def test_quali_calls_only_apply_to_the_players_cars():
    e = RaceEngine(RaceConfig(laps=5, seed=1, controlled_team="aurora", quali_modes={"KOV": "push", "MAR": "safe"}))
    modes = {r["code"]: r["mode"] for r in e.qualifying}
    assert modes["KOV"] == "normal" and modes["MAR"] == "safe"


def test_the_fastest_team_does_not_always_win():
    order = [t[0] for t in TEAMS]
    wins = {t: 0 for t in order}
    back_half_points = 0
    n = 60
    for seed in range(n):
        e = RaceEngine(RaceConfig(laps=10, seed=500 + seed, dnf_chance_per_lap=0))
        run(e)
        res = e.order()
        wins[res[0].driver.team] += 1
        back_half_points += sum(1 for c in res[:10] if order.index(c.driver.team) >= 5)
    assert wins["aurora"] < 0.6 * n                      # was ~75% before the balance change
    assert sum(1 for w in wins.values() if w) >= 4      # several teams win races
    assert back_half_points >= n * 1.5                  # slower teams regularly score points
