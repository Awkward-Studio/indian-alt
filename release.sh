#!/bin/bash
set -e

echo "--- Running Release Tasks ---"

echo "1. Fixing app label (emails -> microsoft)..."
# This is now safe even on fresh DBs thanks to the table check
# || true ensures migrations proceed even if this one-time fix fails
python manage.py fix_app_label || echo "Fix app label failed (skipping)"

echo "2. Running database migrations..."
python manage.py migrate --noinput

echo "3. Ensuring default superuser exists..."
python manage.py create_default_superuser

echo "--- Release Tasks Complete ---"
