"""
Base settings for Django project.
"""
from pathlib import Path
from decouple import config, Csv
import os
import dj_database_url

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = config('SECRET_KEY', default='django-insecure-change-me-in-production')

# Application definition
INSTALLED_APPS = [
    'daphne',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    
    # Third party
    'rest_framework',
    'rest_framework_simplejwt',
    'drf_spectacular',
    'corsheaders',
    'django_extensions',
    'django_filters',
    'pgvector',
    'channels',
    
    # Local apps
    'accounts',
    'core',
    'banks',
    'contacts',
    'deals',
    'meetings',
    'api_requests',
    'microsoft',
    'ai_orchestrator',
]

ASGI_APPLICATION = 'config.asgi.application'

CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels_redis.core.RedisChannelLayer',
        'CONFIG': {
            "hosts": [config('REDIS_URL', default='redis://localhost:6379/0')],
        },
    },
}

def _csv_list(value: str):
    items = [s.strip() for s in (value or "").split(",") if s.strip()]
    return items

def _allowed_hosts(value: str):
    hosts = _csv_list(value)
    # If ALLOWED_HOSTS is missing/empty, do not brick deploy healthchecks.
    return hosts or ["*"]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    # WhiteNoise for static files in production
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

# Static files (CSS, JavaScript, Images)
# Django and admin expect STATIC_URL to start and end with a slash (e.g. "/static/").
# Using "static/" breaks admin CSS because URLs become relative to the current path.
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

# Avoid runtime warnings if STATIC_ROOT doesn't exist yet (e.g., first boot on Railway).
os.makedirs(STATIC_ROOT, exist_ok=True)

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Custom User Model (if needed - using Django's default for now)
# AUTH_USER_MODEL = 'accounts.User'

# REST Framework Configuration
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 20,
    'DEFAULT_FILTER_BACKENDS': (
        'rest_framework.filters.SearchFilter',
        'rest_framework.filters.OrderingFilter',
    ),
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
}

# JWT Settings
from datetime import timedelta

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(days=7),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=14),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'UPDATE_LAST_LOGIN': True,
    'ALGORITHM': 'HS256',
    'SIGNING_KEY': SECRET_KEY,
    'AUTH_HEADER_TYPES': ('Bearer',),
    'AUTH_HEADER_NAME': 'HTTP_AUTHORIZATION',
    'USER_ID_FIELD': 'id',
    'USER_ID_CLAIM': 'user_id',
    'AUTH_TOKEN_CLASSES': ('rest_framework_simplejwt.tokens.AccessToken',),
    'TOKEN_TYPE_CLAIM': 'token_type',
}

# drf-spectacular Settings (Swagger/OpenAPI)
SPECTACULAR_SETTINGS = {
    'TITLE': 'Indian Alt API',
    'DESCRIPTION': 'API documentation for Indian Alt investment management system',
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
    'COMPONENT_SPLIT_REQUEST': True,
    'SCHEMA_PATH_PREFIX': '/api/',
}

# Celery Configuration
# Prefer Railway Redis when REDIS_URL is present, while still allowing explicit Celery overrides.
REDIS_URL = config('REDIS_URL', default='redis://localhost:6379/0')
CELERY_BROKER_URL = config('CELERY_BROKER_URL', default=REDIS_URL)
CELERY_RESULT_BACKEND = config('CELERY_RESULT_BACKEND', default=REDIS_URL)
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 30 * 60  # 30 minutes

CELERY_TASK_ROUTES = {
    'ai_orchestrator.tasks.generate_chat_response_async': {'queue': 'high_priority'},
    'deals.tasks.analyze_folder_async': {'queue': 'high_priority'},
    'deals.tasks.analyze_additional_documents_async': {'queue': 'high_priority'},
    'deals.tasks.process_deal_folder_background': {'queue': 'low_priority'},
    'deals.tasks.process_single_document_async': {'queue': 'low_priority'},
    'deals.tasks.finalize_folder_background': {'queue': 'low_priority'},
}
CELERY_TASK_DEFAULT_QUEUE = 'default'

# CORS Settings
CORS_ALLOWED_ORIGINS = config(
    'CORS_ALLOWED_ORIGINS',
    default='http://localhost:3000,http://localhost:8000',
    cast=Csv()
)
CORS_ALLOW_CREDENTIALS = True

# Hosts
# Railway assigns the domain after first deploy, so default to '*' unless you set ALLOWED_HOSTS explicitly.
ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='*', cast=_allowed_hosts)

# AI Orchestrator Settings
OLLAMA_URL = config('OLLAMA_URL', default='http://localhost:11434')
OLLAMA_DEFAULT_TEXT_MODEL = config('OLLAMA_DEFAULT_TEXT_MODEL', default='llama3.1:latest')
OLLAMA_DEFAULT_VISION_MODEL = config('OLLAMA_DEFAULT_VISION_MODEL', default='llava:latest')

# Database Configuration
# 
# Railway-friendly setup:
# - Local: Uses SQLite by default (no DATABASE_URL needed)
# - Railway: Automatically uses PostgreSQL when DATABASE_URL is provided
# 
# To use PostgreSQL locally:
#   1. Install PostgreSQL locally
#   2. Create a database
#   3. Set DATABASE_URL in .env: postgresql://user:password@localhost:5432/dbname
#
# Railway automatically provides DATABASE_URL when you add a PostgreSQL service.
DATABASE_URL = config('DATABASE_URL', default='')

if DATABASE_URL:
    # Use PostgreSQL (Railway or local if DATABASE_URL is set)
    parsed_config = dj_database_url.parse(
        DATABASE_URL,
        conn_max_age=config('DB_CONN_MAX_AGE', default=600, cast=int),
        # Railway Postgres requires SSL, but allow override for local dev
        ssl_require=config('DB_SSL_REQUIRE', default=True, cast=bool),
    )
    
    # Add connection options for Railway Postgres
    if 'OPTIONS' not in parsed_config:
        parsed_config['OPTIONS'] = {}
    
    # Ensure proper SSL settings for Railway
    if parsed_config.get('HOST') and 'railway.app' in str(parsed_config.get('HOST', '')):
        parsed_config['OPTIONS']['sslmode'] = 'require'
    
    DATABASES = {'default': parsed_config}
else:
    # Default to SQLite for local development
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': config('SQLITE_PATH', default=str(BASE_DIR / 'db.sqlite3')),
        }
    }
