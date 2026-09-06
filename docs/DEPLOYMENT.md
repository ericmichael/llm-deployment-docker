# Deployment Guide

This project deploys to **Azure App Service for Containers** with the `rocketship.py` helper script, run from your machine. There is no deploy pipeline: GitHub Actions only runs the test suite on pull requests.

## Overview

```
python rocketship.py deploy
        │
        ├── Build & push the image to Azure Container Registry (:latest + :<sha>)
        ├── Push the App Service settings
        └── Repoint the Web App at the :<sha> image and restart
```

It pins a **SHA tag** rather than `:latest`, because App Service does not
reliably re-pull a `:latest` tag that has moved. Pushing to `main` deploys
nothing.

## Prerequisites

On your machine:

- Docker, running
- Azure CLI (`az`), logged in
- Python with this project's dependencies — `rocketship.py` imports `requests`,
  `yaml` and `dotenv`, all of which come from `pip install -r requirements.txt`

In Azure, before anything below will work: a resource group, a container
registry, a Linux App Service Plan, a Web App for Containers, a PostgreSQL
Flexible Server, and an Azure OpenAI resource with model deployments. The next
section provisions all of it.

## First-time provisioning

Everything here is idempotent enough to re-run, and each step ends by printing
the value it produces for `.env.deploy`. Pick your own names; the shape below
is what this project's own deployment uses.

```bash
# Names - change these
export RG=my-rg
export LOCATION=southcentralus
export ACR=myregistry                 # 5-50 alphanumerics, globally unique
export PLAN=my-asp
export APP=my-webapp                  # becomes <APP>.azurewebsites.net, globally unique
export PGSERVER=my-psql               # globally unique
export PGDB=myapp_production
export PGADMIN=pgadmin
```

### 1. Sign in

```bash
az login
az account set --subscription "<your subscription name or id>"
az account show --query id -o tsv          # -> AZURE_SUBSCRIPTION_ID
az group create -n $RG -l $LOCATION
```

### 2. Container registry

The Basic SKU is enough. `rocketship.py` authenticates with a username and
password rather than a token, so the admin user must be enabled.

```bash
az acr create -n $ACR -g $RG --sku Basic --admin-enabled true
az acr credential show -n $ACR --query 'passwords[0].value' -o tsv
#   -> ROCKETSHIP_REGISTRY_PASSWORD
#   The username is the registry name; the server is <ACR>.azurecr.io.
```

### 3. App Service Plan and Web App

The plan must be Linux (`--is-linux`). Sizing is yours to choose; a Basic B-tier
is the smallest that supports Always On, which matters here — see the note at
the end of this section.

```bash
az appservice plan create -n $PLAN -g $RG --is-linux --sku B3

# Create the Web App against any placeholder image; rocketship.py repoints it
# at your real image on the first deploy.
az webapp create -n $APP -g $RG -p $PLAN \
  --deployment-container-image-name mcr.microsoft.com/appsvc/staticsite:latest

az webapp update -n $APP -g $RG --https-only true
az webapp config set -n $APP -g $RG --always-on true
```

Point the platform's health probe at the app's own health endpoint:

```bash
az webapp config set -n $APP -g $RG \
  --generic-configurations '{"healthCheckPath":"/health/"}'
```

**Always On.** With it off, App Service unloads the container after roughly 20
minutes idle, and the next request pays a full cold start — which for this app
means booting the embedded LiteLLM proxy and running `sync_litellm_keys` before
anything is served. Turn it on unless you are deliberately saving money on a
plan that does not support it.

**Health check.** `/health/` must be reachable unauthenticated or the probe
reads a redirect as a failure — `/health/*` is in Easy Auth's `excludedPaths`
for exactly that reason (see step 6). Note what the endpoint reports: it returns
503 only when the **database** is unreachable, and 200 for "degraded", which is
what a LiteLLM failure produces. So the platform restarts the instance for a
database outage but not a proxy one, deliberately — restarting the container
would not fix a proxy that is already struggling. With a single instance
Azure restarts rather than routes around, after 10 consecutive failed pings
(`WEBSITE_HEALTHCHECK_MAXPINGFAILURES` changes that).

### 4. PostgreSQL

One server, one database. Django and LiteLLM share it — LiteLLM's Prisma tables
are isolated in a separate schema (`LITELLM_DB_SCHEMA=litellm`), created by
`prisma db push` on the first container boot, so you do not create it yourself.

