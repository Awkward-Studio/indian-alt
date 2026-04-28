#!/bin/bash

# Navigate to the script's directory if called from elsewhere
cd "$(dirname "$0")"

# Detect virtual environment
VENV_BIN="./venv/bin/celery"
if [ -f "$VENV_BIN" ]; then
    CELERY_CMD="$VENV_BIN"
else
    CELERY_CMD="celery"
fi

echo "🧹 Purging all Celery queues for 'config' app..."

# Use Python to flush Redis (works even without redis-cli)
./venv/bin/python -c "import redis; r = redis.from_url('redis://localhost:6379/0'); print('Flushing Redis DB 0...'); r.flushdb();"

# Also run celery purge just to be sure
$CELERY_CMD -A config purge -f

echo "✅ Queues purged."
echo ""
echo "💡 Note: This cleared PENDING tasks."
echo "   If you have tasks CURRENTLY RUNNING, you should restart your Celery worker."
echo "   You can do this by stopping the current worker process (Ctrl+C) and running:"
echo "   ./start.sh"
