#!/bin/bash
set -e

if [ -z "$OPENAI_API_BASE" ]; then
    unset OPENAI_API_BASE
fi

# Run Django migrations
python manage.py migrate

# Run Django static file collection
python manage.py collectstatic --noinput

exec "$@"
