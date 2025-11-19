#!/bin/bash
set -e

# Run the entrypoint script (migrations, collectstatic, etc.)
/entrypoint.sh gunicorn aistarterkit.wsgi:application --bind 0.0.0.0:8000 --workers 3 &

# Start LiteLLM proxy in the background
echo "Starting LiteLLM proxy on port 4000..."
litellm --config config/litellm-config.yaml --host 0.0.0.0 --port 4000 &

# Wait for both processes
wait -n

# Exit with status of process that exited first
exit $?
