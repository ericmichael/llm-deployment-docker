"""
Real-stack test harness.

Boots the SAME ASGI application the container serves (Django + the embedded
LiteLLM proxy) on a local uvicorn thread, against the Django *test* database
(LiteLLM's Prisma schema is pushed into it), and points the Django code at it.

Nothing on the LiteLLM side is mocked. The only outbound traffic is LiteLLM ->
Azure OpenAI, which VCR records to chat/tests/fixtures/cassettes/ on first run
(needs AZURE_OPENAI_* in .env) and replays afterwards. Realtime (WebSocket)
sessions can't be recorded by VCR and always run live.
"""

import os
import pathlib
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse

import httpx
import vcr
from django.conf import settings
from django.db import connection

BASE_DIR = pathlib.Path(settings.BASE_DIR)
CASSETTE_DIR = BASE_DIR / "chat" / "tests" / "fixtures" / "cassettes"
MASTER_KEY = "sk-test-master-key"
SCRUBBED_AZURE_HOST = "azure-openai.example.invalid"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _test_db_url() -> str:
    d = connection.settings_dict
    user = urllib.parse.quote(d["USER"] or "")
    pw = urllib.parse.quote(d["PASSWORD"] or "")
    return f"postgresql://{user}:{pw}@{d['HOST'] or 'localhost'}:{d['PORT'] or 5432}/{d['NAME']}"


class LiteLLMStack:
    """Process-wide singleton: started by the first e2e test class, stopped at exit."""

    _instance = None

    @classmethod
    def get(cls):
        if cls._instance is None:
            cls._instance = cls()
            cls._instance.start()
        return cls._instance

    def __init__(self):
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.proxy_url = f"{self.base_url}/litellm"
        self.api_url = f"{self.base_url}/v1"
        self.server = None
        self.thread = None

    # -- lifecycle -----------------------------------------------------------

    def start(self):
        litellm_db_url = _test_db_url() + "?schema=litellm"
        env = {
            "LITELLM_DATABASE_URL": litellm_db_url,
            "LITELLM_MASTER_KEY": MASTER_KEY,
            "CONFIG_FILE_PATH": str(BASE_DIR / "config" / "litellm-config.yaml"),
            "DISABLE_AIOHTTP_TRANSPORT": "True",  # VCR hooks httpx, not aiohttp
            "PROXY_BATCH_WRITE_AT": "1",  # flush SpendLogs every second
            "DISABLE_ADMIN_UI": "true",
            "LITELLM_LOG": "ERROR",
        }
        # Replaying cassettes needs *some* Azure config for the model list to load.
        env.setdefault("AZURE_OPENAI_ENDPOINT", os.getenv("AZURE_OPENAI_ENDPOINT") or f"https://{SCRUBBED_AZURE_HOST}")
        env.setdefault("AZURE_OPENAI_API_KEY", os.getenv("AZURE_OPENAI_API_KEY") or "recorded")
        env.setdefault("OPENAI_API_VERSION", os.getenv("OPENAI_API_VERSION") or "v1")
        os.environ.update(env)

        self._push_prisma_schema(litellm_db_url)

        import uvicorn
        from gateway.asgi import application  # imports the proxy; reads env above

        config = uvicorn.Config(application, host="127.0.0.1", port=self.port, log_level="error", lifespan="on")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, name="litellm-stack", daemon=True)
        self.thread.start()
        self._wait_healthy()

    def _push_prisma_schema(self, db_url):
        import litellm.proxy

        schema = pathlib.Path(litellm.proxy.__file__).parent / "schema.prisma"
        prisma_bin = str(pathlib.Path(sys.executable).parent / "prisma")
        if not os.path.exists(prisma_bin):
            prisma_bin = shutil.which("prisma") or "prisma"
        subprocess.run(
            [prisma_bin, "db", "push", "--skip-generate", "--accept-data-loss", "--schema", str(schema)],
            env={**os.environ, "DATABASE_URL": db_url},
            check=True,
            capture_output=True,
            timeout=300,
        )

    def _wait_healthy(self, timeout=120):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                r = httpx.get(f"{self.proxy_url}/health/liveliness", timeout=5)
                if r.status_code == 200:
                    return
                last = r.text
            except Exception as exc:  # server not up yet
                last = repr(exc)
            time.sleep(0.5)
        raise RuntimeError(f"LiteLLM stack did not become healthy: {last}")

    def stop(self):
        if self.server:
            self.server.should_exit = True
            self.thread.join(timeout=30)
            self.server = None
        # Prisma keeps a pooled connection to the test DB; make sure it's gone
        # before Django drops the database.
        try:
            from litellm.proxy import proxy_server

            client = getattr(proxy_server, "prisma_client", None)
            if client is not None:
                import asyncio

                asyncio.run(client.disconnect())
        except Exception:  # best effort
            pass

    # -- helpers -------------------------------------------------------------

    def master_headers(self):
        return {"Authorization": f"Bearer {MASTER_KEY}", "Content-Type": "application/json"}

    def chat(self, key, model, content="Reply with the single word: pong", **extra):
        return httpx.post(
            f"{self.api_url}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model, "messages": [{"role": "user", "content": content}], "max_tokens": 5, **extra},
            timeout=60,
        )

    def wait_for_spend(self, key, predicate, timeout=15):
        """Poll /key/info until predicate(spend) holds (spend is written in batches)."""
        from chat import litellm_keys

        deadline = time.time() + timeout
        while time.time() < deadline:
            info = litellm_keys.key_info(key) or {}
            spend = float(info.get("spend") or 0)
            if predicate(spend):
                return spend
            time.sleep(0.5)
        return spend


# -- VCR ---------------------------------------------------------------------

def _scrub_request(request):
    """Only Azure traffic is recorded; hide the tenant host and credentials."""
    if request.host.startswith("127.0.0.1") or request.host == "localhost":
        return None
    real_host = request.host
    request.uri = request.uri.replace(real_host, SCRUBBED_AZURE_HOST)
    # The Host header carries the tenant name too, and rewriting the URI does
    # not touch it - cassettes recorded before this kept the real resource
    # hostname. Matching ignores the host (see match_on), so replacing it is
    # safe. filter_headers would drop the header entirely; this keeps the shape
    # of the request intact.
    for name in list(request.headers):
        if name.lower() == "host":
            request.headers[name] = SCRUBBED_AZURE_HOST
    return request


azure_vcr = vcr.VCR(
    cassette_library_dir=str(CASSETTE_DIR),
    record_mode="once",  # record when the cassette is missing, replay (and fail on new requests) otherwise
    match_on=["method", "path", "query", "body"],  # host is scrubbed, so don't match on it
    filter_headers=["api-key", "authorization", "Authorization", "ocp-apim-subscription-key"],
    before_record_request=_scrub_request,
    decode_compressed_response=True,
)


def azure_cassette(name):
    """Decorator: wrap a test so LiteLLM's Azure calls are recorded/replayed."""
    return azure_vcr.use_cassette(f"{name}.yaml")


def has_live_azure() -> bool:
    return bool(os.getenv("AZURE_OPENAI_API_KEY")) and os.getenv("AZURE_OPENAI_API_KEY") != "recorded"
