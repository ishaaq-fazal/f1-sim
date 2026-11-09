# Pit Wall: F1 strategy simulator

Watch a race and run one team's strategy: call pit stops, pick tyres and set the pace,
while the other 19 cars race themselves. The race engine is Python; the browser draws it.

```
sim/        pure-Python race engine (no web code)
  track.py      circuit geometry, speed profile, sectors, DRS zones
  tyres.py      compounds: pace offset, degradation, wear cliff; pace modes
  strategy.py   pit strategy optimiser (used by AI teams and for "model's best plan")
  engine.py     the race: lap times, fuel, traffic, overtakes, pit stops, VSC, DNFs
  grid.py       default (made-up) teams and drivers
server/     FastAPI app: serves web/, streams the race over a WebSocket
web/        HTML/CSS/JS front end (canvas track map, timing tower, Chart.js lap chart)
tests/      pytest: engine behaviour + server security checks
```

## Run it

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env
uvicorn server.main:app --reload
```
Open http://localhost:8000, pick a team, press **Lights out**. Use 10x-50x to speed things up.
actual URL: https://f1-sim-2.onrender.com

## Checks

```bash
pytest -q                          # engine + security tests
pip-audit -r requirements.txt      # known-vulnerability scan of pinned deps
```

## How the race model works

Each lap a car's target time is:
`reference lap + car/driver pace + tyre delta(wear) + fuel (0.032 s/kg) + pace mode + noise`,
plus a standing-start loss on lap 1, rare mistakes, and x1.4 under the VSC.
Cars move round the lap along a speed profile (slow in corners, fast on straights).
A car that catches the one ahead is held at ~0.2 s unless it wins an overtake roll, which
gets likelier with a bigger pace gap, DRS, and better racecraft.
A pit stop costs ~21.5 s (about half under the VSC). Using only one compound earns +10 s.

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

Argon2id password hashes, short-lived JWT access tokens plus refresh tokens in HttpOnly,
SameSite=Strict cookies, revoked on logout or role change. Roles: viewer (watch only),
principal (run races, command own team), admin (manage roles; the first account).
All database access goes through `server/repo.py`, which scopes every race query to its
owner and uses parameterised SQLModel queries only. Another user's race returns the same
404 as a missing one. Tests in `tests/test_auth.py` try SQL injection, forged and expired
tokens, cross-user reads and deletes, and role escalation.

## Next

1. Real data: load historical sessions with FastF1, fit tyre degradation and fuel effect per
   circuit with scikit-learn, and use them in place of the hand-tuned numbers in `tyres.py`.
