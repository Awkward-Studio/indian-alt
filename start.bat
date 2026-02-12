@echo off
REM Startup script for Windows (local testing)
REM For Railway, use start.sh

echo Running database migrations...
python manage.py migrate --noinput

echo Creating/updating default superuser...
python manage.py create_default_superuser

echo Starting Gunicorn server...
python -m gunicorn config.wsgi:application --bind 0.0.0.0:8000 --access-logfile - --error-logfile - --log-level info
