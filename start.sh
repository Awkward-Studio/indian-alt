#!/bin/bash
# Startup script for Railway deployment
set -e  # Exit on error

echo "--- Startup Diagnostics ---"
echo "Port: $PORT"
if [ -n "$DATABASE_URL" ]; then
    echo "DATABASE_URL is set (PostgreSQL)"
else
    echo "DATABASE_URL is NOT set (Defaulting to SQLite)"
fi

# Safety net: Run migrations here too if releaseCommand was skipped
echo "Running migrations (safety check)..."
python manage.py migrate --noinput

echo "Ensuring default superuser exists..."
python manage.py create_default_superuser

echo "Starting Gunicorn server on port $PORT..."
# Using gunicorn directly with optimized settings
exec gunicorn config.wsgi:application \
    --bind 0.0.0.0:$PORT \
    --access-logfile - \
    --error-logfile - \
    --log-level info \
    --timeout 120 \
    --workers 3


