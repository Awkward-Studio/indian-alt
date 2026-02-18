#!/bin/bash
# Startup script for Railway deployment
# Runs migrations, creates default superuser, then starts the server

set -e  # Exit on error

echo "Fixing app label (one-time: emails -> microsoft)..."
python manage.py fix_app_label

echo "Running database migrations..."
python manage.py migrate --noinput

echo "Creating/updating default superuser..."
python manage.py create_default_superuser

echo "Starting Gunicorn server..."
exec python -m gunicorn config.wsgi:application --bind 0.0.0.0:$PORT --access-logfile - --error-logfile - --log-level info
