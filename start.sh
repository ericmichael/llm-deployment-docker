#!/bin/bash
set -e

echo "=========================================="
echo "Starting LLM Deployment Container"
echo "=========================================="

# Configuration
WORKERS=${GUNICORN_WORKERS:-4}  # Default to 4 workers for 4 vCPU
WORKER_TIMEOUT=${GUNICORN_TIMEOUT:-120}  # 2 minute timeout for LLM requests
WORKER_CLASS="uvicorn.workers.UvicornWorker"
MAX_REQUESTS=${GUNICORN_MAX_REQUESTS:-1000}  # Recycle workers periodically
MAX_REQUESTS_JITTER=${GUNICORN_MAX_REQUESTS_JITTER:-50}

# Run the entrypoint script (migrations, collectstatic, etc.)
echo "Running migrations and collecting static files..."
/entrypoint.sh echo "Entrypoint tasks completed"

# Create log directory
mkdir -p /tmp/logs

# Function to check if a process is running
is_running() {
    kill -0 "$1" 2>/dev/null
}

# Function to start LiteLLM with auto-restart
start_litellm() {
    while true; do
        echo "[$(date)] Starting LiteLLM proxy on port 4000..."
        # Run litellm without DATABASE_URL (it tries to use Prisma if set)
        (unset DATABASE_URL; litellm --config config/litellm-config.yaml --host 0.0.0.0 --port 4000) >> /tmp/logs/litellm.log 2>&1 &
        LITELLM_PID=$!
        echo "[$(date)] LiteLLM started with PID: $LITELLM_PID"

        # Wait for process to exit
        wait $LITELLM_PID || true
        EXIT_CODE=$?

        echo "[$(date)] LiteLLM exited with code $EXIT_CODE, restarting in 5 seconds..."
        sleep 5
    done
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

# Start LiteLLM supervisor in background
start_litellm &
LITELLM_SUPERVISOR_PID=$!

# Wait for LiteLLM to be ready
echo "Waiting for LiteLLM to be ready..."
MAX_WAIT=60
LITELLM_AUTH_HEADER=""
if [ -n "$LITELLM_SERVICE_KEY" ]; then
    LITELLM_AUTH_HEADER="Authorization: Bearer $LITELLM_SERVICE_KEY"
fi
for i in $(seq 1 $MAX_WAIT); do
    if curl -s -H "$LITELLM_AUTH_HEADER" http://localhost:4000/health > /dev/null 2>&1; then
        echo "LiteLLM health check passed!"
        sleep 2
        echo "LiteLLM is ready!"
        break
    fi
    if [ $i -eq $MAX_WAIT ]; then
        echo "WARNING: LiteLLM not responding after ${MAX_WAIT} seconds, continuing anyway..."
    fi
    echo "Waiting for LiteLLM... ($i/$MAX_WAIT)"
    sleep 1
done

# Start Gunicorn supervisor in background
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
echo "  - LiteLLM Proxy: http://0.0.0.0:4000"
echo "  - Django App:    http://0.0.0.0:8000"
echo "  - WebSocket:     ws://0.0.0.0:8000/ws/realtime"
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
    kill $LITELLM_SUPERVISOR_PID 2>/dev/null || true
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
wait $LITELLM_SUPERVISOR_PID $GUNICORN_SUPERVISOR_PID