```bash
az postgres flexible-server create -n $PGSERVER -g $RG -l $LOCATION \
  --tier Burstable --sku-name Standard_B1ms --storage-size 32 --version 17 \
  --admin-user $PGADMIN --admin-password '<a strong password>' \
  --public-access None

az postgres flexible-server db create -g $RG -s $PGSERVER -d $PGDB

# Let App Service reach it. The 0.0.0.0 rule is Azure's "allow Azure services"
# special case, not a literal address, and does not open the server to the
# internet.
az postgres flexible-server firewall-rule create -g $RG -n $PGSERVER \
  --rule-name AllowAzureServices --start-ip-address 0.0.0.0 --end-ip-address 0.0.0.0
```

That gives you:

```
DATABASE_URL=postgres://<PGADMIN>:<password>@<PGSERVER>.postgres.database.azure.com:5432/<PGDB>?sslmode=require
```

`sslmode=require` is not optional — Flexible Server refuses plaintext.

### 5. Azure OpenAI

```bash
az cognitiveservices account create -n my-openai -g $RG -l eastus \
  --kind OpenAI --sku S0 --custom-domain my-openai

az cognitiveservices account show -n my-openai -g $RG \
  --query properties.endpoint -o tsv                      # -> AZURE_OPENAI_ENDPOINT
az cognitiveservices account keys list -n my-openai -g $RG \
  --query key1 -o tsv                                     # -> AZURE_OPENAI_API_KEY
```

Then create one deployment per model you intend to serve. **The deployment name
must equal the name after `azure/` in `config/litellm-config.yaml`** — LiteLLM
sends the deployment name to Azure, so a mismatch is a 404 at request time, not
a startup error.

```bash
az cognitiveservices account deployment create -n my-openai -g $RG \
  --deployment-name gpt-5 --model-name gpt-5 --model-version 2025-08-07 \
  --model-format OpenAI --sku-name GlobalStandard --sku-capacity 100
```

To check an existing account against the config:

```bash
az cognitiveservices account deployment list -n my-openai -g $RG --query '[].name' -o tsv | sort > /tmp/live
grep -E '^\s+model: azure/' config/litellm-config.yaml | sed 's|.*azure/||' | sort -u > /tmp/cfg
comm -13 /tmp/live /tmp/cfg      # anything printed is declared but not deployed
```

Every deployment in `config/litellm-config.yaml` also sets
`model_info.base_model` to a key in LiteLLM's price map. Keep that accurate or
spend tracking — and therefore every budget — silently reads zero.

### 6. Authentication

This is the step that decides whether the app is safe to expose.

`chat/middleware.py` trusts the `X-MS-CLIENT-PRINCIPAL-NAME` header and logs in
(or creates) whatever user it names. That is only sound when App Service
Authentication is enabled, because the platform then strips any client-supplied
copy of that header and sets its own. Turn it on:

```bash
az webapp auth microsoft update -n $APP -g $RG \
  --client-id <app registration client id> \
  --client-secret <client secret> \
  --issuer https://login.microsoftonline.com/<tenant id>/v2.0 \
  --yes
az webapp auth update -n $APP -g $RG --enabled true \
  --action RedirectToLoginPage --redirect-provider azureactivedirectory
```

Verify it. Note that `az webapp auth show` can report the whole block as null
even when authentication is active; read the config resource directly instead:

```bash
az resource show --ids "/subscriptions/$(az account show --query id -o tsv)/resourceGroups/$RG/providers/Microsoft.Web/sites/$APP/config/authsettingsV2" \
  --query 'properties.{platformEnabled:platform.enabled, requireAuth:globalValidation.requireAuthentication}' -o json
```

You want `platformEnabled: true` and `requireAuth: true`.

**If you are not turning authentication on**, you must set
`EASYAUTH_ENABLED=false` in `.env.deploy`. Left unset, `settings.py` infers it
from `WEBSITE_HOSTNAME`, which App Service always sets — so the header would be
trusted while nothing is stripping it, and anyone could sign in as anyone.

### 7. Secrets you generate yourself

```bash
python -c "import secrets; print(secrets.token_urlsafe(50))"   # -> SECRET_KEY
python -c "import secrets; print(secrets.token_urlsafe(32))"   # -> LITELLM_MASTER_KEY
```

`LITELLM_MASTER_KEY` authenticates both the proxy's admin API and Django's calls
into it. Any opaque string works; the `sk-` prefix often shown in LiteLLM's docs
is a convention, not a requirement.

`DEFAULT_ADMIN_EMAIL` and `DEFAULT_ADMIN_PASSWORD` seed a Django superuser
through migration `0002`, but only on a database with no user at that address —
so set them before the first deploy or create the superuser by hand later.

### 8. Point the repo at your resources

Edit `config/azure-deploy.yml` — `image`, `registry.server`, `registry.username`,
`app_service.app_name`, `app_service.resource_group`, and `CUSTOM_HOSTNAME`
under `additional_env` — to match what you just created. The
`${VAR}` placeholders resolve from `.env.deploy` and should be left alone.

