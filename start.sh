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
    CELERY_CONCURRENCY_VALUE="${CELERY_CONCURRENCY:-1}"
    CELERY_POOL_VALUE="${CELERY_POOL:-solo}"
    CELERY_QUEUES_VALUE="${CELERY_QUEUES:-high_priority,low_priority,default}"

    echo ""
    echo "=== STARTING WORKER HEALTHCHECK SERVER ON PORT ${PORT:-8000} ==="
    python - <<'PY' &
from http.server import BaseHTTPRequestHandler, HTTPServer
import os

PORT = int(os.environ.get("PORT", "8000"))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/api/core/health/", "/api/core/health"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok","service":"worker"}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        return


HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
PY

    echo ""
    echo "=== STARTING CELERY WORKER ==="
    echo "Pool: ${CELERY_POOL_VALUE}"
    echo "Concurrency: ${CELERY_CONCURRENCY_VALUE}"
    echo "Queues: ${CELERY_QUEUES_VALUE}"
    exec celery -A config worker \
        --loglevel="${CELERY_LOGLEVEL:-info}" \
        --pool="${CELERY_POOL_VALUE}" \
        -Q "${CELERY_QUEUES_VALUE}" \
        --concurrency="${CELERY_CONCURRENCY_VALUE}"
else
    echo ""
    echo "=== CREATING/UPDATING SUPERUSER ==="
    python manage.py create_default_superuser

    echo ""
    echo "=== SEEDING AI SKILLS ==="
    python manage.py seed_ai_prompts

    echo ""
    echo "=== CHECKING TABLES ==="
    python manage.py shell -c "from django.db import connection; cursor = connection.cursor(); cursor.execute('SELECT COUNT(*) FROM django_migrations'); print(f'Migrations applied: {cursor.fetchone()[0]}');"

    echo ""
    echo "=== COLLECTING STATIC FILES ==="
    python manage.py collectstatic --noinput

    echo ""
    echo "=== STARTING DAPHNE ASGI SERVER ON PORT ${PORT:-8000} ==="
    exec daphne \
        -b 0.0.0.0 \
        -p ${PORT:-8000} \
        config.asgi:application
fi
