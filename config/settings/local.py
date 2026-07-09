"""
Local development settings.
"""
from .base import *

DEBUG = True

# Database
ALLOWED_HOSTS = ['*']
CORS_ALLOW_ALL_ORIGINS = True
CSRF_TRUSTED_ORIGINS = ['http://100.80.123.2:8000', 'http://192.168.1.34:8000', 'http://127.0.0.1:8000', 'http://localhost:8000', 'http://omihome:8000', 'http://omihome:3000']
# Base settings require PostgreSQL/pgvector. Set DATABASE_URL in .env or use
# the local docker-compose default.

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
