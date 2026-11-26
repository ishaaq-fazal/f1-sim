"""TV-style race commentary.

The engine tells the Commentator about each race-control event (an overtake, a stop, a Safety
Car...) and about each lap the leader completes. From those it writes short lines in a
commentator's voice: overtakes, battles, pit stop times, fastest laps, the weather, Safety Cars,
team orders and the fight at the end. They go out as events of kind "comm", next to (not instead
of) the race-control log.

It only reads the race: it never changes a car, and its wording comes from its own random stream
seeded from the race seed, so the same race always gets the same commentary (and both players of
a league race, who watch the same engine, read the same lines). Each topic has several phrasings,
dealt like a shuffled deck so the same one doesn't come round again straight away.
"""
from __future__ import annotations

import random
from typing import TYPE_CHECKING

from .grid import TEAMS
from .tyres import COMPOUNDS

if TYPE_CHECKING:
    from .engine import Car, RaceEngine

TEAM_NAMES = {t[0]: t[1] for t in TEAMS}
BATTLE_GAP_S = 1.0        # within this of the car ahead counts as a battle
BATTLE_LAPS = 3           # laps in a row before the commentary calls it a battle
BATTLE_REPEAT = 6         # leader laps before the same pair is talked about again
# Minor moments (a midfield pass, a routine stop) are only called when the commentary has been
# quiet for a while, so the feed reads like a broadcast rather than a lap chart.
MINOR_GAP_S = 25.0        # race seconds since the last minor line
TOP_GAP_S = 8.0           # the same, for moments in the top six

