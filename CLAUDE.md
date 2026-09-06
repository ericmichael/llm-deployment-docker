# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AI Starter Kit — a Django 5.0+ web application for AI Engineering projects. It integrates with LLMs through a LiteLLM proxy and includes student/course management, WebSocket support, and Azure deployment automation.

**Stack**: Django (ASGI/Daphne), LiteLLM proxy, PostgreSQL, Stimulus.js, TailwindCSS, Webpack, Gunicorn+UvicornWorker.

## Build & Run Commands

```bash
# Local development (unified app at :18000; LiteLLM is mounted at /v1 and /litellm)
docker compose up --build

# Run tests
python manage.py test

# Run specific test modules
python manage.py test chat.tests.unit
python manage.py test chat.tests.integration

# Tests need Postgres (see .github/workflows/run-tests.yml for the CI setup):
DATABASE_URL=postgres://postgres:postgres@localhost:5432/aistarterkit SECRET_KEY=x ENVIRONMENT=test python manage.py test

# Build JS bundle (Webpack)
npm run build

# Django management
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py import_students COURSE_CODE path/to/csv.csv
python manage.py import_tas COURSE_CODE path/to/tas.csv
python manage.py sync_litellm_keys [--dry-run]   # re-push budgets/limits to all keys (also runs at boot)
python manage.py reset_litellm_spend --user EMAIL | --course CODE | --all   # zero current-month spend counters
python manage.py revoke_unenrolled_keys [--dry-run]   # sweep keys held by users with no active enrollment
# fix_litellm_keys / harden_litellm_keys are deprecated aliases for sync_litellm_keys
```

## Architecture

### Unified ASGI Router (`aistarterkit/asgi.py`)

A single Gunicorn process serves both Django and LiteLLM via Starlette mounts:
- `/v1/*` → LiteLLM proxy (OpenAI-compatible API)
- `/litellm/*` → LiteLLM management API
- `/v1/realtime`, `/realtime` → LiteLLM WebSocket
- `/` → Django ASGI application

### Database Schema Isolation

Django and LiteLLM share one PostgreSQL database but use separate schemas. LiteLLM tables live in the `litellm` schema (`LITELLM_DB_SCHEMA=litellm`) and use Prisma for migrations. Django uses the default schema.

### Key Modules

- **`aistarterkit/`** — Django project config, settings, ASGI/WSGI entrypoints, URL routing
- **`chat/`** — Main app: CustomUser (email-based auth, no username), Course, Enrollment models; admin with CSV import/export; LiteLLM virtual key generation per user
- **`config/litellm-config.yaml`** — LLM model definitions routing to Azure OpenAI. Every deployment sets `model_info.base_model` (a key in LiteLLM's price map); `asgi.py` additionally registers the deployment name as a price alias at startup because LiteLLM's realtime cost path ignores `base_model` (without it `gpt-realtime` sessions log $0 and bypass budgets)
- **`assets/js/`** — Webpack entry point + Stimulus controllers (`clipboard`, `reveal`, plus `tailwindcss-stimulus-components`: alert, tabs, toggle); bundle.js is generated (not committed). Styling is Tailwind v3 compiled by webpack (`tailwind.config.js` scans templates/JS/forms; `assets/css/tailwind.css` → `assets/css/app.css`, generated, not committed). Run `npm run build` after adding new Tailwind classes

### LiteLLM Virtual Keys

Per-user API keys are provisioned by `chat/litellm_keys.ensure_key()` on visits to the settings page (row-locked per user), stored in `CustomUser.litellm_key`, `litellm_key_id`, `litellm_key_expires`. Keys carry rate/expiry limits (`LITELLM_KEY_*`) and a spend cap resolved by `litellm_keys.effective_budget()`: `CustomUser.monthly_budget` → budget of the active course they're a student in (`Course.monthly_budget`) → global `LITELLM_KEY_MAX_BUDGET`; `0` means unlimited, blank means inherit. Budgets are per calendar month (`LITELLM_KEY_BUDGET_DURATION=1mo`; LiteLLM resets every key on the 1st at midnight in `litellm_settings.timezone`, set to America/Chicago in `config/litellm-config.yaml`). Staff can zero a counter early via "Reset usage" (per student on the roster, per course on the course page, everyone on the usage dashboard) or `manage.py reset_litellm_spend --user|--course|--all`; spend history is unaffected. Budgets are editable on the course pages and in admin; changes re-sync keys through `chat/signals.py`, and `manage.py sync_litellm_keys` (run at every container start by `start.sh`) re-pushes attribution + limits to every key. Keys are renewed automatically once expired, and users can rotate a leaked key with "Regenerate key" on the settings page (`litellm_keys.regenerate_key`: delete + re-issue carrying the current-period spend across, since LiteLLM's `/key/regenerate` is enterprise-only).

### Course Teams

