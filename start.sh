#!/bin/bash
# Startup script for Railway deployment
set -e  # Exit on error

echo "=== STARTUP DIAGNOSTICS ==="
echo "Port: ${PORT:-8000}"
echo "Python: $(python --version)"
echo "Working directory: $(pwd)"
RUN_AS_WORKER_NORMALIZED=$(printf '%s' "${RUN_AS_WORKER:-false}" | tr '[:upper:]' '[:lower:]')
echo "Run as worker: ${RUN_AS_WORKER_NORMALIZED}"

# Check database connection
if [ -n "$DATABASE_URL" ]; then
    echo "DATABASE_URL: PRESENT (PostgreSQL)"
else
    echo "DATABASE_URL: MISSING (Will default to SQLite)"
    exit 1
fi

echo ""
echo "=== ENSURING PGVECTOR ==="
python manage.py ensure_pgvector

echo ""
echo "=== RUNNING MIGRATIONS ==="
python manage.py migrate --noinput

if [ "$RUN_AS_WORKER_NORMALIZED" = "true" ]; then
    echo ""
    echo "=== STARTING CELERY WORKER (SINGLETON MODE) ==="
    exec celery -A config worker -l info --concurrency=1
else
    echo ""
    echo "=== CREATING/UPDATING SUPERUSER ==="
    python manage.py create_default_superuser

    echo ""
    echo "=== CHECKING TABLES ==="
    python manage.py shell -c "from django.db import connection; cursor = connection.cursor(); cursor.execute('SELECT COUNT(*) FROM django_migrations'); print(f'Migrations applied: {cursor.fetchone()[0]}');"

    echo ""
    echo "=== COLLECTING STATIC FILES ==="
    python manage.py collectstatic --noinput

    echo ""
    echo "=== STARTING GUNICORN ON PORT ${PORT:-8000} ==="
    exec gunicorn config.wsgi:application \
        --bind 0.0.0.0:${PORT:-8000} \
        --workers 3 \
        --timeout 120 \
        --access-logfile - \
        --error-logfile - \
        --log-level debug \
        --capture-output
fi
