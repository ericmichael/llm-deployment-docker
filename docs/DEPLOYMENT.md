# Deployment Guide

This project deploys to **Azure App Service for Containers** using a combination of the `rocketship.py` helper script and GitHub Actions for CI/CD.

## Overview

```
Local Setup (python rocketship.py deploy)
        │
        ├── Build & push Docker image to Azure Container Registry (:latest + :<sha>)
        ├── Push secrets to GitHub Actions
        ├── Push App Service settings
        └── Repoint the Web App at the :<sha> image and restart

Ongoing Deploys (git push to main)
        │
        └── GitHub Actions builds & pushes :latest + :<sha>
                │
                └── azure/webapps-deploy repoints the Web App at :<sha>
                    (only when AZURE_WEBAPP_PUBLISH_PROFILE is set)
```

Both paths pin a **SHA tag** rather than `:latest`, because App Service does not
reliably re-pull a `:latest` tag that has moved.

## Prerequisites

- Docker installed locally
- Azure CLI (`az`) installed and logged in
- `pynacl` (`pip install pynacl`), used to encrypt GitHub secrets
- A `.env.deploy` file (see below)

## The two env files

`rocketship.py` reads **`.env.deploy` and nothing else**. `.env` belongs to the
local dev server.

| File | Read by | Contents |
|------|---------|----------|
| `.env` | docker compose (`env_file`), `manage.py`, `settings.py` via `load_dotenv()` | the local Postgres URL and local credentials |
| `.env.deploy` | `rocketship.py` only | production settings **and** the deploy credentials |

Keeping them apart fixes two problems that a single `.env` caused:

- `.env` held the production `DATABASE_URL`, and `aistarterkit/settings.py`
  calls `load_dotenv()`. Any `manage.py` invocation outside docker compose
  therefore opened a connection to the Azure database.
- Everything in `.env` was uploaded as an App Service setting, so local-only
  values shipped to production, and deploy credentials the running app never
  reads (the registry password, the subscription id) sat in its configuration.

Both files are gitignored (`.env`, `.env.*`). Neither is ever committed.

### What is and is not uploaded

Everything in `.env.deploy` becomes an App Service setting and a GitHub Actions
secret, except the prefixes listed in `NOT_FOR_DEPLOYMENT` in `rocketship.py`:

```python
NOT_FOR_DEPLOYMENT = (
    "GITHUB_TOKEN",
    "ROCKETSHIP_",
    "AZURE_SUBSCRIPTION_ID",
    "AZURE_WEBAPP_NAME",
)
```

These are credentials for the deploy, not for the thing deployed — the running
container has no use for a subscription id or a registry password, and shipping
them puts them in configuration that anyone with read access can see. The deploy
prints which names it dropped.

`OPENAI_API_KEY` is deliberately **not** on that list: in this project it is a
live fallback for `LITELLM_SERVICE_KEY` (`aistarterkit/settings.py`), so the
deployed app really does read it. The `AZURE_OPENAI_*` and `LITELLM_*`
credentials are load-bearing for the same reason — the embedded proxy resolves
them from the environment at boot via `config/litellm-config.yaml`.

The registry credentials the GitHub Actions workflow needs
(`ROCKETSHIP_REGISTRY_SERVER`/`USERNAME`/`PASSWORD`, `ROCKETSHIP_IMAGE`) are
still pushed to GitHub — `setup()` adds them explicitly after the filtered env,
so the denylist does not starve CI.

## Configuration: `config/azure-deploy.yml`

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
      DATABASE_URL: ${DATABASE_URL}
      ENVIRONMENT: production
      WEBSITES_PORT: 8000
      # ... see the file for the rest
