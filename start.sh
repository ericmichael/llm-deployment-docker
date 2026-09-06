#!/bin/bash
set -e

echo "=========================================="
echo "Starting LLM Deployment Container"
echo "=========================================="

# Configuration
WORKERS=${GUNICORN_WORKERS:-1}  # Async UvicornWorkers handle concurrency via the event loop, not process count. 1 worker is sufficient for ~30-50 concurrent users while keeping memory usage low.
WORKER_TIMEOUT=${GUNICORN_TIMEOUT:-960}  # 16 minutes — must exceed httpx timeout (900s) so Django can return proper errors
WORKER_CLASS="uvicorn.workers.UvicornWorker"
# Worker recycling is off by default: with a single worker, every recycle
# restarts the whole LiteLLM proxy (dropping realtime sockets/streams) and
# queues requests until its startup finishes. Set GUNICORN_MAX_REQUESTS>0 to opt in.
MAX_REQUESTS=${GUNICORN_MAX_REQUESTS:-0}
MAX_REQUESTS_JITTER=${GUNICORN_MAX_REQUESTS_JITTER:-0}
GUNICORN_PIDFILE=/tmp/gunicorn.pid
SHUTTING_DOWN=0

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
        gunicorn gateway.asgi:application \
            --bind 0.0.0.0:8000 \
            --workers $WORKERS \
            --worker-class $WORKER_CLASS \
            --timeout $WORKER_TIMEOUT \
            --graceful-timeout 30 \
            --keep-alive 65 \
            --max-requests $MAX_REQUESTS \
            --max-requests-jitter $MAX_REQUESTS_JITTER \
            --pid $GUNICORN_PIDFILE \
            --access-logfile /tmp/logs/gunicorn-access.log \
            --error-logfile /tmp/logs/gunicorn-error.log \
            --capture-output \
            --enable-stdio-inheritance \
            >> /tmp/logs/gunicorn.log 2>&1 &
        GUNICORN_PID=$!
        echo "[$(date)] Gunicorn started with PID: $GUNICORN_PID"

        # Wait for process to exit
        EXIT_CODE=0
        wait $GUNICORN_PID || EXIT_CODE=$?

        if [ -f /tmp/shutting_down ]; then
            echo "[$(date)] Gunicorn exited with code $EXIT_CODE during shutdown"
            return 0
        fi
        echo "[$(date)] Gunicorn exited with code $EXIT_CODE, restarting in 5 seconds..."
        sleep 5
    done
}
rm -f /tmp/shutting_down "$GUNICORN_PIDFILE"

export CONFIG_FILE_PATH=${CONFIG_FILE_PATH:-$APP_HOME/config/litellm-config.yaml}

# Function to handle shutdown
shutdown() {
    echo ""
    echo "[$(date)] Shutting down services gracefully..."

    # Tell the supervisor loop not to restart Gunicorn
    touch /tmp/shutting_down

    # Gunicorn is a grandchild (started inside the supervisor subshell), so it
    # must be signalled by PID: SIGTERM lets it finish in-flight requests and
    # run LiteLLM's shutdown hook (flushes the buffered spend logs).
    GUNICORN_MASTER=$(cat "$GUNICORN_PIDFILE" 2>/dev/null || true)
    if [ -n "$GUNICORN_MASTER" ]; then
        kill -TERM "$GUNICORN_MASTER" 2>/dev/null || true
        for _ in $(seq 1 30); do
            kill -0 "$GUNICORN_MASTER" 2>/dev/null || break
            sleep 1
        done
        kill -KILL "$GUNICORN_MASTER" 2>/dev/null || true
    fi

    [ -n "${GUNICORN_SUPERVISOR_PID:-}" ] && kill $GUNICORN_SUPERVISOR_PID 2>/dev/null || true
    [ -n "${TAIL_PID:-}" ] && kill $TAIL_PID 2>/dev/null || true
    pkill -KILL -P $$ 2>/dev/null || true

    echo "[$(date)] Shutdown complete"
    exit 0
}

# Install the trap BEFORE anything starts: as PID 1, bash drops SIGTERM unless a
# handler is registered, and docker/Azure stop can arrive during the startup
# health poll.
trap shutdown SIGTERM SIGINT SIGQUIT

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

# Re-sync every virtual key with the proxy (spend attribution + effective
# budget/rate/expiry limits) so limits never drift from settings.
# Brief delay to let LiteLLM auth fully initialize after health check passes
sleep 5
echo "Syncing LiteLLM virtual key limits..."
python manage.py sync_litellm_keys || echo "WARNING: sync_litellm_keys failed (non-fatal)"

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


# Wait for supervisor processes (the trap installed above handles SIGTERM)
wait $GUNICORN_SUPERVISOR_PID
