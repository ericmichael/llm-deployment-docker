# AI Starter Kit

A Django application that gives a class of students metered, per-user access to
LLMs. It issues each student a LiteLLM virtual key, enforces per-user and
per-course monthly spend caps and model allowlists, and gives staff a roster,
budget and usage view over the whole thing.

Django and a LiteLLM proxy run **in one process** behind a single ASGI router
(`aistarterkit/asgi.py`), sharing one PostgreSQL database:

```
                    Gunicorn + UvicornWorker  (port 8000)
                              │
              ┌───────────────┴───────────────┐
              │      Starlette mount router    │
              ├────────────────────────────────┤
  /v1/*       │  LiteLLM proxy (OpenAI-compatible API)
  /litellm/*  │  LiteLLM management API
  /realtime   │  LiteLLM WebSocket
  /           │  Django (settings, courses, usage, admin)
              └────────────────────────────────┘
                              │
                      PostgreSQL
              Django tables │ litellm schema (Prisma)
```

## Features

- **Email-based auth** (`CustomUser`, no username), with optional Azure App
  Service Easy Auth via `chat/middleware.py`
- **Per-user LiteLLM virtual keys**, provisioned on first visit to the settings
  page, renewed on expiry, and rotatable by the student ("Regenerate key")
- **Budgets** resolved user → course → global, reset per calendar month, with
  staff "Reset usage" controls at every level
- **Course teams** mapping each `Course` to a LiteLLM team carrying a
  course-wide cap and a model allowlist
- **Usage dashboard** reading LiteLLM's aggregated daily activity table
- **Roster management** — CSV import of students and TAs, enrollment changes
  that revoke keys automatically

## Prerequisites

- Python 3.11+ (3.12 in the container; LiteLLM requires >= 3.11)
- Node.js 20+ and npm
- PostgreSQL (16 in the bundled Docker Compose service, 17 in production)
- Docker, if you want the containerized path

## Dependencies

### Python (`requirements.txt`)

- `django` (5.x) — the web framework
- `litellm[proxy]`, `prisma` — the embedded proxy and its schema tooling
- `uvicorn`, `gunicorn` — ASGI server and process manager (Gunicorn runs
  `uvicorn.workers.UvicornWorker`, so the stack is ASGI, not WSGI)
- `channels`, `daphne`, `websockets` — WebSocket support
- `psycopg2-binary`, `dj-database-url` — PostgreSQL
- `httpx` — the HTTP client Django uses to talk to the proxy
- `openai` — pinned for LiteLLM's benefit; no application code imports it
- `whitenoise` — static file serving
- `python-dotenv`, `pyyaml`
- `vcrpy` — test-only, records the end-to-end suite's Azure traffic

### JavaScript (`package.json`)

- `webpack` + `babel-loader` — bundles `assets/js/application.js` into
  `assets/js/bundle.js` (generated, not committed)
- `tailwindcss` (v3) — compiled through PostCSS by webpack into
  `assets/css/app.css` (generated, not committed). Run `npm run build` after
  adding new Tailwind classes
- `stimulus` + `tailwindcss-stimulus-components` — the `clipboard` and `reveal`
  controllers plus alert/tabs/toggle/modal/dropdown components
- `babel-plugin-prismjs` — syntax highlighting, configured in `.babelrc` for all
  languages with the 'tomorrow' theme and line numbers

FontAwesome is the one asset still loaded from a CDN
(`chat/templates/base_generic.html`).

## Setup

### Docker Compose (recommended)

Brings up PostgreSQL and the unified app with hot reload:

```bash
docker compose up --build
```

The app is served at <http://localhost:18000>, with LiteLLM mounted inside it at
`/v1` and `/litellm`. Settings come from `.env`, except `DATABASE_URL`, which
compose overrides to point at its own Postgres container.

### Running directly

1. Install dependencies:

   ```bash
   pip install -r requirements.txt
   npm install
   npm run build
   ```

