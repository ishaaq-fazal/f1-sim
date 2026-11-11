# Pit Wall: F1 strategy simulator

**Live: https://f1-sim-2.onrender.com**

Watch a race and run one team's strategy: pick the circuit and your starting tyres, then call
pit stops and set the pace while the other 18 cars race themselves on their own strategies.
The race engine is Python; the browser draws it.

![Race screen: track map, timing tower and your pit wall](screenshot.png)

## Features

* **10 original circuits**, each with its own layout, lap length, tyre wear and overtaking difficulty:
  Harbourline (balanced), Monteverde Street Circuit (very hard to pass, easy on tyres),
  Sandridge Raceway (very high wear, easy to pass), Alderwood Park (high wear, hard to pass),
  Riverside Ring (long straights, easy to pass), Glacier Pass (longest lap, very low wear),
  Neon Bay Street Circuit (night street race, hard to pass), Highmoor Circuit (very fast, high wear),
  Cedar Vale (short and twisty, almost no passing) and Saltmarsh Coast (two big straights, coarse surface).
* **Starting tyres**: start each of your cars on Soft, Medium or Hard, or let the model choose.
  The two-compound rule still applies (+10 s if you only use one).
* **Main menu, tutorial and sound**: a main menu (Quick race, Season, Career, How to play), a
  one-minute guided tour of the race screen on a paused practice race, and synthesised sound
  (no audio files): an engine note that follows your lead car's speed through the gears, wheel
  guns and the pit limiter when your car stops, start lights and the chequered flag. Sound
  settings live in the menu and are remembered in your browser.
* **Season mode**: a 10-race championship, one round at every circuit, with the same team all
  season. Every car scores F1 points (25-18-15-12-10-8-6-4-2-1 for the top 10), and the
  leaderboard shows drivers' and teams' standings plus the calendar with each round's winner.
  Each round's weather is random (dry 32%, changeable 24%, showers 18%, drying 12%, wet 14%) and fixed in advance, so
  it can't be re-rolled. The pre-race screen shows the forecast and you pick your starting tyres.
  Seasons are saved to your account; only you can see or delete them.
* **Online leagues**: start a league and share its 8-letter invite code; friends join with
  their own team and a nickname. Everyone runs their own 10-round season, every round has the
  same weather for everybody, and a shared table ranks players by the F1 points their team has
  scored (ties on countback). Only members can see a league, other members only ever see your
  nickname (never your email), and the owner can remove players or delete the league.
* **Career mode**: season after season with one team. Win prize money after every round and a
  bigger payout by constructors' position at the end of each season, spend it on four areas of
  car development between races (power unit, aerodynamics, suspension/tyre wear, reliability),
  and manage two driver contracts: renew them when they run out or sign someone from the driver
  market before a season starts. Rival teams develop every off-season, so standing still means
  falling back.
* **Weather**: dry, changeable, showers (short bursts on and off), drying (wet start that dries out) or wet races. Rain comes and goes, the track soaks and dries out
  over time, and a forecast shows the chance of rain. Intermediates and Wets are quick in the wet
  but overheat on a drying track; AI teams switch tyres when the gain pays for the stop. Using a
  wet tyre waives the two-compound rule.
* **Pit wall**: box this lap for any compound, push / normal / conserve pace, and a
  "model's best plan" prediction that updates as the tyres wear.
* **Live timing**: track map with DRS zones and sectors, timing tower (gap/interval, last and
  best laps, tyre and age, stops), lap time chart, race control feed (overtakes, pit stops,
  fastest laps, Virtual Safety Car, retirements).
* **Accounts**: sign up and log in, with your race history saved to your account.

```
sim/        pure-Python race engine (no web code)
  track.py      the 10 circuits: geometry, speed profile, sectors, DRS zones, tyre wear, overtaking
  tyres.py      compounds: pace offset, degradation, wear cliff; pace modes
  strategy.py   pit strategy optimiser (used by AI teams and for "model's best plan")
  engine.py     the race: lap times, fuel, traffic, overtakes, pit stops, VSC, DNFs, starting tyres
  grid.py       default (made-up) teams and drivers
  season.py     season calendar, F1 points and championship standings
  career.py     career mode: budget, car upgrades, driver contracts, rival development
server/     FastAPI app: serves web/, streams the race over a WebSocket, accounts + database
  repo.py       the only code that touches the database (every query scoped to its owner)
  cli.py        admin commands: list-users, reset-password
web/        HTML/CSS/JS front end (canvas track map, timing tower, Chart.js lap chart,
            menu.js main menu + tutorial, sound.js Web Audio sounds)
tests/      pytest: engine, circuits, accounts/authorisation and security checks
```

