# LiteLLM Integration Progress

## Completed Work
- Added LiteLLM as a hard dependency (`litellm[proxy]==1.80.0`) and raised `openai`/`gunicorn` versions to satisfy compatibility.
- Introduced `config/litellm-config.yaml` plus `LITELLM_BASE_URL`, `LITELLM_SERVICE_KEY`, and `LITELLM_DEFAULT_MODEL` settings; default model switched to `gpt-5` (Azure).
- Rewired all Django passthrough endpoints (`/api/v1/chat|completions|responses`) and the internal `Agent` class to target LiteLLM instead of OpenAI/Azure directly.
- Extended `docker-compose.yml` with a `litellm-proxy` service so the proxy runs alongside the Django app in stateless mode.
- Updated README with LiteLLM setup instructions, env var expectations, and compose workflow details.
- Removed obsolete vector/tool-agent unit tests and their VCR fixtures, leaving only the endpoints still supported.
- Re-recorded the `openai_api_chat_completions` cassette against the new proxy flow using your Azure credentials.
- Defaulted `LITELLM_SERVICE_KEY` to a deterministic value when unset, added configurable `LITELLM_MODEL_LIST`, and wired `Thread` records plus the `Agent` class to honor a user's model/temperature selection.
- Refreshed the `message_creation` cassette to return a successful LiteLLM-style response so integration tests no longer depend on failed OpenAI calls.
- Documented how to run the LiteLLM proxy during tests (including unsetting `OPENAI_API_KEY`) and kept the Thread dropdown aligned with the proxy's supported models.

## Outstanding Items
- None at this time.