2. Start PostgreSQL and make sure `DATABASE_URL` in `.env` points at it.

3. Push LiteLLM's Prisma schema into the `litellm` schema, then run Django's
   migrations:

   ```bash
   prisma db push --skip-generate --schema \
     $(python -c "import pathlib, litellm.proxy; print(pathlib.Path(litellm.proxy.__file__).parent / 'schema.prisma')")
   python manage.py migrate
   ```

4. Run the server. Use an ASGI server rather than `runserver`, since the LiteLLM
   mounts and WebSocket routes live in `aistarterkit/asgi.py`:

   ```bash
   gunicorn aistarterkit.asgi:application --bind 0.0.0.0:8000 \
     --worker-class uvicorn.workers.UvicornWorker
   ```

### Environment variables

Two files, and they are not interchangeable:

| File | Read by | Contents |
|------|---------|----------|
| `.env` | docker compose, `manage.py`, `settings.py` | local development only |
| `.env.deploy` | `rocketship.py` only | production settings + deploy credentials |

Never put a production `DATABASE_URL` in `.env` — `settings.py` calls
`load_dotenv()`, so a bare `manage.py` run would connect to it. Both files are
gitignored. See `docs/DEPLOYMENT.md` and the Environment Variables section of
`CLAUDE.md`.

The proxy resolves its Azure credentials (`AZURE_OPENAI_ENDPOINT`,
`AZURE_OPENAI_API_KEY`, `OPENAI_API_VERSION`) from the environment at boot, via
`os.environ/...` references in `config/litellm-config.yaml`.

### Hostname configuration

Local development accepts `localhost` and `127.0.0.1` by default, plus the
Azure health-probe address `169.254.130.2`. Extend the list with:

- `DJANGO_HEALTHCHECK_HOSTS` — comma-separated probe IPs. Defaults to `169.254.130.2`.
- `DJANGO_ADDITIONAL_ALLOWED_HOSTS` — additional hostnames or IPs.
- `CUSTOM_HOSTNAME` — the public hostname the app is served on.

## Usage

With the app running, `/` redirects to the settings page.

| Route | Who | What |
|-------|-----|------|
| `/chat/settings/` | any user | the user's virtual key, month-to-date spend, quickstart snippets |
| `/chat/models/` | any user | available models, pricing, and which ones their key may call |
| `/chat/courses/` | staff | roster, budgets, allowlists, CSV import |
| `/chat/usage/` | staff | spend across all users |
| `/admin` | staff | Django admin |
| `/health/` | anyone | database + LiteLLM liveliness, 200 healthy/degraded, 503 unhealthy |

Students point any OpenAI-compatible client at `https://<host>/v1` using the key
from their settings page.

### Management commands

```bash
python manage.py import_students COURSE_CODE path/to/students.csv
python manage.py import_tas COURSE_CODE path/to/tas.csv
python manage.py sync_litellm_keys [--dry-run]      # re-push budgets/limits (also runs at boot)
python manage.py reset_litellm_spend --user EMAIL | --course CODE | --all
python manage.py revoke_unenrolled_keys [--dry-run]
```

### Creating an admin

`DEFAULT_ADMIN_EMAIL` and `DEFAULT_ADMIN_PASSWORD` seed a superuser through
migration `0002` on a fresh database. Otherwise:

```bash
python manage.py createsuperuser
```

## Testing

```bash
DATABASE_URL=postgres://postgres:postgres@localhost:5432/aistarterkit \
  SECRET_KEY=x ENVIRONMENT=test python manage.py test
```

- **`chat/tests/unit/`** — key lifecycle and budget resolution, with LiteLLM HTTP
  mocked at the `chat.litellm_keys` boundary
