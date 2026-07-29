# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

**Project City** — a single-user Flask app for tracking projects, with two views:
a CRUD project list (`/`) and a "city" visualizer (`/city`) where projects render
as buildings. Data lives in **Supabase Postgres**; the app is deployed on
**Render** (free plan). There is no build step and no frontend framework — the
two big HTML files are served largely as-is, with a right-side nav injected at
serve time so the source files stay pristine.

## Layout

- `main/app.py` — the entire backend (Flask). All routes, auth, and DB access.
- `main/list.html`, `main/city.html` — the two views (large, hand-authored).
- `main/migrate_sqlite_to_pg.py` — one-off SQLite→Postgres migration helper.
- `render.yaml` — Render service definition (build/start commands, env vars).
- `.github/workflows/keepalive.yml` — pings `/healthz` to keep Render + Supabase awake.
- `context/skyscraper.md` — design notes.

## Running locally

```bash
pip install -r requirements.txt
DATABASE_URL=<supabase-session-pooler-uri> python main/app.py   # http://127.0.0.1:5000
```

- **`DATABASE_URL` is required in every environment** — `app.py` reads it with no
  fallback and crashes on boot if it's unset. Use the Supabase **session pooler**
  URI (port 5432 on `*.pooler.supabase.com`, the IPv4 host); the direct
  `db.*.supabase.co` host is IPv6-only and Render has no IPv6 egress.
- `PW_HASH` (scrypt) gates the single-user login. Unset means login always fails.
- Production runs under gunicorn, not `app.run`: `gunicorn app:app --chdir main`.

## Verifying a change

There is no test suite yet. At minimum, before committing, confirm the app still
imports and the key routes are intact:

```bash
python -c "import ast; ast.parse(open('main/app.py').read())"   # syntax check (no DB needed)
```

With a `DATABASE_URL` available you can boot it and hit `/healthz` (returns
`{"ok": true}` after one `SELECT 1`) — the only unauthenticated route.

## Conventions

- **Never commit the database.** `*.db` is gitignored; live data is in Supabase.
- **Never hand-set `SECRET_KEY`** — Render generates and persists it (see `render.yaml`).
- Keep secrets (`DATABASE_URL`, `PW_HASH`) out of git; they're set in the Render
  dashboard with `sync: false`.
- Match the existing style in `app.py`: terse, comment-heavy where behavior is
  non-obvious (pooler quirks, keepalive rationale, N+1 fixes). Don't reformat
  wholesale.

## Deploy

Push to the branch Render watches (`main`) → Render auto-builds from `render.yaml`.
Claude cannot deploy directly; the role here is verify-then-merge. After deploy,
`/healthz` should return 200.
