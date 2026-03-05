#!/bin/bash
# Startup script for Railway deployment
# Release tasks (migrations, fix_app_label) now run in releaseCommand (release.sh)
# This script only starts the server.

set -e  # Exit on error

echo "Starting Gunicorn server on port $PORT..."
# Using gunicorn directly with optimized settings for container
exec gunicorn config.wsgi:application \
    --bind 0.0.0.0:$PORT \
    --access-logfile - \
    --error-logfile - \
    --log-level info \
    --timeout 120 \
    --workers 3