## Run it

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env
uvicorn server.main:app --reload
```
Open http://localhost:8000, create an account, pick a circuit, team and starting tyres, and
press **Lights out**. Use 10x-50x to speed things up.

Forgot a password? `python -m server.cli reset-password you@example.com`

## Checks

```bash
pytest -q                          # 185 tests: engine, circuits, weather, seasons, leagues, career, login, security
pip-audit -r requirements.txt      # known-vulnerability scan of pinned deps
```

## How the race model works

Each lap a car's target time is:
`reference lap + car/driver pace + tyre delta(wear) + fuel (0.032 s/kg) + pace mode + noise`,
plus a standing-start loss on lap 1, rare mistakes, and x1.4 under the VSC.
Cars move round the lap along a speed profile (slow in corners, fast on straights).
A car that catches the one ahead is held at ~0.2 s unless it wins an overtake roll, which
gets likelier with a bigger pace gap, DRS, and better racecraft.
A pit stop costs 19-23 s depending on the circuit (about half under the VSC). Using only one
compound earns +10 s. Each circuit scales tyre wear per lap and the chance of overtaking, and the
AI teams' strategies (and the "best plan" prediction) are optimised for that circuit.

## Security already in place

* Every WebSocket message is validated by a strict Pydantic model (unknown fields, bad values
  and oversized messages are rejected).
* Server-side authorisation: you can only give orders to your own team's cars.
* WebSocket Origin allowlist, CORS allowlist, per-IP HTTP rate limit, per-connection message
  rate limit, max sockets per IP.
* Strict CSP (same-origin scripts only; Chart.js is vendored in `web/vendor`), nosniff,
  frame-deny, no-referrer. The front end never uses `innerHTML`.
* Generic error responses (details only in server logs); `/docs` only when `APP_ENV=dev`.
* Config in `.env` (gitignored); dependencies pinned and checked with pip-audit.

## Deploy to Render

1. Push this folder to a GitHub repo.
2. In Render: **New > Blueprint**, pick the repo. `render.yaml` creates the web service
   (Docker, always-on "starter" plan) and a Postgres database, generates `SECRET_KEY`,
   and wires `DATABASE_URL`. Render only lets its own services reach the database.
3. When asked for `ALLOWED_ORIGINS`, enter your site's address, e.g.
   `https://pitwall.example.com` (the `onrender.com` address is trusted automatically).
4. Custom domain: Settings > Custom Domains in Render, then add the DNS record it shows at
   your registrar. Render issues the HTTPS certificate.
5. Open the site and create your account first: the first account becomes admin.

Production mode (`APP_ENV=prod`) refuses to start without a 32+ character `SECRET_KEY`,
sends cookies only over HTTPS, adds HSTS, and hides `/docs`. `/healthz` is the health check.
To try the production image locally:
```bash
docker build -t f1-sim .
docker run -p 8080:8080 -e PORT=8080 -e SECRET_KEY=$(python -c "import secrets;print(secrets.token_urlsafe(48))") \
  -e ALLOWED_ORIGINS=http://localhost:8080 -e DATABASE_URL=postgresql://user:pass@host:5432/db f1-sim
```
Run the tests against Postgres with `TEST_DATABASE_URL=postgresql://... pytest -q`.

Known limits: tables are created at startup (add Alembic migrations before changing the
schema with real users), and live races and rate limits live in one server's memory, so run
a single instance until those move to a shared store such as Redis.

## Accounts and data protection

Sign up with an email and a password (10+ characters). Roles:

| Role | Can |
|---|---|
| viewer | watch races, but not give orders |
| principal | run races and command their own team (default for new accounts) |
| admin | everything, plus change other users' roles (the first account created) |

Finished races are saved to your history (circuit, team, result, seed to replay it); only you
can see or delete them.

Argon2id password hashes, short-lived JWT access tokens plus refresh tokens in HttpOnly,
SameSite=Strict cookies, revoked on logout or role change. All database access goes through `server/repo.py`, which scopes every race query to its
owner and uses parameterised SQLModel queries only. Another user's race returns the same
404 as a missing one. Leagues are members-only: a league you aren't in returns 404, joining
one never opens up other members' seasons or race history, and invite-code guesses are rate
limited (`tests/test_leagues.py`). Tests in `tests/test_auth.py` try SQL injection, forged and expired
tokens, cross-user reads and deletes, and role escalation.

## Next

1. Real data: load historical sessions with FastF1 and fit tyre degradation and fuel effect per
   circuit with scikit-learn, replacing the hand-tuned numbers in `tyres.py` and `track.py`.
2. Alembic migrations before the next database schema change.
3. Privacy page and a "forgot password" email flow before sharing the site widely.
