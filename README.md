# Pit Wall: F1 strategy simulator


**Live: https://f1-sim-2.onrender.com**

Watch a race and run one team's strategy: pick the circuit and your starting tyres, then call
pit stops and set the pace while the other 18 cars race themselves on their own strategies.
The race engine is Python; the browser draws it.

![Race screen: track map, timing tower and your pit wall](screenshot.png)

## Features

* **4 original circuits**, each with its own layout, lap length, tyre wear and overtaking difficulty:
  Harbourline (balanced), Monteverde Street Circuit (very hard to pass, easy on tyres),
  Sandridge Raceway (very high wear, easy to pass), Alderwood Park (high wear, hard to pass).
* **Starting tyres**: start each of your cars on Soft, Medium or Hard, or let the model choose.
  The two-compound rule still applies (+10 s if you only use one).
* **Weather**: dry, wet or changeable races. Rain comes and goes, the track soaks and dries out
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
  track.py      the 4 circuits: geometry, speed profile, sectors, DRS zones, tyre wear, overtaking
  tyres.py      compounds: pace offset, degradation, wear cliff; pace modes
  strategy.py   pit strategy optimiser (used by AI teams and for "model's best plan")
  engine.py     the race: lap times, fuel, traffic, overtakes, pit stops, VSC, DNFs, starting tyres
  grid.py       default (made-up) teams and drivers
server/     FastAPI app: serves web/, streams the race over a WebSocket, accounts + database
  repo.py       the only code that touches the database (every query scoped to its owner)
  cli.py        admin commands: list-users, reset-password
web/        HTML/CSS/JS front end (canvas track map, timing tower, Chart.js lap chart)
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
pytest -q                          # 90 tests: engine, circuits, weather, accounts, security
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
404 as a missing one. Tests in `tests/test_auth.py` try SQL injection, forged and expired
tokens, cross-user reads and deletes, and role escalation.

## Next

1. Real data: load historical sessions with FastF1 and fit tyre degradation and fuel effect per
   circuit with scikit-learn, replacing the hand-tuned numbers in `tyres.py` and `track.py`.
2. Alembic migrations before the next database schema change.
3. Privacy page and a "forgot password" email flow before sharing the site widely.
