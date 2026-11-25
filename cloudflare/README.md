# QuestLog on Cloudflare (Workers + D1)

This directory ports QuestLog to **Cloudflare Workers (Python)** with a **D1** database,
so it runs serverless on Cloudflare's global network with no VPS to manage.
The classic Flask + SQLite version stays in the repo root (`app.py`) for VPS/PaaS deploys.

The app code is FastAPI (ASGI) in `src/app.py`; `src/worker.py` exposes it to Workers via
`workers.asgi.entrypoint(app)`. Templates and CSS from the repo root are embedded as Python
data in `src/templates_data.py` (Workers have no filesystem) — regenerate after editing
them with:

```bash
python3 scripts/sync_templates.py
```

The database layer (`src/db.py`) uses the D1 binding when running on Workers and falls back
to a local SQLite file when running locally, so the same code is testable with uvicorn.

## Deploy steps (run on your machine, in this directory)

```bash
# 1. install uv + the Python Workers CLI
pip install uv
uvx --from workers-py pywrangler --help

# 2. install dependencies
uv sync

# 3. create the D1 database and put its id into wrangler.jsonc (database_id)
uv run pywrangler d1 create questlog

# 4. apply the schema (migrations/0001_schema.sql)
uv run pywrangler d1 migrations apply DB --local    # test locally first
uv run pywrangler d1 migrations apply DB --remote   # then production

# 5. local dev (same code, local SQLite file app.db)
uv run pywrangler dev            # or: cd src && python3 worker.py  (uvicorn on :8787)

# 6. deploy
uv run pywrangler deploy
```

After deploy you get a `https://questlog.<your-subdomain>.workers.dev` URL.
To use your own domain: Cloudflare dashboard → Workers & Pages → questlog → Settings →
Triggers → Custom Domains (free HTTPS included). If you put the domain on Cloudflare DNS
with the orange cloud enabled, you additionally get CDN/WAF/DDoS protection in front.

## Notes

- Sessions live in D1 (`sessions` table), same as the Flask version; password hashing is
  still PBKDF2-HMAC-SHA256 with per-user salt; forms are CSRF-protected.
- No `SECRET_KEY` is needed: flash messages are passed via a redirect query parameter and
  session cookies are random tokens (only their SHA-256 hash is stored).
- D1 is SQLite-compatible, so the schema is identical to the Flask version; the worker
  also self-creates the schema on first request if the tables are missing.