- **`chat/tests/integration/`** — auth, Easy Auth middleware, course views, health check
- **`chat/tests/e2e/`** — boots the real unified ASGI app (Django *and* the
  embedded LiteLLM proxy) on a uvicorn thread against the test database and
  exercises key/team/budget/allowlist/spend flows over HTTP. Nothing
  LiteLLM-side is mocked; the traffic out to Azure is replayed from VCR
  cassettes in `chat/tests/fixtures/cassettes/`. Needs `prisma generate` once in
  the venv — see `CLAUDE.md`.

## Deployment

The project includes a deployment script that automates the process of deploying the application to Azure and setting up GitHub Actions secrets. The deployment script uses a YAML configuration file to manage deployment settings.

### Build Process

The JavaScript bundle (`assets/js/bundle.js`) is automatically generated during the Docker build process and should **not** be committed to version control. The Dockerfile handles running `npm install` and `npm run build` to create the production bundle. For local development, run `npm run build` manually after making changes to JavaScript files.

### Deployment Dependencies

Before running the deployment script, ensure that the following dependencies are installed and configured:

- `az`: The Azure CLI tool must be installed and available in your system's PATH. It is used to interact with Azure services.
- `docker`: Docker must be installed and running to build and push the Docker image.
- `pynacl`: This Python library is required for encryption operations used in the script. Install it using `pip install pynacl`.

### Configuration: `config/azure-deploy.yml`

Deployment settings live in `config/azure-deploy.yml` (the registry, the image
repository, the App Service name and resource group, and the `additional_env`
block of App Service settings). `python rocketship.py init` writes a starter
template if you are setting up a new project; this repository already has one.

Every `${VAR}` in that file is resolved from the environment at deploy time, so
no credential is written into it.

### The two env files

| File | Read by | Contents |
|------|---------|----------|
| `.env` | the local dev server, docker compose, `manage.py` | the local Postgres URL and local credentials |
| `.env.deploy` | `rocketship.py`, and nothing else | production settings **and** the deploy credentials |

`rocketship.py` never reads `.env`. Keeping them apart is what stops a local
experiment shipping to production, and stops a bare `manage.py` run on your
machine from opening a connection to the Azure database.

Everything in `.env.deploy` is uploaded as an App Service setting and a GitHub
Actions secret, except the names matched by `NOT_FOR_DEPLOYMENT` in
`rocketship.py` — `GITHUB_TOKEN`, `ROCKETSHIP_*`, `AZURE_SUBSCRIPTION_ID`,
`AZURE_WEBAPP_NAME`. Those are credentials for the deploy, not for the thing
deployed, and the running app never asks for them.

Both files are gitignored. Neither should ever be committed.

### Deploying

```
python rocketship.py deploy            # add --no-cache to skip the Docker cache
```

This:

1. Checks for Docker, a Dockerfile, `GITHUB_TOKEN` (optional) and the Azure CLI.
2. Loads `config/azure-deploy.yml` and resolves its `${VAR}` placeholders.
3. Logs into the container registry, builds the image, and pushes it tagged both
   `:latest` and `:<short git sha>`.
4. If `GITHUB_TOKEN` is set, pushes the deployable `.env.deploy` values plus the
   registry credentials to the repository as GitHub Actions secrets.
5. Pushes the App Service settings, repoints the Web App at the **SHA-tagged**
   image, and restarts it.

Step 5 pins a SHA rather than `:latest` because App Service will not reliably
re-pull a moved `:latest` tag.

### Other commands

```
python rocketship.py ssh               # shell into the running container
python rocketship.py logs              # stream live application logs
python rocketship.py restart           # stop/start the App Service
python rocketship.py download          # list files in the Kudu /home/backups
python rocketship.py download <file>   # download one, via the Kudu VFS API
python rocketship.py upload <file>     # upload one into the Kudu /home/backups
```

The last two talk to the Kudu (SCM) container, which has its own filesystem —
files there are not visible to the running application. See the Storage section
of `docs/DEPLOYMENT.md`.
