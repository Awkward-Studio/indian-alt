"""
Local development settings.
"""
from .base import *

DEBUG = True

# Local dev default; can still be overridden by ALLOWED_HOSTS env var (handled in base.py)
ALLOWED_HOSTS = ['localhost', '127.0.0.1', '[::1]']

# Database
# Base settings default to SQLite unless DATABASE_URL is provided.
# For local Postgres, set DATABASE_URL in .env and USE_SQLITE=false.

# Email backend for development
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

# Logging
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'INFO',
    },
    'loggers': {
        'django': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
    },
}
