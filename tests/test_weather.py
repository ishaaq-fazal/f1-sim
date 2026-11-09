"""Rain, track wetness, wet-weather tyres and how teams react."""
import random

import pytest

from sim import RaceConfig, RaceEngine
from sim.tyres import DRY, WET, condition_delta, ideal_type, needs_second_compound, wear_factor
from sim.weather import Weather


def run(e, step=2.0):
    events = []
    while not e.finished:
        e.step(step)
        events += e.snapshot()["events"]
    return events


# ---------------------------------------------------------------- tyres
@pytest.mark.parametrize("wetness,best", [(0.0, "dry"), (0.1, "dry"), (0.4, "I"), (0.6, "I"), (0.9, "W"), (1.0, "W")])
def test_each_tyre_is_fastest_in_its_conditions(wetness, best):
    pace = {"dry": condition_delta("M", wetness), "I": condition_delta("I", wetness), "W": condition_delta("W", wetness)}
    assert min(pace, key=pace.get) == best
    assert ideal_type(wetness) == best


def test_wet_tyres_overheat_on_a_dry_track_and_slicks_are_hopeless_in_water():
    assert wear_factor("I", 0.0) > 2.5 and wear_factor("W", 0.0) > 3
    assert wear_factor("I", 0.5) == 1.0
    assert condition_delta("I", 0.0) > 5 and condition_delta("W", 0.0) > 10
    assert condition_delta("S", 0.9) > 15


def test_two_compound_rule_is_waived_by_wet_tyres():
    assert needs_second_compound({"M"})
    assert not needs_second_compound({"M", "H"})
    assert not needs_second_compound({"M", "I"})
    assert not needs_second_compound({"W"})


# ---------------------------------------------------------------- weather model
def test_dry_weather_never_rains():
    w = Weather("dry", random.Random(1), 3000)
    for t in range(0, 3000, 10):
        w.step(t, 10)
        assert w.rain == 0 and w.wetness == 0


def test_rain_soaks_the_track_and_it_dries_slowly_afterwards():
    for seed in range(20):
        w = Weather("changeable", random.Random(seed), 3000)
        sp = w.spells[0]
        if sp.end - sp.start > 400 and sp.peak > 0.5:
            break
    t, peak_wet, after = 0.0, 0.0, []
    while t < sp.end + 900:
        w.step(t, 5)
        if sp.start < t < sp.end:
            peak_wet = max(peak_wet, w.wetness)
        if t > sp.end:
            after.append(w.wetness)
        t += 5
    assert peak_wet > 0.4
    assert after[0] > 0.1                      # still wet just after the rain stops ...
    assert after == sorted(after, reverse=True)  # ... then steadily dries ...
    assert after[-1] == 0                      # ... until it's dry again


def test_forecast_sees_rain_coming():
    for seed in range(20):
        w = Weather("changeable", random.Random(seed), 3000)
        if w.spells[0].start > 1300:
            break
    start = w.spells[0].start
    fc = {f["in_min"]: f["chance"] for f in w.forecast(start - 10 * 60 + 60)}
    assert fc[15] > 60 and fc[5] < 20        # rain due in ~10 min: unlikely in 5, likely in 15


def test_bad_weather_mode_rejected():
    with pytest.raises(ValueError):
        RaceEngine(RaceConfig(weather="snow"))


# ---------------------------------------------------------------- races
def test_wet_race_starts_on_wet_tyres_and_nobody_gets_the_compound_penalty():
    e = RaceEngine(RaceConfig(laps=12, seed=4, weather="wet", dnf_chance_per_lap=0))
    assert e.weather.wetness > 0.5
    assert all(c.tyre in WET for c in e.cars)
    events = run(e)
    assert any(ev["text"].startswith("Lights out") and "wet race" in ev["text"] for ev in events)
    assert all(c.penalty_s == 0 for c in e.cars)
    wet_laps = [l["time"] for c in e.cars for l in c.laps if 1 < l["lap"] < 6 and not l["pit"]]
    assert min(wet_laps) > e.track.base_lap_s + 2      # rain makes everyone slower


def _changeable_race():
    """A seed where rain arrives mid-race and clears with time to dry."""
    for seed in range(60):
        e = RaceEngine(RaceConfig(laps=30, seed=seed, weather="changeable", dnf_chance_per_lap=0))
        race_s = 30 * e.track.base_lap_s
        sp = e.weather.spells[0]
        if 0.25 * race_s < sp.start and sp.end < 0.65 * race_s and sp.peak > 0.45:
            return e
    raise AssertionError("no suitable seed")


def test_ai_teams_switch_to_inters_in_the_rain_and_back_to_slicks():
    e = _changeable_race()
    events = run(e)
    kinds = [ev["text"] for ev in events if ev["kind"] == "weather"]
    assert any("Rain is falling" in k for k in kinds) and any("slicks are now" in k for k in kinds)
    ai = [c for c in e.cars if not c.controlled and c.finished]
    went_wet = [c for c in ai if c.used & set(WET)]
    assert len(went_wet) >= len(ai) * 0.7
    # and nearly all of them were back on slicks by the flag
    assert sum(c.tyre in DRY for c in went_wet) >= len(went_wet) * 0.7


def test_player_can_call_inters_and_cars_on_slicks_are_slower_in_the_wet():
    e = RaceEngine(RaceConfig(laps=10, seed=4, weather="wet", controlled_team="aurora",
                              start_tyres={"MAR": "S", "OKA": "I"}, dnf_chance_per_lap=0))
    mar, oka = e.car("MAR"), e.car("OKA")
    assert mar.tyre == "S" and oka.tyre == "I"
    while mar.lap < 2:
        e.step(1.0)
    slick_lap = mar.laps[-1]["time"]
    assert slick_lap > oka.laps[-1]["time"] + 5
    assert e.predicted_plan("MAR")["weather_call"] in WET
    assert e.request_pit("MAR", "I")
    while mar.lap < 3:
        e.step(0.5)
    assert mar.tyre == "I" and mar.pits == 1


def test_ai_does_not_switch_tyres_for_weather_when_too_few_laps_remain_to_pay_back_the_stop():
    e = RaceEngine(RaceConfig(laps=12, seed=4, weather="wet", dnf_chance_per_lap=0))
    car = next(c for c in e.cars if not c.controlled)
    car.tyre = "I"
    e.weather.wetness = 0.0                  # track has dried: inters overheat (~5 s/lap)
    e.weather.spells = []
    car.weather_threshold = 0.6
    _, gain = e._weather_gain(car, 2)
    assert gain > 4                          # a real gain per lap...
    assert e._weather_call(car, 2) is None   # ...but 2 laps can't pay back a ~21 s stop
    assert e._weather_call(car, 10) in DRY
