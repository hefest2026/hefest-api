# Hefest API

Backend for **Hefest** — a school events & notification centre. Organizers
publish events with a capacity; students register and are either **confirmed**
or placed on a **FIFO waitlist**; when a confirmed seat is freed the next
waitlisted student is promoted automatically. Registration changes emit domain
events that a **separate worker process** turns into email notifications.

Built with FastAPI, Tortoise ORM (asyncpg/PostgreSQL), Redis, and a
transactional-outbox notification pipeline.

Full design docs (architecture, data model, auth, transactions, the
notification pipeline, and the grading-criteria mapping) live in
[`../hefest-docs`](../hefest-docs) and render as a MkDocs site.

---

## Two processes: producer and consumer

Hefest is a single codebase that runs as **two independent processes**. This
separation is deliberate — the HTTP request never blocks on email delivery.

| Process | Command | Role |
|---|---|---|
| **API** (producer) | `uv run uvicorn hefest.main:app --port 8000` | Serves REST endpoints. On a successful write it inserts notification job rows in the **same DB transaction** as the business change (transactional outbox). |
| **Worker** (consumer) | `uv run python -m hefest.worker` | A separate long-running process that claims job rows (`FOR UPDATE SKIP LOCKED`), sends email via SMTP, and advances each row's state. Never shares the HTTP request thread. |

**The queue is the `notification_jobs` table**, not an external broker. Because
the job row and the registration are written in one transaction, a crash can
never leave one without the other (no dual-write race). The worker is woken by
Postgres `LISTEN/NOTIFY` and falls back to polling. See
[`hefest-docs/architecture/pipeline.md`](../hefest-docs/docs/architecture/pipeline.md).

Domain event types that flow end-to-end (API → outbox → worker → email):
`RegistrationConfirmed`, `RegistrationWaitlisted`, `WaitlistPromoted`,
`RegistrationCancelled`, `EventCancelled` (bulk fan-out), plus `EmailVerify`.

Reliability: each job tracks `status` (`pending` / `processing` / `completed` /
`failed`), `attempts`, `next_attempt_at` (exponential backoff), and
`last_error`. Idempotency is enforced by a unique `idempotency_key` per
`(entity, event_type)`, so a retry can never send a duplicate notification.

---

## Quickstart (local)