# {a} and {b} are drivers' surnames, {ta} {tb} their teams, {p} a position number.
LINES: dict[str, list[str]] = {
    "start": [
        "It's lights out and away we go! {a} starts from pole.",
        "And they're off! {a} on pole, {b} alongside.",
        "Lights out! {a} gets away from pole with {b} right behind.",
    ],
    "lap-one": [
        "End of the opening lap, and {a} leads.",
        "{a} leads at the end of lap one.",
        "One lap down: {a} in front, {b} in second.",
    ],
    "start-lead-lost": [
        "What a start from {a}! Up from P{g} to lead the opening lap.",
        "{a} has snatched the lead! P{g} to P1 on lap one.",
    ],
    "start-gain": [
        "A flying start from {a}, up {n} places to P{p} on the opening lap.",
        "{a} has made up {n} places already. P{g} on the grid, P{p} now.",
        "Look at {a} go! {n} places gained on lap one for {ta}.",
    ],
    "start-loss": [
        "A nightmare opening lap for {a}, down {n} places to P{p}.",
        "{a} has gone backwards at the start: P{g} on the grid, P{p} now.",
    ],
    "lead-pass": [
        "{a} takes the lead! Past {b} and into P1.",
        "There's a new leader! {a} gets by {b}.",
        "{a} is through on {b}, and {a} leads the race!",
        "Brilliant from {a}! {b} can't hold on, and the lead changes hands.",
    ],
    "podium-pass": [
        "{a} passes {b} for P{p}, and that's a podium place.",
        "Into the top three goes {a}, at the expense of {b}.",
        "{a} gets the job done on {b}. P{p} for {ta}.",
    ],
    "pass": [
        "{a} goes past {b} for P{p}.",
        "{a} makes the move on {b}. That's P{p}.",
        "Good move by {a}, round the outside of {b} for P{p}.",
        "{b} can't defend that one. {a} up to P{p}.",
        "{a} is through! {b} drops to P{q}.",
        "Up the inside goes {a}, and {b} has no answer. P{p}.",
    ],
    "pass-drs": [
        "DRS open and {a} breezes past {b} for P{p}.",
        "{a} uses the DRS to get by {b}. P{p}.",
        "Easy with the flap open: {a} past {b} on the straight for P{p}.",
    ],
    "battle-lead": [
        "{b} is all over the back of {a} in the fight for the lead. {gap}s between them.",
        "The battle for the lead is on: {b} has been within a second of {a} for {n} laps.",
        "{a} is under real pressure up front, {b} right with them.",
    ],
    "battle": [
        "Great scrap for P{p}: {b} has been glued to {a} for {n} laps.",
        "{b} is hunting {a} for P{p}, just {gap}s behind.",
        "Keep an eye on {a} and {b}, nose to tail for P{p}.",
        "{a} is defending hard from {b}. The gap is {gap}s.",
    ],
    "pit": [
        "{a} pits from P{p}. {s}s stationary, out goes {tyre}.",
        "In comes {a} for {tyre}s. {s}s, a tidy stop by {ta}.",
        "Pit stop for {a}: {s}s and they switch to {tyre}s.",
        "{ta} bring {a} in from P{p}. {s}s, on with the {tyre}s.",
    ],
    "pit-quick": [
        "Lightning stop for {a}! Just {s}s in the box.",
        "What a stop by {ta}! {a} away in {s}s.",
    ],
    "pit-slow": [
        "Oh no, a slow stop for {a}! {s}s stationary, that's cost them dearly.",
        "Problems in the {ta} pit box! {a} sits there for {s}s.",
        "Disaster in the pits for {a}, {s}s before they're released.",
    ],
    "pit-flurry": [
        "{n} cars have taken the cheap stop under the {flag}.",
        "Busy pit lane: {n} cars in under the {flag} so far.",
    ],
    "pit-neutral": [
        "{a} takes the cheap stop under the {flag}. {s}s, on to {tyre}s.",
        "{ta} react quickly and bring {a} in under the {flag}.",
        "In comes {a} under the {flag}. That stop costs much less now.",
    ],
    "rejoin": [
        "{a} rejoins in P{p}.",
        "Out comes {a}, back in P{p}.",
        "{a} is back on track in P{p}.",
    ],
    "rejoin-behind": [
        "{a} rejoins in P{p}, right behind {b}.",
        "{a} comes out just behind {b}, in P{p}.",
    ],
    "undercut": [
        "The undercut has worked! {a} is now ahead of {b}.",
        "{ta} timed that perfectly: {a} has jumped {b} in the stops.",
        "{b} comes out behind {a}. Advantage {ta} after the stops.",
    ],
    "fastest": [
        "Purple! {a} sets the fastest lap: {time}.",
        "Fastest lap of the race now belongs to {a}, a {time}.",
        "{a} goes quickest of anyone so far: {time}.",
        "{a} lights up the timing screens with a {time}.",
    ],
    "rain": [
        "Here comes the rain! It's {rain} rain for now.",
        "Spots of rain on the camera lenses: {rain} rain.",
        "The rain has arrived, {rain} at the moment. This could shake things up.",
    ],
    "rain-stops": [
        "The rain has stopped. Now the question is when the track dries.",
        "No more rain falling. The teams will be watching that dry line appear.",
    ],
    "ideal-I": [
        "It's getting wet enough for Intermediates. Who blinks first?",
        "That's Intermediate weather now. The slick runners are struggling.",
    ],
    "ideal-W": [
        "There's standing water now. Full Wets are the tyre to be on.",
        "It's really coming down: this is full Wet weather.",
    ],
    "ideal-dry": [
        "The track is drying. Slicks are the quicker tyre now!",
        "A dry line has appeared. Time to gamble on slicks?",
    ],
    "sc": [
        "Safety Car! {why} The field will bunch right up.",
        "The Safety Car is out. {why} Every gap is wiped out.",
        "We have a Safety Car. {why} A cheap pit stop for anyone who wants one.",
    ],
    "vsc": [
        "Virtual Safety Car. {why} Everyone slows, the gaps freeze.",
        "The VSC is out. {why} Will anyone dive into the pits?",
    ],
    "sc-in": [
        "The Safety Car lights are out. It comes in this lap!",
        "Safety Car in this lap. {a} will control the restart.",
    ],
    "green": [
        "Green flag! {a} gets the restart away cleanly.",
        "We're racing again! {a} leads the restart.",
        "And we're back to green. {a} bolts away.",
    ],
    "crash": [
        "Big crash for {a}! Out of the race from P{p}.",
        "{a} is in the wall! That's the end of their race.",
        "Oh, {a} has lost it and crashed out!",
    ],
    "dnf": [
        "{a} is slowing... it's a power unit failure. Race over from P{p}.",
        "Heartbreak for {ta}: {a} retires with a mechanical problem.",
        "Smoke from the back of {a}'s car! That's a retirement.",
    ],
    "mistake": [
        "{a} runs wide and loses time.",
        "A big lock-up for {a}, off the track and back on.",
        "Moment for {a}! A trip across the run-off costs them.",
    ],
    "order-obeyed": [
        "Team orders at {ta}: {a} waves {b} through.",
        "{a} moves aside for team-mate {b}. Orders obeyed.",
    ],
    "order-ignored": [
        "{a} is ignoring the team order! {b} is stuck behind.",
        "Tension at {ta}: {a} won't let {b} through.",
        "No swap at {ta}. {a} has other ideas.",
    ],
    "penalty": [
        "{a} gets a 10 second penalty: only one compound used.",
    ],
    "gap": [
        "{a} leads by {gap}s from {b}.",
        "{a} still in front, {gap}s clear of {b}.",
        "{a} controlling the race, {gap}s up on {b}.",
        "Gap at the front is {gap}s: {a} from {b}.",
    ],
    "ten-to-go": [
        "Ten laps to go. {a} leads {b} by {gap}s.",
        "Ten to go, and it's {a} from {b}, {gap}s apart.",
    ],
    "five-to-go": [
        "Five laps left! {a} leads by {gap}s.",
        "Into the final five laps. {a} is {gap}s up on {b}.",
    ],
    "close-finish": [
        "This is going down to the wire! {b} is only {gap}s behind {a}.",
        "{b} is closing in on {a}: {gap}s with {n} laps left!",
        "Can {a} hold on? {b} is right there, {gap}s back.",
    ],
    "final-lap": [
        "Final lap! {a} leads by {gap}s.",
        "Last lap! It's {a}'s to lose, {gap}s ahead of {b}.",
        "One lap to go. {a} leads, {b} is {gap}s back.",
    ],
    "final-lap-close": [
        "Last lap and {b} is {gap}s behind {a}! One more chance!",
        "Final lap, {gap}s in it! Can {b} find a way past {a}?",
    ],
    "win": [
        "{a} wins! A brilliant drive for {ta}.",
        "Chequered flag, and {a} takes the victory for {ta}!",
        "And {a} wins it! What a race for {ta}.",
    ],
    "win-from-back": [
        "{a} wins from P{g} on the grid! What a drive!",
        "From P{g} to victory! {a} wins for {ta}!",
    ],
    "second": [
        "{a} crosses the line in second.",
        "Second place for {a}.",
    ],
    "third": [
        "{a} completes the podium in P3.",
        "Third for {a}, and a trophy for {ta}.",
    ],
    "player-finish": [
        "{a} brings it home in P{p} for {ta}.",
        "P{p} at the flag for {ta}'s {a}.",
        "{a} takes the flag in P{p}.",
    ],
}