Each `Course` maps to a LiteLLM team (`Course.litellm_team_id`, created/updated by `litellm_keys.ensure_team()` from the Course save signal and by `sync_litellm_keys`). The team carries the course-wide monthly cap (`Course.total_budget`, enforced across all member keys) and the model allowlist (`Course.allowed_models`, empty = all; `LITELLM_KEY_DEFAULT_MODELS` env sets the fallback). Member keys are generated with `team_id` + `models` and re-synced when either changes. Deleting a course deletes its team (and its keys at the proxy). The usage dashboard reads LiteLLM's aggregated daily table (`/user/daily/activity`) rather than raw `SpendLogs`. `chat/signals.py` revokes keys whenever an enrollment is deleted (any path, incl. admin and course-delete cascade) or a course is deactivated, unless the user is staff or still actively enrolled elsewhere.

### Health Check (`/health/`)

Checks database connectivity (`SELECT 1`) and LiteLLM liveliness. Returns 200 for healthy/degraded, 503 for unhealthy. Used by Docker HEALTHCHECK and Azure probes.

### Container Startup

`entrypoint.sh` waits for PostgreSQL, runs Prisma migrations (LiteLLM), Django migrations, and collectstatic. `start.sh` launches Gunicorn with 960s worker timeout and polls `/health/` before tailing logs.

## Testing

- **Unit tests** (`chat/tests/unit/`): service/key-lifecycle branching; LiteLLM HTTP calls are mocked at the `chat.litellm_keys` boundary
- **Integration tests** (`chat/tests/integration/`): Django TestCase for auth, Easy Auth middleware, course views, health check
- **End-to-end tests** (`chat/tests/e2e/`, `python manage.py test chat.tests.e2e`): boots the real unified ASGI app (Django + embedded LiteLLM) on a uvicorn thread against the Django test DB (`harness.py` pushes LiteLLM's Prisma schema into it) and exercises key/team/budget/allowlist/spend flows over HTTP. Nothing LiteLLM-side is mocked. LiteLLM→Azure HTTP is recorded by VCR into `chat/tests/fixtures/cassettes/` (host + credentials scrubbed; replayed on later runs; delete a cassette to re-record, which needs `AZURE_OPENAI_*` in `.env`). The realtime WebSocket test can't be recorded and runs live when `AZURE_OPENAI_API_KEY` is set. Needs `prisma generate` once in the venv (`prisma generate --schema $(python -c "import pathlib,litellm.proxy;print(pathlib.Path(litellm.proxy.__file__).parent/'schema.prisma')")`). `TEST_RUNNER` is `chat.tests.runner.StackAwareRunner`, which stops the stack before the test DB is dropped.

## Deployment

- **Local**: Docker Compose with PostgreSQL and hot-reload (`Dockerfile.dev`)
- **Production**: `Dockerfile` (Python 3.12 + Node 20 base) → Azure Container Registry → Azure App Service
- **CI/CD**: GitHub Actions — PRs run tests (`run-tests.yml`), pushes to main build and push the image tagged `:latest` and `:<sha>` (`deploy-azure-wappserv.yaml`); the App Service is repointed to the SHA tag only if `AZURE_WEBAPP_PUBLISH_PROFILE` + `AZURE_WEBAPP_NAME` secrets are set, otherwise deploy with `python rocketship.py deploy`
- **Azure limit**: the App Service front door times out non-streaming responses at ~230s regardless of the 900s proxy/Gunicorn timeouts — long completions must be streamed

## Environment Variables

Key settings are driven by env vars. There are two files and they are not
interchangeable: `.env` is local development only (docker compose, `manage.py`,
`settings.py`'s `load_dotenv()`), and `.env.deploy` is read only by
`rocketship.py` and holds the production settings plus the deploy credentials.
Never put a production `DATABASE_URL` in `.env` — `settings.py` loads it, so a
bare `manage.py` run would connect to Azure. See `docs/DEPLOYMENT.md`.

- `DATABASE_URL` — PostgreSQL connection string
- `LITELLM_MASTER_KEY` — authenticates both the proxy's own admin API and Django's
  calls into it (`litellm_keys.master_headers()`)
- `LITELLM_PROXY_BASE_URL` — LiteLLM management endpoint used by Django (default `http://localhost:8000/litellm`)
- `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT` — Azure OpenAI credentials
- `ENVIRONMENT` — Controls DEBUG mode and security settings
- `EASYAUTH_ENABLED` — Defaults to on when `WEBSITE_HOSTNAME` is set; set to `false` on any App Service that does not have Authentication turned on (otherwise the `X-MS-CLIENT-PRINCIPAL-NAME` header is client-controlled)
- `LITELLM_PRISMA_ACCEPT_DATA_LOSS` — `entrypoint.sh` runs `prisma db push` without `--accept-data-loss`; set `true` for one boot to apply a LiteLLM schema change that drops columns
- `GUNICORN_MAX_REQUESTS` — Worker recycling, off by default (a recycle restarts the embedded LiteLLM proxy)
