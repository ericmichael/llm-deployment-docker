# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build/Test Commands

```bash
# Frontend
npm install && npm run build

# Django server (requires LiteLLM proxy running)
LITELLM_BASE_URL=http://127.0.0.1:4000 python manage.py runserver

# LiteLLM proxy (run in separate terminal)
litellm --config config/litellm-config.yaml --host 0.0.0.0 --port 4000

# Database
python manage.py migrate

# Tests
python manage.py test                                              # All tests
python manage.py test chat.tests.unit                              # Unit tests only
python manage.py test chat.tests.unit.test_api                     # Specific module
python manage.py test chat.tests.unit.test_api.TestAPI.test_auth   # Specific test case

# Integration tests (requires running LiteLLM proxy)
LITELLM_BASE_URL=http://127.0.0.1:4000 python manage.py test chat.tests.integration

# Docker (starts Django + PostgreSQL + LiteLLM)
docker-compose up --build

# Bulk import users
python manage.py import_students CSCI-4380-01 students.csv
python manage.py import_tas CSCI-4380-01 tas.csv
```

## Architecture Overview

This is a Django-based AI Starter Kit that provides authenticated access to LLM APIs through a stateless LiteLLM proxy. Designed for educational settings with course-based access control.

### Request Flow

```
Client (Bearer token) → Django /chat/api/v1/* → LiteLLM Proxy (port 4000) → Azure OpenAI
```

1. Client authenticates with DRF Token (`Authorization: Bearer <token>`)
2. Django validates token and checks active course enrollment
3. Request proxied to LiteLLM with `LITELLM_SERVICE_KEY`
4. Response streamed back (supports SSE streaming via `stream: true`)

### Key Components

- **`chat/views.py`**: `litellm_proxy_catchall()` - async HTTP proxy to LiteLLM, handles streaming/non-streaming
- **`chat/consumers.py`**: `RealtimeProxyConsumer` - WebSocket proxy for realtime API (`/ws/v1/realtime`)
- **`chat/models.py`**: `CustomUser` (email-based auth), `Course`, `Enrollment` (student/TA roles)
- **`aistarterkit/settings.py`**: Django config, LiteLLM settings, database config
- **`config/litellm-config.yaml`**: LiteLLM model routing configuration

### Authentication & Authorization

- Email-based user model (`CustomUser` replaces username with email)
- DRF TokenAuthentication for API access
- Course enrollment required for API access (superusers bypass)
- Students limited to one course enrollment; TAs can be in multiple courses
- Inactive courses deny API access to all enrolled students

### WebSocket Support

- Endpoint: `/ws/v1/realtime?model=gpt-realtime&token=<auth_token>`
- Uses Django Channels with Daphne ASGI server
- Bidirectional proxy to LiteLLM realtime endpoint

## Code Style

- **Python**: PEP 8, Django conventions
- **Import order**: stdlib → third-party → Django → local apps
- **Type hints**: Required for function parameters and returns
- **Naming**: snake_case (Python), camelCase (JavaScript)
- **Tests**: unittest with VCR for API mocking (`chat/tests/unit/vcr_config.py`)
- **Django**: MTV pattern (Model-Template-View)

## Key Environment Variables

```bash
# Required
LITELLM_SERVICE_KEY=          # Shared auth key for Django↔LiteLLM
LITELLM_BASE_URL=http://127.0.0.1:4000
AZURE_OPENAI_ENDPOINT=        # Azure OpenAI resource URL
AZURE_OPENAI_API_KEY=         # Azure OpenAI key

# Optional
LITELLM_DEFAULT_MODEL=gpt-5
LITELLM_MODEL_LIST=gpt-5,gpt-5.1,gpt-4o-mini  # Available models
DATABASE_URL=postgres://...    # PostgreSQL (defaults to SQLite)
```

## Deployment

```bash
python rocketship.py init    # Create config/deploy.yml template
python rocketship.py setup   # Deploy to Azure (builds image, pushes secrets)
```

Container runs both Django (Gunicorn/Uvicorn on port 8000) and LiteLLM (port 4000) via `start.sh`.