def surname(name: str) -> str:
    return name.split()[-1] if name else name


class Commentator:
    def __init__(self, engine: "RaceEngine"):
        self.e = engine
        seed = engine.cfg.seed
        self.rng = random.Random(f"commentary-{seed}" if seed is not None else None)
        self.decks: dict[str, list[int]] = {}
        self.lines: list[dict] = []          # every line, in order
        self.close: dict[tuple[str, str], int] = {}     # pair -> leader laps in a row within a second
        self.battle_said: dict[tuple[str, str], int] = {}
        self.fastest_said = -99
        self.minor_t = -99.0                 # race time of the last minor line
        self.neutral_stops = 0               # stops under the current Safety Car / VSC
        self.neutral_said = 0
        self.mistakes_said = 0
        self.last_lap = 0
        self.flag = "Safety Car"

    # -------------------------------------------------------------- output
    def _player_teams(self) -> set[str]:
        cfg = self.e.cfg
        return set(cfg.controlled_teams) | ({cfg.controlled_team} if cfg.controlled_team else set())

    def _quiet(self, gap_s: float = MINOR_GAP_S) -> bool:
        """True (and the clock restarts) if no minor line came in the last gap_s seconds."""
        if self.e.t - self.minor_t < gap_s:
            return False
        self.minor_t = self.e.t
        return True

    def pick(self, topic: str) -> str:
        """Next phrasing for a topic: a shuffled deck, reshuffled once used up, never starting
        the new deck with the line that ended the old one."""
        options = LINES[topic]
        deck = self.decks.get(topic)
        if not deck:
            last = self.decks.get(topic + "#last")
            deck = list(range(len(options)))
            self.rng.shuffle(deck)
            if len(deck) > 1 and last and deck[-1] == last[0]:
                deck[0], deck[-1] = deck[-1], deck[0]
            self.decks[topic] = deck
        i = deck.pop()
        self.decks[topic + "#last"] = [i]
        return options[i]

    def say(self, topic: str, cars: list["Car"] = (), tone: str = "normal", **fmt) -> None:
        cars = [c for c in cars if c is not None]
        names = {}
        for key, car in zip(("a", "b"), cars):
            names[key] = surname(car.driver.name)
            names["t" + key] = TEAM_NAMES.get(car.driver.team, car.driver.team.title())
        text = self.pick(topic).format(**names, **fmt)
        teams = sorted({c.driver.team for c in cars})
        codes = [c.code for c in cars]
        self.lines.append({"t": self.e.t, "topic": topic})
        self.e._event("comm", text, codes[0] if codes else None, teams=teams, codes=codes, tone=tone, topic=topic)

    # ---------------------------------------------------------- race events
    def on_event(self, ev: dict) -> None:
        kind, e = ev["kind"], self.e
        if kind == "start":
            grid = sorted(e.cars, key=lambda c: c.grid)
            self.say("start", grid[:2], tone="big")
            return
        car = e.car(ev["code"]) if ev.get("code") else None
        mine = self._player_teams()
        involved = lambda *cs: any(c is not None and c.driver.team in mine for c in cs)  # noqa: E731
        if kind == "overtake" and car is not None:
            other, p = e.car(ev["other"]), ev["pos"]
            topic = "pass-drs" if ev.get("drs") else "pass"
            if p == 1:
                self.say("lead-pass", [car, other], tone="big")
            elif p <= 3:
                self.say("podium-pass", [car, other], tone="big", p=p)
            elif involved(car, other):
                self.say(topic, [car, other], p=p, q=p + 1)
            elif car.lap == 0:
                return                               # the first-lap scramble gets a summary instead
            elif self._quiet(TOP_GAP_S if p <= 6 else MINOR_GAP_S):
                self.say(topic, [car, other], p=p, q=p + 1)
        elif kind == "pit" and car is not None:
            stop = ev["stop"]
            fmt = dict(p=stop["from_pos"], s=f"{stop['stationary']:.1f}", tyre=COMPOUNDS[stop["new"]].name)
            top = stop["from_pos"] <= 6
            if stop["neutral"]:
                self.neutral_stops += 1
            if stop["slow"] and (stop["from_pos"] <= 12 or involved(car) or self._quiet()):
                self.say("pit-slow", [car], tone="bad", **fmt)
            elif stop["neutral"] and (involved(car) or (top and self.neutral_said < 3)):
                self.neutral_said += 1
                self.say("pit-neutral", [car], flag="Safety Car" if stop["neutral"] == "sc" else "VSC", **fmt)
            elif stop["neutral"]:
                return
            elif stop["stationary"] <= 2.1 and (top or involved(car) or self._quiet()):
                self.say("pit-quick", [car], **fmt)
            elif involved(car) or (top and self._quiet(TOP_GAP_S)) or self._quiet():
                self.say("pit", [car], **fmt)
        elif kind == "pit-out" and car is not None:
            self._rejoin(car, ev, involved(car))
        elif kind == "fastest" and car is not None:
            laps_left = e.cfg.laps - car.lap
            if ev["lap"] - self.fastest_said >= 3 or laps_left <= 5 or involved(car):
                self.fastest_said = ev["lap"]
                from .engine import fmt_time
                self.say("fastest", [car], tone="purple", time=fmt_time(e.fastest["time"]))
        elif kind == "weather" and ev.get("topic"):
            self.say(ev["topic"], tone="weather", rain=ev.get("rain", "light rain"))
        elif kind in ("sc", "vsc") and ev.get("sc_in"):
            self.say("sc-in", [e.leader()], tone="flag")
        elif kind in ("sc", "vsc"):
            self.neutral_stops = self.neutral_said = 0
            self.flag = "Safety Car" if kind == "sc" else "VSC"
            who = e.car(ev["who"]) if ev.get("who") else None
            why = ("Debris on the track." if ev.get("cause") == "debris" else
                   f"{surname(who.driver.name)} has crashed." if who and ev.get("cause") == "crash" else
                   f"{surname(who.driver.name)} has stopped on track." if who else "")
            self.say(kind, [], tone="flag", why=why)
        elif kind == "green":
            self.say("green", [e.leader()], tone="flag")
        elif kind == "crash" and car is not None:
            self.say("crash", [car], tone="bad", p=ev.get("pos", 0))
        elif kind == "dnf" and car is not None:
            self.say("dnf", [car], tone="bad", p=ev.get("pos", 0))
        elif kind == "mistake" and car is not None and car.lap > 0:
            p = e.position(car)
            if involved(car) or (p <= 5 and self._quiet(TOP_GAP_S)):
                self.say("mistake", [car])
        elif kind == "radio" and ev.get("order") in ("obeyed", "ignored"):
            other = e.car(ev["other"])
            self.say("order-" + ev["order"], [car, other], tone="radio")
        elif kind == "penalty" and car is not None:
            self.say("penalty", [car], tone="bad")

    def _rejoin(self, car: "Car", ev: dict, mine: bool) -> None:
        e, stop = self.e, ev.get("stop")
        p = ev["pos"]
        # The undercut: someone who was ahead before this car stopped, and has stopped since,
        # now comes out behind it. Checked when the rival rejoins.
        for other in reversed(e.pit_log):
            if other is stop or other["code"] == car.code or other["lap"] < car.lap - 5:
                continue
            if other["rejoin_pos"] is None or stop is None or other["neutral"] or stop["neutral"]:
                continue
            before = other["order"]
            if (car.code in before and other["code"] in before
                    and 0 < before.index(other["code"]) - before.index(car.code) <= 2):
                rival = e.car(other["code"])
                if (rival is not None and rival.running and e.position(rival) < p
                        and e.position(rival) <= 10):
                    self.say("undercut", [rival, car], tone="big")
                    return
        if p <= 6 or mine:
            ahead = next((c for c in e.order() if e.position(c) == p - 1), None)
            if ahead is not None and ahead.running and (ahead.dist - car.dist) * car.lap_target < 1.5:
                self.say("rejoin-behind", [car, ahead], p=p)
            elif mine:
                self.say("rejoin", [car], p=p)

    # ---------------------------------------------------------- each leader lap
    def on_leader_lap(self, lap: int) -> None:
        if lap <= self.last_lap:
            return
        self.last_lap = lap
        e = self.e
        order = [c for c in e.order() if c.running]
        if len(order) < 2:
            return
        lead, second = order[0], order[1]
        gap = max(0.0, (lead.dist - second.dist) * second.lap_target)
        laps_left = e.cfg.laps - lap
        if lap == 1:
            self._lap_one(order)
            return
        if e.neutral:
            if self.neutral_stops >= 5 and self.neutral_stops > self.neutral_said + 2:
                self.say("pit-flurry", [], n=self.neutral_stops, flag=self.flag)
                self.neutral_said = self.neutral_stops
            return
        g = f"{gap:.1f}"
        if laps_left == 1:
            self.say("final-lap-close" if gap < 1.5 else "final-lap", [lead, second], tone="big", gap=g)
            return
        if laps_left == 10 and e.cfg.laps >= 18:
            self.say("ten-to-go", [lead, second], gap=g)
        elif laps_left == 5 and e.cfg.laps >= 10:
            self.say("five-to-go", [lead, second], gap=g)
        elif laps_left <= 4 and gap < 1.2:
            self.say("close-finish", [lead, second], tone="big", gap=g, n=laps_left)
            self._battles(order, skip_lead=True)
            return
        elif lap % 7 == 0 and laps_left > 5:
            self.say("gap", [lead, second], gap=g, lap=lap)
        self._battles(order)

    def _lap_one(self, order: list["Car"]) -> None:
        lead = order[0]
        if lead.grid == 1:
            self.say("lap-one", [lead, order[1]])
        else:
            self.say("start-lead-lost", [lead], tone="big", g=lead.grid)
        mine = self._player_teams()
        pos = {c.code: i for i, c in enumerate(self.e.order(), 1)}
        moves = sorted(((c.grid - pos[c.code], c) for c in order if c is not lead), key=lambda x: -x[0])
        if moves and moves[0][0] >= 3:
            n, c = moves[0]
            self.say("start-gain", [c], n=n, p=pos[c.code], g=c.grid)
        worst = min(moves, key=lambda x: x[0]) if moves else None
        if worst and worst[0] <= -4 and worst[1].grid <= 10:
            n, c = worst
            self.say("start-loss", [c], tone="bad", n=-n, p=pos[c.code], g=c.grid)
        for n, c in moves:                         # the player's own cars, if they moved a lot
            if c.driver.team in mine and abs(n) >= 3 and moves[0][1] is not c and (not worst or worst[1] is not c):
                self.say("start-gain" if n > 0 else "start-loss", [c], tone="normal" if n > 0 else "bad",
                         n=abs(n), p=pos[c.code], g=c.grid)

    def _battles(self, order: list["Car"], skip_lead: bool = False) -> None:
        """Count the laps each pair runs within a second; call the best battle once it's lasted."""
        e = self.e
        now: dict[tuple[str, str], int] = {}
        for i, (ahead, car) in enumerate(zip(order, order[1:])):
            if ahead.pit_timer > 0 or car.pit_timer > 0 or ahead.dist - car.dist >= 0.5:
                continue
            gap = (ahead.dist - car.dist) * car.lap_target
            if gap < BATTLE_GAP_S:
                key = (ahead.code, car.code)
                now[key] = self.close.get(key, 0) + 1
        self.close = now
        mine = self._player_teams()
        best = None
        for (a, b), n in now.items():
            if n < BATTLE_LAPS or self.last_lap - self.battle_said.get((a, b), -99) < BATTLE_REPEAT:
                continue
            ahead, car = e.car(a), e.car(b)
            p = e.position(ahead)
            if skip_lead and p == 1:
                continue
            score = (25 - p) + (15 if ahead.driver.team in mine or car.driver.team in mine else 0)
            if best is None or score > best[0]:
                best = (score, ahead, car, n, p)
        if best is None:
            return
        _, ahead, car, n, p = best
        if p > 10 and ahead.driver.team not in mine and car.driver.team not in mine:
            return
        self.battle_said[(ahead.code, car.code)] = self.last_lap
        gap = max(0.1, (ahead.dist - car.dist) * car.lap_target)
        self.say("battle-lead" if p == 1 else "battle", [ahead, car], tone="big" if p == 1 else "normal",
                 n=n, p=p, gap=f"{gap:.1f}")

    # ---------------------------------------------------------- the flag
    def on_finish(self, car: "Car") -> None:
        e = self.e
        p = sum(1 for c in e.cars if c.finished and c.finish_t <= car.finish_t)
        if p == 1:
            if car.grid >= 6:
                self.say("win-from-back", [car], tone="big", g=car.grid)
            else:
                self.say("win", [car], tone="big")
        elif p == 2:
            self.say("second", [car])
        elif p == 3:
            self.say("third", [car])
        elif car.driver.team in self._player_teams():
            self.say("player-finish", [car], p=p)
