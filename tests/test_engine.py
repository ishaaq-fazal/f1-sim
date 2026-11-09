from sim import RaceConfig, RaceEngine
from sim.strategy import best_plan
from sim.tyres import COMPOUNDS


def run(engine, step=2.0, limit=20000):
    for _ in range(limit):
        if engine.finished:
            return
        engine.step(step)
    raise AssertionError("race never finished")


def test_full_race_finishes_and_classifies_everyone():
    e = RaceEngine(RaceConfig(laps=20, seed=1))
    run(e)
    snap = e.snapshot()
    assert len(snap["cars"]) == 20
    winner = e.order()[0]
    assert winner.finished and winner.lap == 20
    for c in e.cars:
        assert c.finished or c.retired
    # the classification is sorted: finished cars by laps then time
    finished = [c for c in e.order() if c.finished]
    keys = [(-c.lap, c.finish_t + c.penalty_s) for c in finished]
    assert keys == sorted(keys)


def test_same_seed_same_race():
    a, b = RaceEngine(RaceConfig(laps=10, seed=99)), RaceEngine(RaceConfig(laps=10, seed=99))
    run(a), run(b)
    assert [c.code for c in a.order()] == [c.code for c in b.order()]


def test_lap_times_are_realistic():
    e = RaceEngine(RaceConfig(laps=10, seed=3, mistake_chance_per_lap=0, vsc_chance_per_lap=0))
    run(e)
    clean = [l["time"] for c in e.cars for l in c.laps if l["lap"] > 1 and not l["pit"]]
    assert 84 < min(clean) and max(clean) < 100


def test_ai_cars_use_two_compounds():
    e = RaceEngine(RaceConfig(laps=25, seed=5, dnf_chance_per_lap=0))
    run(e)
    for c in e.cars:
        assert len(c.used) >= 2 and c.penalty_s == 0, c.code


def test_player_cars_only_pit_when_told_and_get_penalty_otherwise():
    e = RaceEngine(RaceConfig(laps=12, seed=8, controlled_team="aurora", dnf_chance_per_lap=0))
    mine = [c for c in e.cars if c.controlled]
    assert len(mine) == 2
    first, second = mine
    while first.lap < 3:
        e.step(1.0)
    assert first.pits == 0
    start = first.tyre
    new = "H" if start != "H" else "M"
    assert e.request_pit(first.code, new)
    target = first.lap + 1
    while first.lap < target:
        e.step(0.5)
    assert first.pits == 1 and first.tyre == new and first.tyre_age == 0
    run(e)
    assert second.pits == 0 and second.penalty_s == 10.0
    assert first.penalty_s == 0


def test_bad_orders_are_refused():
    e = RaceEngine(RaceConfig(laps=5, seed=2))
    code = e.cars[0].code
    assert not e.request_pit("ZZZ", "S")
    assert not e.request_pit(code, "X")
    assert not e.set_mode(code, "flat-out")


def test_tyres_degrade_and_softs_are_fastest_when_new():
    for c in COMPOUNDS.values():
        assert c.delta(10) > c.delta(0)
        assert c.delta(c.cliff_lap + 5) - c.delta(c.cliff_lap + 4) > c.delta(5) - c.delta(4)
    assert COMPOUNDS["S"].delta(0) < COMPOUNDS["M"].delta(0) < COMPOUNDS["H"].delta(0)


def test_strategy_respects_two_compound_rule_and_stint_limits():
    for laps in (15, 30, 58):
        p = best_plan(laps, 21.5)
        compounds = {p.start_compound} | {c for _, c in p.stops}
        assert len(compounds) >= 2
        assert all(0 < n < laps for n, _ in p.stops)
