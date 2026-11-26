"""The 22-circuit calendar: original tracks with their own character, a mixed-up season order,
sprint weekends spread through the year, and a career economy that still balances."""
import pytest

import sim.career as cr
from sim.grid import TEAMS
from sim.season import (ROUNDS, SCHEDULE, SPRINT_TRACKS, is_sprint_round, sprint_rounds,
                        track_for_round)
from sim.track import TRACK_SPECS, get_track

STREET = {"monteverde", "neon", "lanternquay"}


def test_every_circuit_has_its_own_name_and_id():
    names = [s["name"] for s in TRACK_SPECS.values()]
    assert len(set(names)) == len(names) == 22
    assert not set(TRACK_SPECS) & {t[0] for t in TEAMS}        # no circuit shares an id with a team
    assert all(len(tid) <= 20 for tid in TRACK_SPECS)          # fits the track column in the database
    assert all(s["blurb"] for s in TRACK_SPECS.values())


def test_circuits_cover_a_wide_range_of_characters():
    tracks = [get_track(t) for t in TRACK_SPECS]
    lengths = [t.length_m for t in tracks]
    avg_kmh = [t.length_m / t.base_lap_s * 3.6 for t in tracks]
    assert min(lengths) < 3500 and max(lengths) > 6500           # short club track to long road course
    assert min(avg_kmh) < 175 and max(avg_kmh) > 250              # slow streets to a flat-out speedway
    assert min(t.wear_mult for t in tracks) <= 0.7 and max(t.wear_mult for t in tracks) >= 1.5
    assert min(t.overtake_mult for t in tracks) <= 0.4 and max(t.overtake_mult for t in tracks) >= 1.5
    longest = max(tracks, key=lambda t: t.length_m)
    shortest = min(tracks, key=lambda t: t.length_m)
    assert "longest" in longest.blurb and "shortest" in shortest.blurb.lower()


def test_every_circuit_fits_the_map_and_has_slow_and_fast_parts():
    for tid in TRACK_SPECS:
        t = get_track(tid)
        w, h = t.points.max(axis=0) - t.points.min(axis=0)
        assert 0.85 <= w / h <= 2.2, tid                      # drawable in the landscape map panel
        kmh = t.speed * 3.6
        assert kmh.max() > 215 and kmh.min() < 175, tid
        assert t.pit_loss_s > 15


def test_the_calendar_mixes_circuit_types():
    assert ROUNDS == len(SCHEDULE) == 22
    assert [track_for_round(n) for n in range(1, ROUNDS + 1)] == list(SCHEDULE)
    for a, b in zip(SCHEDULE, SCHEDULE[1:]):
        assert not (a in STREET and b in STREET)             # no back-to-back street races
        wa, wb = TRACK_SPECS[a]["wear_mult"], TRACK_SPECS[b]["wear_mult"]
        assert not (wa >= 1.3 and wb >= 1.3), (a, b)         # nor two tyre-killers in a row


def test_six_sprint_weekends_spread_through_the_year_at_tracks_you_can_pass_on():
    rounds = sprint_rounds()
    assert len(rounds) == len(SPRINT_TRACKS) == 6
    assert all(b - a >= 3 for a, b in zip(rounds, rounds[1:]))
    assert rounds[0] > 1 and rounds[-1] < ROUNDS              # not the opener, not the finale
    for n in rounds:
        assert is_sprint_round(n) and TRACK_SPECS[track_for_round(n)]["overtake_mult"] >= 0.9


@pytest.mark.parametrize("pts", [0, 6, 18, 43])
def test_a_longer_season_pays_about_the_same_as_before(pts):
    """Prize money and development points per round are scaled so the season totals match the
    10-round season the economy was tuned for."""
    st = cr.new_career("solace", 1)
    budget, dp = st["budget"], st["dev_points"]
    for _ in range(ROUNDS):
        cr.credit_round(st, pts)
    old_money = cr.ECONOMY_ROUNDS * (cr.ROUND_PRIZE_BASE + cr.ROUND_PRIZE_PER_POINT * pts)
    assert st["budget"] - budget == pytest.approx(old_money, rel=0.06, abs=0.3)
    old_dp = cr.ECONOMY_ROUNDS * (cr.DP_PER_ROUND_BASE + pts // cr.DP_PER_ROUND_POINTS)
    assert old_dp <= st["dev_points"] - dp <= old_dp + cr.ECONOMY_ROUNDS


def test_board_points_target_scales_with_the_season():
    st = cr.new_career("aurora", 1)
    target = next(o["target"] for o in cr.set_objectives(st)["items"] if o["kind"] == "points")
    assert target > cr.EXPECTED_POINTS[0] * cr.POINTS_TARGET_SHARE      # well above a 10-round target
