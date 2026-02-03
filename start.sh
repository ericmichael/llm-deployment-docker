#!/bin/bash
set -e

echo "=========================================="
echo "Starting LLM Deployment Container"
echo "=========================================="

# Configuration
WORKERS=${GUNICORN_WORKERS:-1}  # Async UvicornWorkers handle concurrency via the event loop, not process count. 1 worker is sufficient for ~30-50 concurrent users while keeping memory usage low.
WORKER_TIMEOUT=${GUNICORN_TIMEOUT:-960}  # 16 minutes — must exceed httpx timeout (900s) so Django can return proper errors
WORKER_CLASS="uvicorn.workers.UvicornWorker"
MAX_REQUESTS=${GUNICORN_MAX_REQUESTS:-1000}  # Recycle workers periodically
MAX_REQUESTS_JITTER=${GUNICORN_MAX_REQUESTS_JITTER:-50}

export LITELLM_NON_ROOT=${LITELLM_NON_ROOT:-true}

# Create log directory
mkdir -p /tmp/logs

# Function to check if a process is running
is_running() {
    kill -0 "$1" 2>/dev/null
}

# Function to start Gunicorn with auto-restart
start_gunicorn() {
    while true; do
        echo "[$(date)] Starting Gunicorn with $WORKERS workers on port 8000..."
        gunicorn aistarterkit.asgi:application \
            --bind 0.0.0.0:8000 \
            --workers $WORKERS \
            --worker-class $WORKER_CLASS \
            --timeout $WORKER_TIMEOUT \
            --graceful-timeout 30 \
            --keep-alive 65 \
            --max-requests $MAX_REQUESTS \
            --max-requests-jitter $MAX_REQUESTS_JITTER \
            --access-logfile /tmp/logs/gunicorn-access.log \
            --error-logfile /tmp/logs/gunicorn-error.log \
            --capture-output \
            --enable-stdio-inheritance \
            >> /tmp/logs/gunicorn.log 2>&1 &
        GUNICORN_PID=$!
        echo "[$(date)] Gunicorn started with PID: $GUNICORN_PID"

        # Wait for process to exit
        wait $GUNICORN_PID || true
        EXIT_CODE=$?

        echo "[$(date)] Gunicorn exited with code $EXIT_CODE, restarting in 5 seconds..."
        sleep 5
    done
}

export CONFIG_FILE_PATH=${CONFIG_FILE_PATH:-$APP_HOME/config/litellm-config.yaml}

# Start Gunicorn supervisor in background (serves both Django + LiteLLM via ASGI router)
start_gunicorn &
GUNICORN_SUPERVISOR_PID=$!

# Wait for Gunicorn to be ready
echo "Waiting for Django to be ready..."
sleep 3
for i in $(seq 1 30); do
    if curl -s http://localhost:8000/health/ > /dev/null 2>&1; then
        echo "Django health check passed!"
        break
    fi
    if [ $i -eq 30 ]; then
        echo "WARNING: Django not responding after 30 seconds"
    fi
    sleep 1
done

echo ""
echo "=========================================="
echo "Services Started Successfully"
echo "  - Unified ASGI:  http://0.0.0.0:8000"
echo "    - LiteLLM:     /v1/*"
echo "    - Django:      /admin, /chat, etc."
echo "  - Health Check:  http://0.0.0.0:8000/health/"
echo "  - Workers:       $WORKERS"
echo "=========================================="
echo ""

# Tail all log files
tail -f /tmp/logs/*.log &
TAIL_PID=$!

# Function to handle shutdown
shutdown() {
    echo ""
    echo "[$(date)] Shutting down services gracefully..."

    # Kill supervisor processes (they manage the actual services)
    kill $GUNICORN_SUPERVISOR_PID 2>/dev/null || true
    kill $TAIL_PID 2>/dev/null || true

    # Send SIGTERM to all child processes for graceful shutdown
    pkill -TERM -P $$ 2>/dev/null || true

    # Wait briefly for graceful shutdown
    sleep 2

    # Force kill remaining processes
    pkill -KILL -P $$ 2>/dev/null || true

    echo "[$(date)] Shutdown complete"
    exit 0
}

# Trap signals for graceful shutdown
trap shutdown SIGTERM SIGINT SIGQUIT

# Wait for supervisor processes
wait $GUNICORN_SUPERVISOR_PID