Requires [`uv`](https://docs.astral.sh/uv/) and a reachable PostgreSQL + Redis
(and an SMTP sink such as [Mailpit](https://mailpit.axllent.org/) for the
worker). The easiest path is the compose stack in
[`../hefest-compose`](../hefest-compose), which wires all of this up:

```bash
cd ../hefest-compose
docker compose up          # postgres, redis, mailpit, migrate, api, worker, frontend
```

To run the API and worker directly against your own Postgres/Redis:

```bash
uv sync                                                   # install deps into .venv

# 1. Apply migrations (native Tortoise CLI — never hand-write migrations)
uv run tortoise -c hefest.config.TORTOISE_ORM migrate

# 2. (optional) Seed demo accounts
PYTHONPATH=. uv run python scripts/seed.py

# 3. Start the API (producer) — terminal 1
uv run uvicorn hefest.main:app --host 0.0.0.0 --port 8000 --reload

# 4. Start the worker (consumer) — terminal 2
uv run python -m hefest.worker
```

Interactive API docs: <http://localhost:8000/docs>.
Liveness/readiness: `GET /health`, `GET /ready`.

### Migrations

Migrations are generated with the native Tortoise CLI and **never hand-written**.
After editing a model in `hefest/models/`, generate then apply:

```bash
uv run tortoise -c hefest.config.TORTOISE_ORM makemigrations   # create from model changes
uv run tortoise -c hefest.config.TORTOISE_ORM migrate          # apply pending migrations
```

---

## Configuration

All settings are environment variables prefixed `HEFEST_` (or a `.env` file).
Defaults suit local development; override in production. Never commit secrets.

| Variable | Default | Purpose |
|---|---|---|
| `HEFEST_DB_URL` | `asyncpg://hefest:hefest@localhost:5432/hefest_db` | PostgreSQL DSN (asyncpg scheme) |
| `HEFEST_REDIS_URL` | `redis://localhost:6379` | Redis (rate limiting, caching) |
| `HEFEST_JWT_SECRET` | `change-me-in-production` | HS256 signing key — **must** be set in prod |
| `HEFEST_JWT_EXPIRE_MINUTES` | `15` | Access-token lifetime |
| `HEFEST_REFRESH_TOKEN_EXPIRE_DAYS` | `14` | Refresh-token lifetime |
| `HEFEST_SMTP_HOST` / `HEFEST_SMTP_PORT` | `localhost` / `1025` | SMTP endpoint (Mailpit in dev, Resend in prod) |
| `HEFEST_SMTP_USERNAME` / `HEFEST_SMTP_PASSWORD` | `""` | SMTP auth (set in prod) |
| `HEFEST_SMTP_FROM` | `noreply@hefest.local` | Sender address |
| `HEFEST_WORKER_MAX_ATTEMPTS` | `3` | Max delivery attempts before a job is marked `failed` |
| `HEFEST_WORKER_BACKOFF_BASE_SECONDS` | `30` | Base for exponential retry backoff |
| `HEFEST_GOOGLE_CLIENT_ID` / `_SECRET` / `_REDIRECT_URI` | `""` | Google SSO (optional; all three required to enable) |
| `HEFEST_MICROSOFT_CLIENT_ID` / `_SECRET` / `_TENANT` / `_REDIRECT_URI` | `""` | Microsoft SSO (optional; all four required to enable) |
| `HEFEST_FRONTEND_OAUTH_SUCCESS_URL` | `""` | Web SSO landing URL (access token in fragment, refresh in cookie) |
| `HEFEST_MOBILE_OAUTH_SUCCESS_URL` | `hefestmobile://auth/callback` | Native SSO deeplink (both tokens in fragment) |

See `hefest/config.py` for the complete list.

---

## REST surface

Authentication is JWT (access + refresh); protected routes require a valid
access token. Every sensitive read/write checks **role and ownership**.

| Area | Endpoints |
|---|---|
| Auth | `POST /register`, `POST /login`, `POST /auth/refresh`, `POST /auth/logout`, `POST /auth/logout-all`, `POST /auth/change-password`, `POST /auth/verify-email` |
| Profile | `GET /users/me`, `PATCH /users/me` |
| SSO | `GET /auth/providers`, `GET /auth/{google,microsoft}/login?client=web\|mobile`, `.../callback` |
| Events | `POST /events`, `GET /events`, `GET /events/{id}`, `PUT /events/{id}`, `POST /events/{id}/publish`, `POST /events/{id}/cancel` |
| Registrations | `POST /events/{id}/registrations`, `DELETE /registrations/{id}`, `GET /registrations/me`, `GET /events/{id}/registrations` (organizer), `GET /events/{id}/waitlist` (organizer) |
| Notification jobs | `GET /notification-jobs`, `GET /notification-jobs/{id}` |
| Stats / devices | `GET /stats`, `POST /devices`, `POST /devices/unregister` |
| Operational | `GET /health`, `GET /ready`, `GET /metrics` |

Students see only published events and only their own registrations; organizers
see and manage only their own events. Passwords are stored with bcrypt (cost 12).

---

## Testing

```bash
uv run pytest                       # full suite
uv run pytest tests/test_services_registration.py -q
```

Unit tests mock the ORM and need no database. Integration tests (the
`db`-fixture ones) spin up an ephemeral `postgres:16-alpine` via testcontainers
and are **skipped automatically when Docker is unavailable**, so the suite runs
everywhere and exercises real SQL where Docker exists.

Quality gates:

```bash
uv run ty check          # static type checking
uv run ruff check .      # lint
uv run ruff format .     # format
```

---

## Project layout

```
hefest/
  main.py            # FastAPI app, lifespan, /health, /ready, /metrics
  config.py          # HEFEST_* settings + Tortoise ORM config (API + worker pools)
  models/            # Tortoise models (user, event, registration, notification_job, ...)
  schemas/           # Pydantic request/response schemas
  services/          # business logic (auth, event, registration, stats, device)
  routers/           # HTTP endpoints (auto-wired in main.py)
  middleware/        # rate limiting
  worker/            # consumer: __main__, consumer, claim, mailer, templates, ...
migrations/          # Tortoise migrations (generated, never hand-written)
scripts/             # seed.py, seed_events.py, ci_check_migrations.py
tests/               # pytest unit + integration suites
```

`services/` is the producer side of the outbox; `worker/` is the consumer — the
two never share a process. That boundary is the core of the architecture.
