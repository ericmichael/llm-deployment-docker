#!/bin/bash
set -e

if [ -z "$OPENAI_API_BASE" ]; then
    unset OPENAI_API_BASE
fi

# Wait for PostgreSQL to be ready
wait_for_postgres() {
    echo "Waiting for PostgreSQL to be ready..."
    local max_attempts=30
    local attempt=1

    # Extract host and port from DATABASE_URL if PGHOST not set
    if [ -z "${PGHOST}" ] && [ -n "${DATABASE_URL}" ]; then
        # Parse postgres://user:pass@host:port/db format
        PGHOST=$(echo "$DATABASE_URL" | sed -n 's|.*@\([^:/]*\).*|\1|p')
        PGPORT=$(echo "$DATABASE_URL" | sed -n 's|.*:\([0-9]*\)/.*|\1|p')
    fi

    local host="${PGHOST:-localhost}"
    local port="${PGPORT:-5432}"

    echo "  Connecting to $host:$port..."

    while [ $attempt -le $max_attempts ]; do
        if pg_isready -h "$host" -p "$port" -q 2>/dev/null; then
            echo "PostgreSQL is ready!"
            return 0
        fi
        echo "  Attempt $attempt/$max_attempts - PostgreSQL not ready yet..."
        sleep 2
        attempt=$((attempt + 1))
    done

    echo "Warning: PostgreSQL may not be fully ready, proceeding anyway..."
    return 0
}

# Wait for PostgreSQL if DATABASE_URL is set
if [ -n "${DATABASE_URL}" ]; then
    wait_for_postgres
fi

# Run Django migrations
python manage.py migrate

# Run Django static file collection
python manage.py collectstatic --noinput

exec "$@"
