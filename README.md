# ⚔️ QuestLog — gamified to-do web app

A small web app where every finished task earns you points, streaks and badges.
Finish on time for a bonus, but every postponement permanently shrinks the reward.

## Gamification rules

| Event | Effect on points |
|---|---|
| Base points by difficulty | Easy 10 · Normal 20 · Hard 40 |
| Finished before the deadline | base × 1.25 (on-time bonus) |
| Finished after the deadline | base × 0.5 (late penalty) |
| Each postpone (+1 day) | base × 0.8 per postpone (1 postpone = 80 %, 2 = 64 %, …) |
| Daily streak | +1 pt per streak day, up to +10, added to every completion |
| Level | `floor(sqrt(points / 50)) + 1`, with a progress bar to the next level |
| Badges | First Steps, Punctual Pro (10 on-time), Week Warrior (7-day streak), Unstoppable (30-day), Century Club (100 pts), Quest Legend (1,000 pts), Resilient (finish a postponed task) |

Example: a Normal task (20) finished on time with a 3-day streak = 20 × 1.25 + 3 = **28 pts**.
The same task postponed twice and then finished "on time" = 20 × 0.64 × 1.25 + 3 = **19 pts**.

## Quickstart (local)

```bash
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py            # serves on http://127.0.0.1:5000
```

Open http://127.0.0.1:5000, sign up, and add your first quest.
Configuration via environment variables: `PORT`, `QUESTLOG_DB` (SQLite file path),
`SECRET_KEY` (Flask session signing key — set this explicitly in production).

## Project layout

```
app.py                  # Flask routes, auth, gamification rules, schema
templates/              # base, index (dashboard), login, register
static/style.css        # dark theme
smoke_test.py           # end-to-end HTTP test (register → add → postpone → complete → points)
requirements.txt        # Flask (+ gunicorn for deployment)
```

Run the smoke test with the app running: `python3 smoke_test.py` (expects port 5057,
set via `PORT`).

## Database — my proposal

**SQLite (what the app uses now).** For a personal or small-team to-do app it is the
right default: zero setup, ACID transactions, one file you can back up by copying it.
The schema lives in `SCHEMA` in `app.py` (tables: `users`, `sessions`, `tasks`,
`user_badges`); the file path is the `QUESTLOG_DB` env var, default `app.db`.

**When to switch to PostgreSQL:** if you ever run multiple app servers behind a load
balancer, or want managed backups/point-in-time recovery. The switch is small — swap
`sqlite3` for `psycopg` (plus SQLAlchemy if you want an ORM), keep the same schema,
and point the connection string at Postgres. On hosted platforms (below) most offer a
managed Postgres add-on for exactly this.

## Login mechanism — my proposal

What the app implements (no extra dependencies):

- **Passwords:** PBKDF2-HMAC-SHA256, 600,000 iterations, per-user random 16-byte salt,
  stored as `pbkdf2_sha256$iterations$salt$hash`; verified with a constant-time compare.
  Never stored or logged in plain text.
- **Sessions:** random 256-bit token in an `HttpOnly`, `SameSite=Lax` cookie (set
  `Secure` automatically when served over HTTPS); only the SHA-256 hash of the token
  is stored server-side, with a 30-day expiry, so logout truly revokes access.
- **CSRF:** every authenticated POST must carry a per-session token rendered into the
  forms; requests without it get HTTP 400.

For a public deployment add: HTTPS (Let's Encrypt), `Secure` cookies, and simple
rate limiting on `/login` (e.g. 5 attempts/minute per IP). If you later want social
login, OAuth2/OIDC (Google, GitHub) is the standard route and slots in next to the
password flow.

## Hosting — what I can and can't do here

This sandbox **cannot expose anything to the public internet**, so I ran the app
loopback-only (`127.0.0.1`) for verification — it is not reachable from outside.
To put it online yourself, any of these works:

1. **A cheap VPS (Hetzner, DigitalOcean, etc.)** — most control, ~$4–5/month:
   ```bash
   pip install -r requirements.txt
   SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))") \
     gunicorn --bind 127.0.0.1:8000 app:app   # behind nginx + Let's Encrypt HTTPS
   ```
   Serve over HTTPS with nginx as a reverse proxy and a free Let's Encrypt cert.
   Keep SQLite on disk, or attach a Postgres add-on.
2. **PaaS (Render, Railway, Fly.io, PythonAnywhere)** — upload/push this folder,
   set `SECRET_KEY` in the environment, start command `gunicorn app:app` (a
   `Procfile` with `web: gunicorn app:app` makes this automatic on Render/Heroku-style
   platforms). These platforms give you HTTPS for free and can host Postgres too.

Either way: never commit `app.db` or your `SECRET_KEY`; both are runtime artifacts.

## Security notes

- Password hashes and session tokens are never logged or displayed.
- All task queries are scoped by `user_id`, so users can only see their own data.
- Jinja2 auto-escapes all template output (XSS-safe); forms are CSRF-protected.
- For anything internet-facing, run behind HTTPS and a production WSGI server
  (gunicorn), never `app.run` with the development server.
