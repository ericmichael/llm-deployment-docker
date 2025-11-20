#!/bin/bash
set -e

echo "=========================================="
echo "Starting LLM Deployment Container"
echo "=========================================="

# Run the entrypoint script (migrations, collectstatic, etc.)
echo "Running migrations and collecting static files..."
/entrypoint.sh echo "Entrypoint tasks completed"

# Start LiteLLM proxy in the background
echo ""
echo "Starting LiteLLM proxy on port 4000..."
litellm --config config/litellm-config.yaml --host 0.0.0.0 --port 4000 > /tmp/litellm.log 2>&1 &
LITELLM_PID=$!
echo "LiteLLM started with PID: $LITELLM_PID"

# Give LiteLLM a moment to start
echo "Waiting for LiteLLM to be ready..."
MAX_WAIT=60
for i in $(seq 1 $MAX_WAIT); do
    # Check if LiteLLM health endpoint is responding
    if curl -s http://localhost:4000/health > /dev/null 2>&1; then
        echo "LiteLLM health check passed!"
        # Give it a couple more seconds to fully initialize
        sleep 3
        echo "LiteLLM is ready!"
        break
    fi
    if [ $i -eq $MAX_WAIT ]; then
        echo "ERROR: LiteLLM failed to become ready after ${MAX_WAIT} seconds"
        echo "LiteLLM logs:"
        cat /tmp/litellm.log
        exit 1
    fi
    echo "Waiting for LiteLLM... ($i/$MAX_WAIT)"
    sleep 1
done

# Check if LiteLLM is still running
if ! kill -0 $LITELLM_PID 2>/dev/null; then
    echo "ERROR: LiteLLM failed to start!"
    cat /tmp/litellm.log
    exit 1
fi
echo "LiteLLM is running successfully"

# Start Django with Daphne (ASGI server for WebSocket support)
echo ""
echo "Starting Django with Daphne on port 8000..."
daphne -b 0.0.0.0 -p 8000 aistarterkit.asgi:application > /tmp/daphne.log 2>&1 &
DAPHNE_PID=$!
echo "Daphne started with PID: $DAPHNE_PID"

# Give Daphne a moment to start
sleep 2

# Check if Daphne is still running
if ! kill -0 $DAPHNE_PID 2>/dev/null; then
    echo "ERROR: Daphne failed to start!"
    cat /tmp/daphne.log
    exit 1
fi
echo "Daphne is running successfully"

echo ""
echo "=========================================="
echo "Services Started Successfully"
echo "  - LiteLLM Proxy: http://0.0.0.0:4000"
echo "  - Django App:    http://0.0.0.0:8000"
echo "  - WebSocket:     ws://0.0.0.0:8000/ws/realtime"
echo "=========================================="
echo ""
echo "Tailing logs (Ctrl+C to view both logs)..."

# Tail both log files
tail -f /tmp/litellm.log /tmp/daphne.log &
TAIL_PID=$!

# Function to handle shutdown
shutdown() {
    echo ""
    echo "Shutting down services..."
    kill $LITELLM_PID 2>/dev/null || true
    kill $DAPHNE_PID 2>/dev/null || true
    kill $TAIL_PID 2>/dev/null || true
    exit 0
}

# Trap SIGTERM and SIGINT
trap shutdown SIGTERM SIGINT

# Wait for either process to exit
wait -n $LITELLM_PID $DAPHNE_PID

# If we get here, one of the processes exited
EXIT_CODE=$?
echo "A service has exited with code: $EXIT_CODE"

# Show recent logs
echo ""
echo "Recent LiteLLM logs:"
tail -20 /tmp/litellm.log
echo ""
echo "Recent Daphne logs:"
tail -20 /tmp/daphne.log

# Cleanup
shutdown
