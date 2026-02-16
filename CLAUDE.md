# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AI Starter Kit — a Django 5.0+ web application for AI Engineering projects. It integrates with LLMs through a LiteLLM proxy and includes student/course management, WebSocket support, and Azure deployment automation.

**Stack**: Django (ASGI/Daphne), LiteLLM proxy, PostgreSQL, Stimulus.js, TailwindCSS, Webpack, Gunicorn+UvicornWorker.

## Build & Run Commands

```bash
# Local development (Django at :18000, LiteLLM at :14000)
docker compose up --build

# Run tests
python manage.py test

# Run specific test modules
python manage.py test chat.tests.unit
python manage.py test chat.tests.integration

# Run integration tests against live LiteLLM proxy
LITELLM_BASE_URL=http://127.0.0.1:4000 python manage.py test chat.tests.integration

# Build JS bundle (Webpack)
npm run build

# Django management
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py import_students COURSE_CODE path/to/csv.csv
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
- **`config/litellm-config.yaml`** — LLM model definitions routing to Azure OpenAI
- **`assets/js/`** — Stimulus controllers, Webpack entry point; bundle.js is generated (not committed)
- **`patches/`** — Unmerged LiteLLM fixes applied during Docker build

### LiteLLM Virtual Keys

Per-user API keys are generated on first visit to the settings page, stored in `CustomUser.litellm_key` and `litellm_key_id`. Falls back to service key or test key for anonymous requests.

### Health Check (`/health/`)

Checks database connectivity (`SELECT 1`) and LiteLLM liveliness. Returns 200 for healthy/degraded, 503 for unhealthy. Used by Docker HEALTHCHECK and Azure probes.

### Container Startup

`entrypoint.sh` waits for PostgreSQL, runs Prisma migrations (LiteLLM), Django migrations, and collectstatic. `start.sh` launches Gunicorn with 960s worker timeout and polls `/health/` before tailing logs.

## Testing

- **Unit tests** (`chat/tests/unit/`): Use VCR cassettes in `chat/tests/fixtures/` for deterministic API replay (YAML serialization, match on URI/method/path/query/body)
- **Integration tests** (`chat/tests/integration/`): Django TestCase for auth flows and database transactions

## Deployment

- **Local**: Docker Compose with PostgreSQL and hot-reload (`Dockerfile.dev`)
- **Production**: `Dockerfile` multi-stage build → Azure Container Registry → Azure App Service
- **CI/CD**: GitHub Actions — PRs run tests (`run-tests.yml`), pushes to main build and push Docker image (`deploy-azure-wappserv.yaml`)

## Environment Variables

Key settings are driven by env vars (see `.env` for development defaults):
- `DATABASE_URL` — PostgreSQL connection string
- `LITELLM_MASTER_KEY`, `LITELLM_SERVICE_KEY` — LiteLLM authentication
- `LITELLM_BASE_URL` — Proxy endpoint (default `http://127.0.0.1:4000`)
- `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT` — Azure OpenAI credentials
- `ENVIRONMENT` — Controls DEBUG mode and security settings