## The two env files

`rocketship.py` reads **`.env.deploy` and nothing else**. `.env` belongs to the
local dev server. Both have committed templates - `.env.example` and
`.env.deploy.example` - listing every variable the app reads.

| File | Read by | Contents |
|------|---------|----------|
| `.env` | docker compose (`env_file`), `manage.py`, `settings.py` via `load_dotenv()` | the local Postgres URL and local credentials |
| `.env.deploy` | `rocketship.py` only | production settings **and** the deploy credentials |

Keeping them apart fixes two problems that a single `.env` caused:

- `.env` held the production `DATABASE_URL`, and `gateway/settings.py`
  calls `load_dotenv()`. Any `manage.py` invocation outside docker compose
  therefore opened a connection to the Azure database.
- Everything in `.env` was uploaded as an App Service setting, so local-only
  values shipped to production, and deploy credentials the running app never
  reads (the registry password, the subscription id) sat in its configuration.

Both files are gitignored (`.env`, `.env.*`). Neither is ever committed.

### What is and is not uploaded

Everything in `.env.deploy` becomes an App Service setting, except the prefixes
listed in `NOT_FOR_DEPLOYMENT` in `rocketship.py`:

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

The `AZURE_OPENAI_*`, `OPENAI_API_VERSION` and `LITELLM_MASTER_KEY` values are
deliberately **not** on that list and must not be: the embedded proxy resolves
them from the environment at boot, through the `os.environ/...` references in
`config/litellm-config.yaml`.

## Configuration: `config/azure-deploy.yml`

```yaml
image: cs4341/playground
registry:
  server: cs4341.azurecr.io
  username: cs4341
  password: ${ROCKETSHIP_REGISTRY_PASSWORD}
service: myapp
azure:
  subscription: ${AZURE_SUBSCRIPTION_ID}
  app_service:
    app_name: csci3351
    resource_group: cs-rg-4341-scus
    additional_env:
      DATABASE_URL: ${DATABASE_URL}
      ENVIRONMENT: production
      CUSTOM_HOSTNAME: rgvaiclass.com
      WEBSITES_PORT: 8000
      WEBSITES_CONTAINER_START_TIME_LIMIT: 500
      WEBSITES_ENABLE_APP_SERVICE_STORAGE: "false"
      CONFIG_FILE_PATH: /usr/src/app/config/litellm-config.yaml
      LITELLM_DB_SCHEMA: litellm
      LITELLM_ENABLE_VIRTUAL_KEYS: true
      LITELLM_MASTER_KEY: ${LITELLM_MASTER_KEY}
      LITELLM_PROXY_BASE_URL: http://localhost:8000/litellm
      DISABLE_ADMIN_UI: true
      EASYAUTH_ENABLED: ${EASYAUTH_ENABLED}
```

