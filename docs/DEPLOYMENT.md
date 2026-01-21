# Deployment Guide

This project deploys to **Azure App Service for Containers** using a combination of the `rocketship.py` helper script and GitHub Actions for CI/CD.

## Overview

```
Local Setup (rocketship.py setup)
        │
        ├── Build & push Docker image to Azure Container Registry
        ├── Push secrets to GitHub Actions
        └── Configure Azure App Service settings

Ongoing Deploys (git push to main)
        │
        └── GitHub Actions builds & pushes new image
                │
                └── Azure App Service pulls new image automatically
```

## Prerequisites

- Docker installed locally
- Azure CLI (`az`) installed and configured
- GitHub personal access token with `repo` scope (set as `GITHUB_TOKEN` env var)
- A `.env` file with your application secrets
- `pynacl` Python package (`pip install pynacl`)

## Configuration

### config/deploy.yml

The deployment configuration lives in `config/deploy.yml`:

```yaml
image: cs4341/playground
registry:
  server: cs4341.azurecr.io
  username: cs4341
  password: ${ROCKETSHIP_REGISTRY_PASSWORD}
service: myapp
github:
  repo: ericmichael/llm-deployment-docker
azure:
  subscription: ${AZURE_SUBSCRIPTION_ID}
  app_service:
    app_name: csci3351
    resource_group: cs-rg-4341-scus
    additional_env:
      SQLITE3_STORAGE_PATH: /home/app/db/sqlite3/
      CHROMADB_STORAGE_PATH: /home/app/db/chromadb/
      DUCKDB_STORAGE_PATH: /home/app/db/duckdb/
      WEBSITES_ENABLE_APP_SERVICE_STORAGE: true
      # ... other settings
```

Environment variables like `${AZURE_SUBSCRIPTION_ID}` are substituted from your `.env` file at runtime.

## rocketship.py

The `rocketship.py` script provides two commands:

### `python rocketship.py init`

Creates a starter `config/deploy.yml` template for new projects.

### `python rocketship.py setup`

Runs the full deployment setup:

1. **Validates prerequisites** - Checks for Docker, Dockerfile, GitHub token, and Azure CLI
2. **Loads and validates config** - Reads `config/deploy.yml` and substitutes environment variables
3. **Docker build & push**:
   - Logs into Azure Container Registry
   - Builds the Docker image locally
   - Pushes to the registry (e.g., `cs4341.azurecr.io/cs4341/playground:latest`)
4. **GitHub secrets**:
   - Encrypts and pushes all `.env` variables as GitHub Actions secrets
   - Pushes registry credentials (`ROCKETSHIP_REGISTRY_*`) for CI/CD
5. **Azure App Service configuration**:
   - Pushes environment variables to Azure App Service via `az webapp config appsettings set`
   - Restarts the App Service to apply changes

## CI/CD Pipeline

After initial setup, the GitHub Actions workflow (`.github/workflows/deploy-azure-wappserv.yaml`) handles ongoing deployments:

1. On push to `main` branch
2. Builds the Docker image using GitHub Actions cache
3. Pushes to Azure Container Registry
4. Azure App Service automatically pulls the new image

## Persistent Storage

Azure App Service for Containers has an **ephemeral filesystem** by default. However, this project maintains database state across restarts using Azure's built-in persistent storage.

### How it works

The key setting in `config/deploy.yml`:

```yaml
WEBSITES_ENABLE_APP_SERVICE_STORAGE: true
```

When this is set to `true`, Azure mounts a persistent storage volume at `/home` that survives:
- Container restarts
- Redeployments
- Scaling events

### Database paths

The application stores all databases under `/home/app/`:

| Database | Path | Environment Variable |
|----------|------|---------------------|
| SQLite | `/home/app/db/sqlite3/db.sqlite3` | `SQLITE3_STORAGE_PATH` |
| ChromaDB | `/home/app/db/chromadb/` | `CHROMADB_STORAGE_PATH` |
| DuckDB | `/home/app/db/duckdb/` | `DUCKDB_STORAGE_PATH` |

### Application configuration

In `aistarterkit/settings.py`, the SQLite path is configured dynamically:

```python
sqlite_storage_path = os.getenv("SQLITE3_STORAGE_PATH")

if not sqlite_storage_path:
    # Fallback for local development
    sqlite_storage_path = Path(BASE_DIR) / "db/sqlite3/"

os.makedirs(sqlite_storage_path, exist_ok=True)
db_path = Path(sqlite_storage_path) / "db.sqlite3"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(db_path),
    }
}
```

This allows the same codebase to work locally (using `./db/sqlite3/`) and in production (using `/home/app/db/sqlite3/`).

## Quick Start

1. Copy `.env.example` to `.env` and fill in your secrets:
   ```bash
   cp .env.example .env
   ```

2. Ensure you have the required environment variables:
   ```
   GITHUB_TOKEN=your_github_pat
   AZURE_SUBSCRIPTION_ID=your_subscription_id
   ROCKETSHIP_REGISTRY_PASSWORD=your_acr_password
   ```

3. Run the setup:
   ```bash
   python rocketship.py setup
   ```

4. For subsequent deploys, just push to `main`:
   ```bash
   git push origin main
   ```