```

Every `${VAR}` is resolved from the environment (populated from `.env.deploy`)
at deploy time, so no credential is written into this file. Values in
`additional_env` **override** same-named values from `.env.deploy`, which is why
`CONFIG_FILE_PATH`, `LITELLM_DB_SCHEMA`, `LITELLM_ENABLE_VIRTUAL_KEYS` and
`LITELLM_PROXY_BASE_URL` are pinned here and not repeated in `.env.deploy`.

### EASYAUTH_ENABLED

`additional_env` carries `EASYAUTH_ENABLED: ${EASYAUTH_ENABLED}`. Left empty the
setting is skipped rather than pushed, and `settings.py` decides from
`WEBSITE_HOSTNAME` — which App Service always sets, so Easy Auth is **on** by
default.

Set `EASYAUTH_ENABLED=false` in `.env.deploy` on any Web App where App Service
Authentication is **not** turned on. There the `X-MS-CLIENT-PRINCIPAL-NAME`
header is client-controlled, and trusting it lets anyone sign in as anyone.

Note that an empty value is *skipped*, not *cleared*: to turn Easy Auth off you
must set a literal `false`, and to remove a setting entirely you have to delete
it in the portal or with `az webapp config appsettings delete`.

## rocketship.py commands

```
python rocketship.py init              # write a starter config + .rocketship/ scaffolding
python rocketship.py deploy            # build, push, configure, restart (--no-cache to skip cache)
python rocketship.py ssh               # shell into the running container
python rocketship.py logs              # stream live application logs
python rocketship.py restart           # stop/start the App Service
python rocketship.py download          # list backups in /home/backups
python rocketship.py download <file>   # download one via the Kudu VFS API
python rocketship.py upload <file>     # upload one into /home/backups
```

### `deploy` in detail

1. **Validates prerequisites** — Docker, a Dockerfile, `GITHUB_TOKEN` (optional), Azure CLI
2. **Loads config** — reads `config/azure-deploy.yml`, substitutes `${VAR}` placeholders
3. **Docker build & push** — logs into ACR, builds, pushes `:latest` and `:<short git sha>`
4. **GitHub secrets** — encrypts the deployable `.env.deploy` values with a libsodium
   sealed box and PUTs them, plus the `ROCKETSHIP_*` registry credentials
5. **Azure App Service** — pushes settings via a temp JSON file, repoints the container
   at the SHA tag, then stop/starts the Web App

App settings are pushed as one JSON file and never as individual
`--settings K=V` arguments: argv is world-readable through `ps`, and these are
secrets. A failure aborts rather than retrying key-by-key.

## CI/CD Pipeline

`.github/workflows/deploy-azure-wappserv.yaml` handles ongoing deployments:

1. On push to `main`
2. Builds the image with the GitHub Actions cache
3. Pushes `:latest` and `:<github.sha>` to ACR
4. Repoints App Service at `:<github.sha>` — **only if** the
   `AZURE_WEBAPP_PUBLISH_PROFILE` secret is set (App Service → Get publish
   profile). Without it the step is skipped and the new image is built but never
   deployed; use `python rocketship.py deploy` instead.

## Persistent Storage

`WEBSITES_ENABLE_APP_SERVICE_STORAGE: true` in `additional_env` mounts a
persistent volume at `/home` that survives container restarts, redeployments and
scaling events.

Application state does **not** live there. Django and LiteLLM share an Azure
Database for PostgreSQL Flexible Server, addressed by `DATABASE_URL`, with
LiteLLM's Prisma tables isolated in the `litellm` schema
(`LITELLM_DB_SCHEMA=litellm`). `/home` is used for ad-hoc backups
(`/home/backups`, which `rocketship.py download`/`upload` read and write) and
for anything else that must outlive a container.

## Quick Start

1. Create `.env.deploy` with the production settings and the deploy credentials:

   ```
   AZURE_SUBSCRIPTION_ID=...
   ROCKETSHIP_REGISTRY_PASSWORD=...
   GITHUB_TOKEN=...            # optional, only for pushing GitHub secrets

   SECRET_KEY=...
   DATABASE_URL=postgres://...@...postgres.database.azure.com:5432/...?sslmode=require
   AZURE_OPENAI_ENDPOINT=...
   AZURE_OPENAI_API_KEY=...
   LITELLM_MASTER_KEY=...
   LITELLM_SERVICE_KEY=...
   ```

2. Deploy:

   ```bash
   python rocketship.py deploy
   ```

3. For subsequent deploys, push to `main` (or re-run `deploy`):

   ```bash
   git push origin main
   ```