That is the whole file. There is no `github:` block: deployment does not
involve GitHub.

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
python rocketship.py download          # list files in the Kudu /home/backups
python rocketship.py download <file>   # download one via the Kudu VFS API
python rocketship.py upload <file>     # upload one into the Kudu /home/backups
```

### `deploy` in detail

1. **Validates prerequisites** — Docker, a Dockerfile, the Azure CLI
2. **Loads config** — reads `config/azure-deploy.yml`, substitutes `${VAR}` placeholders
3. **Docker build & push** — logs into ACR, builds, pushes `:latest` and `:<short git sha>`
4. **Azure App Service** — pushes settings via a temp JSON file, repoints the container
   at the SHA tag, then stop/starts the Web App

App settings are pushed as one JSON file and never as individual
`--settings K=V` arguments: argv is world-readable through `ps`, and these are
secrets. A failure aborts rather than retrying key-by-key.

## Continuous integration

`.github/workflows/run-tests.yml` runs the unit and integration suites against a
PostgreSQL service container on every pull request. It reads no secrets.

There is deliberately no deploy workflow. Deployment happens from a machine that
already holds `.env.deploy` and an `az login` session, which keeps the
production credentials off GitHub entirely — nothing needs to be mirrored into
repository secrets, and a push can never move production.

## Storage

`WEBSITES_ENABLE_APP_SERVICE_STORAGE` is **`false`**. `/home` inside the app
container is ephemeral: it does not survive a restart, a redeploy, or a move
between instances. Do not put anything there you expect to find later.

Nothing needs it. All application state is in PostgreSQL — Django's tables in
the default schema and LiteLLM's Prisma tables in the `litellm` schema of the
same database (`LITELLM_DB_SCHEMA=litellm`), reached through `DATABASE_URL`.
Static files are baked into the image by `collectstatic` at build time.

Turning the flag on mounts an Azure Files share at `/home` instead, which
persists but adds network-filesystem latency to every read the container makes
there. Only do it if something actually needs to outlive the container, and
expect the switch itself to change how the container starts.

### Backups, and the Kudu filesystem

`rocketship.py download` and `upload` do **not** talk to the app container.
They use the Kudu (SCM) site's VFS API, which is a separate container with its
own `/home`, always backed by persistent storage regardless of the flag above.
So a file you upload is not visible to the running application, and vice versa.
Kudu is a place to park a database dump you are moving between machines, not a
shared volume.

Two things about that API are worth recording, because both were wrong in this
script until they were fixed:

- The VFS root **is** `/home`. `/api/vfs/backups/` is `/home/backups`;
  `/api/vfs/home/backups/` means `/home/home/backups` and 404s.
- `az webapp deployment list-publishing-credentials --query '[a, b]' -o tsv`
  separates the two values with a **newline**, not a tab.

To take a database backup, run `pg_dump` against `DATABASE_URL` from wherever
you have network access to the server — the App Service firewall rule permits
Azure services, so `python rocketship.py ssh` and dumping from inside the
container works, as does dumping from a machine you have added a firewall rule
for.

## Deploying, end to end

Assuming the provisioning above is done:

1. Write `.env.deploy`, starting from the committed template:

   ```bash
   cp .env.deploy.example .env.deploy
   chmod 600 .env.deploy
   ```

   It is gitignored, and lists every variable with the command that produces
   it. Filled in, it looks like:

   ```
   # Deploy credentials - filtered out before upload
   AZURE_SUBSCRIPTION_ID=...
   ROCKETSHIP_REGISTRY_PASSWORD=...

   # Django
   SECRET_KEY=...
   DATABASE_URL=postgres://user:pass@host.postgres.database.azure.com:5432/db?sslmode=require
   DEFAULT_ADMIN_EMAIL=...
   DEFAULT_ADMIN_PASSWORD=...
   #EASYAUTH_ENABLED=false           # REQUIRED if App Service Authentication is off

   # Azure OpenAI - read from the environment by config/litellm-config.yaml
   AZURE_OPENAI_ENDPOINT=https://<name>.openai.azure.com
   AZURE_OPENAI_API_KEY=...
   OPENAI_API_VERSION=v1

   # LiteLLM
   LITELLM_MASTER_KEY=...
   ```

   Anything `config/azure-deploy.yml` pins in `additional_env` (`ENVIRONMENT`,
   `WEBSITES_PORT`, `CONFIG_FILE_PATH`, `LITELLM_DB_SCHEMA`,
   `LITELLM_ENABLE_VIRTUAL_KEYS`, `LITELLM_PROXY_BASE_URL`) does not belong
   here — that file wins on conflicts.

2. Deploy:

   ```bash
   python rocketship.py deploy
   ```

   It prints which names it declined to upload. The first boot is slow: it runs
   `prisma db push` to create the `litellm` schema, Django's migrations,
   `collectstatic`, and then `sync_litellm_keys`.

3. Verify, in this order:

   ```bash
   # a. the container came up and both halves are healthy
   curl -s https://$APP.azurewebsites.net/health/

   # b. watch the boot if it did not
   python rocketship.py logs

   # c. the proxy is serving the models you deployed
   curl -s -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
     https://$APP.azurewebsites.net/litellm/model/info | head -c 400
   ```

   `/health/` returns 200 for healthy or degraded and 503 for unhealthy, and
   names which check failed. Then open the site: you should be redirected to
   your identity provider, and land on `/chat/settings/` with a virtual key
   issued.

4. Subsequent deploys — re-run the same command. Pushing to `main` publishes
   code, not a deployment.

## Operational notes

- **App settings are only ever set, never deleted.** Removing a variable from
  `.env.deploy` leaves the old value on the Web App. Delete it explicitly:

  ```bash
  az webapp config appsettings delete -n $APP -g $RG --setting-names FOO BAR
  ```

- **`LITELLM_PRISMA_ACCEPT_DATA_LOSS` is a one-boot flag.** `entrypoint.sh` runs
  `prisma db push` without `--accept-data-loss` so that a LiteLLM schema change
  which would drop columns fails the boot instead of destroying the key and
  spend tables. Set it to `true` for the single boot that applies such an
  upgrade, then **remove it again**. Left on permanently it silently re-arms
  that failure mode for every future LiteLLM version bump.

- **The ~230s front door.** App Service times out non-streaming responses at
  about 230 seconds regardless of the 900s proxy and Gunicorn timeouts. Long
  completions must be streamed.

- **Worker recycling is off by default.** With a single worker, a recycle
  restarts the embedded LiteLLM proxy and drops realtime sockets and in-flight
  streams. `GUNICORN_MAX_REQUESTS` opts back in.
